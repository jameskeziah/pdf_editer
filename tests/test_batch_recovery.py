from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from pdf_branding import batch
from pdf_branding.journal import JobJournal, RunLock


def test_interrupted_job_is_recovered_from_durable_database(tmp_path: Path):
    output = tmp_path / "out"
    output.mkdir()
    source = tmp_path / "source" / "first.pdf"
    first = JobJournal(output, source.parent, "profile", {})
    first.record({"source": str(source), "status": "running"}, increment_attempt=True)
    first.close()  # Simulate a stopped process with no finish/checkpoint export.
    second = JobJournal(output, source.parent, "profile", {})
    assert second.get(source)["status"] == "interrupted"
    assert second.get(source)["attempts"] == 1
    second.record({"source": str(source), "status": "running"}, increment_attempt=True)
    assert second.get(source)["attempts"] == 2
    second.finish("completed")
    second.close()
    manifest = json.loads((output / "batch_manifest.json").read_text())
    assert manifest["jobs"][0]["attempts"] == 2


def test_run_lock_rejects_concurrent_batch_and_releases(tmp_path: Path):
    with RunLock(tmp_path):
        with pytest.raises(RuntimeError, match="Another batch"):
            with RunLock(tmp_path):
                pass
    with RunLock(tmp_path):
        pass


def test_job_manifest_cannot_be_reused_for_a_different_source_library(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    first = JobJournal(output, tmp_path / "first", "profile", {})
    first.finish("completed")
    first.close()
    with pytest.raises(ValueError, match="different input library"):
        JobJournal(output, tmp_path / "second", "profile", {})


def _mock_jobs(tmp_path, monkeypatch, number=3):
    root = tmp_path / "input"
    root.mkdir()
    sources = []
    for index in range(number):
        source = root / f"{index}.pdf"
        source.write_bytes(f"source {index}".encode())
        sources.append(source)
    meta = SimpleNamespace(class_name="6", subject="physics", chapter="Measurement and Motion", material_type="DPP")
    monkeypatch.setattr(batch, "_meta_quick", lambda *_: meta)
    return root, tmp_path / "output", sources


def test_cancel_checkpoints_remaining_jobs_and_resume_skips_verified_output(tmp_path, monkeypatch):
    root, output, sources = _mock_jobs(tmp_path, monkeypatch)
    cancel = threading.Event()
    calls, events = [], []

    def process(source, dest, *_):
        calls.append(source.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        if len(calls) == 1:
            cancel.set()
        return SimpleNamespace(status="completed", output=dest, message="", plan=None, qa=None)

    monkeypatch.setattr(batch, "process_pdf", process)
    assert batch.main([str(root), str(output)], event_callback=events.append, cancel_event=cancel) == 130
    saved = json.loads((output / "batch_manifest.json").read_text())
    assert saved["counts"] == {"completed": 1, "cancelled": 2}
    assert any(e.get("status") == "completed" for e in events)
    cancel.clear()
    assert batch.main([str(root), str(output), "--resume"], cancel_event=cancel) == 0
    assert calls == [s.name for s in sources]
    records = json.loads((output / "batch_report.json").read_text())
    assert [r["status"] for r in records] == ["cached", "completed", "completed"]


def test_retry_failure_but_do_not_automatically_retry_qa_failure(tmp_path, monkeypatch):
    root, output, sources = _mock_jobs(tmp_path, monkeypatch, 2)
    attempts = {}

    def process(source, dest, *_):
        attempts[source.name] = attempts.get(source.name, 0) + 1
        if source == sources[1]:
            return SimpleNamespace(status="qa_failed", output=None, message="body loss", plan=None, qa=None)
        if attempts[source.name] == 1:
            return SimpleNamespace(status="failed", output=None, message="temporary I/O", plan=None, qa=None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        return SimpleNamespace(status="completed", output=dest, message="", plan=None, qa=None)

    monkeypatch.setattr(batch, "process_pdf", process)
    assert batch.main([str(root), str(output), "--retries", "2"]) == 2
    assert attempts == {sources[0].name: 2, sources[1].name: 1}
    assert batch.main([str(root), str(output), "--retry-failed"]) == 2
    assert attempts == {sources[0].name: 2, sources[1].name: 2}


def test_same_input_and_output_rejected_before_source_mutation(tmp_path):
    with pytest.raises(ValueError, match="different"):
        batch.main([str(tmp_path), str(tmp_path)])
