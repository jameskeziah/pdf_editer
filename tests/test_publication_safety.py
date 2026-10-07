from __future__ import annotations

import json
import os
from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_branding import engine, publication
from pdf_branding.atomic import FileLock, LockBusyError
from pdf_branding.config import load_profile
from pdf_branding.models import QAReport
from pdf_branding.publication import PublicationError, PublicationTransaction, journal_path, recover_publication


ROOT = Path(__file__).parents[1]


@pytest.fixture
def job(tmp_path: Path, monkeypatch):
    source = tmp_path / "6th class/physics/01 Measurement and Motion/Chapter.pdf"
    source.parent.mkdir(parents=True)
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((30, 28), "ALLEN", fontsize=15)
    page.insert_text((45, 90), "Academic body content must remain readable.", fontsize=11)
    document.save(source)
    document.close()
    output = tmp_path / "output/fixture.pdf"
    failed = output.with_name("fixture.qa_failed.pdf")
    reports = tmp_path / "reports"
    plan, qa = reports / "fixture.plan.json", reports / "fixture.qa.json"
    brand = load_profile(ROOT / "profiles/sskem.json")
    monkeypatch.setattr(engine, "run_qa", lambda src, staged, *_: QAReport(str(source), str(staged), True, len(src), len(src)))
    return source, output, failed, reports, plan, qa, brand


def _run(job):
    source, output, _, reports, _, _, brand = job
    return engine.process_pdf(source, output, source.parents[3], brand, reports, Path("fixture.pdf"))


def _seed(job, previous: bool):
    _, output, failed, _, plan, qa, _ = job
    files = {failed: b"previous failed PDF"}
    if previous:
        files.update({output: b"previous production PDF", plan: b"previous plan", qa: b"previous QA"})
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return files


def _assert_restored(job, files):
    for path, data in files.items():
        assert path.read_bytes() == data
    for path in (job[1], job[4], job[5]):
        if path not in files:
            assert not path.exists()
    assert not journal_path(job[1]).exists()
    assert not [path for parent in (job[1].parent, job[3]) if parent.exists()
                for path in parent.iterdir() if ".stage" in path.name or ".backup" in path.name]


@pytest.mark.parametrize("previous", [False, True])
def test_report_write_failure_never_publishes_pdf_or_partial_reports(job, monkeypatch, previous):
    files = _seed(job, previous)
    writer = engine._write_json_atomic
    calls = 0

    def fail_second(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected QA report write failure")
        writer(path, data)

    monkeypatch.setattr(engine, "_write_json_atomic", fail_second)
    result = _run(job)
    assert result.status == "failed" and not result.publication_committed
    assert "report write failure" in result.message
    _assert_restored(job, files)


@pytest.mark.parametrize("previous", [False, True])
def test_pdf_replace_failure_rolls_back_already_replaced_reports(job, monkeypatch, previous):
    files = _seed(job, previous)
    replace = os.replace

    def fail_pdf(source, destination):
        if Path(destination) == job[1].resolve():
            raise OSError("injected PDF replacement failure")
        return replace(source, destination)

    monkeypatch.setattr(publication.os, "replace", fail_pdf)
    result = _run(job)
    assert result.status == "failed" and not result.recovery_required
    assert "previous files were restored" in result.message
    _assert_restored(job, files)


def test_failed_qa_preserves_previous_production_and_failed_pdf_until_commit(job, monkeypatch):
    files = _seed(job, True)
    source, output, failed, _, _, qa, _ = job
    monkeypatch.setattr(engine, "run_qa", lambda src, staged, *_: QAReport(str(source), str(staged), False, len(src), len(src)))
    writer = engine._write_json_atomic
    monkeypatch.setattr(engine, "_write_json_atomic", lambda *_: (_ for _ in ()).throw(OSError("reports unavailable")))
    result = _run(job)
    assert result.status == "failed"
    _assert_restored(job, files)
    monkeypatch.setattr(engine, "_write_json_atomic", writer)
    result = _run(job)
    assert result.status == "qa_failed" and result.publication_committed
    assert output.read_bytes() == files[output]
    assert failed.read_bytes() != files[failed]
    report = json.loads(qa.read_text(encoding="utf-8"))
    assert report["passed"] is False and Path(report["output"]) == failed


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("interrupt_after_pdf", [False, True])
def test_interrupted_publication_recovers_old_or_absent_artifacts(job, monkeypatch, previous, interrupt_after_pdf):
    files = _seed(job, previous)
    replace, write_journal = os.replace, publication.atomic_write_json

    def interrupt_pdf(source, destination):
        if Path(destination) == job[1].resolve():
            raise KeyboardInterrupt("injected process interruption")
        return replace(source, destination)

    def interrupt_commit(path, data):
        if data.get("state") == "committed":
            raise KeyboardInterrupt("interrupted before durable commit")
        return write_journal(path, data)

    if interrupt_after_pdf:
        monkeypatch.setattr(publication, "atomic_write_json", interrupt_commit)
    else:
        monkeypatch.setattr(publication.os, "replace", interrupt_pdf)
    with pytest.raises(KeyboardInterrupt):
        _run(job)
    assert journal_path(job[1]).exists()
    assert json.loads(journal_path(job[1]).read_text(encoding="utf-8"))["state"] == "prepared"
    monkeypatch.setattr(publication.os, "replace", replace)
    monkeypatch.setattr(publication, "atomic_write_json", write_journal)
    with FileLock(job[1]):
        assert "Rolled back" in recover_publication(job[1], [job[1], job[2], job[4], job[5]])
        assert recover_publication(job[1], [job[1], job[2], job[4], job[5]]) == ""
    _assert_restored(job, files)


def test_failed_rollback_keeps_journal_and_backups_for_next_run(job, monkeypatch):
    files = _seed(job, True)
    replace = os.replace
    failing = True

    def fail_install_and_rollback(source, destination):
        destination = Path(destination)
        if failing and destination in (job[1].resolve(), job[5].resolve()):
            # Let the new QA report install, then prevent rollback of its backup.
            if destination == job[1].resolve() or ".backup" in str(source):
                raise OSError("injected lock preventing replace")
        return replace(source, destination)

    monkeypatch.setattr(publication.os, "replace", fail_install_and_rollback)
    result = _run(job)
    assert result.status == "failed" and result.recovery_required
    assert journal_path(job[1]).exists()
    failing = False
    with FileLock(job[1]):
        recover_publication(job[1], [job[1], job[2], job[4], job[5]])
    _assert_restored(job, files)


def test_recovery_refuses_to_overwrite_external_edit(job, monkeypatch):
    _seed(job, True)
    replace = os.replace

    def interrupt_pdf(source, destination):
        if Path(destination) == job[1].resolve():
            raise KeyboardInterrupt
        return replace(source, destination)

    monkeypatch.setattr(publication.os, "replace", interrupt_pdf)
    with pytest.raises(KeyboardInterrupt):
        _run(job)
    monkeypatch.setattr(publication.os, "replace", replace)
    job[5].write_bytes(b"external edit after interruption")
    with FileLock(job[1]), pytest.raises(PublicationError, match="changed outside"):
        recover_publication(job[1], [job[1], job[2], job[4], job[5]])
    assert job[5].read_bytes() == b"external edit after interruption"
    assert journal_path(job[1]).exists()


def test_successful_publication_reports_final_path_and_cleans_stages(job):
    _seed(job, True)
    result = _run(job)
    assert result.status == "completed" and result.publication_committed
    report = json.loads(job[5].read_text(encoding="utf-8"))
    assert report["passed"] is True and Path(report["output"]) == job[1]
    assert job[2].read_bytes() == b"previous failed PDF"
    assert not journal_path(job[1]).exists()


def test_output_lock_is_exclusive_and_reusable(tmp_path: Path):
    path = tmp_path / "output.pdf"
    with FileLock(path):
        with pytest.raises(LockBusyError):
            with FileLock(path):
                pass
    with FileLock(path):
        pass


def test_recovery_rejects_unapproved_journal_targets(tmp_path: Path):
    output, report, victim = tmp_path / "output.pdf", tmp_path / "report.json", tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    with PublicationTransaction(output, [output, report]) as transaction:
        transaction.stage_path(output).write_bytes(b"PDF")
        transaction.stage_path(report).write_bytes(b"report")
        transaction.commit()
    # A structurally invalid/unapproved recovery record must fail before mutations.
    journal_path(output).write_text(json.dumps({"version": 1, "state": "prepared", "anchor": str(output),
        "transaction_id": "a" * 32, "entries": [{"target": str(victim)}]}), encoding="utf-8")
    with FileLock(output), pytest.raises(PublicationError, match="unexpected"):
        recover_publication(output, [output, report])
    assert victim.read_bytes() == b"keep me"
