from __future__ import annotations

import json
from pathlib import Path
import threading

import pymupdf as fitz

from pdf_branding import batch
from pdf_branding.cache import BatchCache
from pdf_branding.config import load_profile


ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "profiles/sskem.json"


def _library(root: Path, count: int, invalid: int | None = None) -> list[Path]:
    directory = root / "6th class/physics/07 Measurement and Motion"
    directory.mkdir(parents=True)
    files = []
    for index in range(count):
        path = directory / f"Exercise _ solutions_Measurement and Motion_{index + 1:02}.pdf"
        files.append(path)
        if index == invalid:
            path.write_bytes(b"invalid PDF, worker must report failure and continue")
            continue
        doc = fitz.open()
        for page_index in range(2):
            page = doc.new_page(width=595, height=842)
            page.insert_text((30, 28), "ALLEN", fontsize=15)
            page.insert_text((500, 28), "Physics", fontsize=10)
            page.draw_line((30, 46), (565, 46), color=(0, 0, .45))
            page.insert_text((45, 95), f"Question {index + 1}.{page_index + 1}: a ruler measures length.", fontsize=11)
            page.insert_text((45, 135), f"Solution {index + 1}.{page_index + 1}: metre is the standard unit.", fontsize=11)
            page.draw_line((30, 812), (565, 812), color=(0, 0, .45))
            page.insert_text((520, 825), f"[{page_index + 1}]", fontsize=8)
        doc.save(path)
        doc.close()
    return files


def _manifest(output: Path) -> dict:
    return json.loads((output / "batch_manifest.json").read_text(encoding="utf-8"))


def _consistent_completed(source_root: Path, output_root: Path, *, expected: int):
    manifest = _manifest(output_root)
    records = manifest["jobs"]
    completed = [record for record in records if record["status"] in ("completed", "cached")]
    assert len(completed) == expected
    cache = BatchCache(output_root)
    fingerprint = cache.profile_hash(load_profile(PROFILE))
    assert manifest["profile_fingerprint"] == fingerprint
    for record in completed:
        source, output = Path(record["source"]), Path(record["output"])
        relative = source.relative_to(source_root)
        assert cache.is_fresh(relative, source, output, fingerprint)
        qa_path = output_root / "_reports" / relative.parent / (relative.stem + ".qa.json")
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        assert qa["passed"] is True and Path(qa["output"]) == output
        with fitz.open(output) as rendered:
            assert len(rendered) == 2
            assert "a ruler measures length" in rendered[0].get_text()
        assert not output.with_name(f".{output.name}.publication.json").exists()
    return records


def test_actual_parallel_outputs_match_serial_rendering_and_cache(tmp_path: Path):
    sources, parallel, serial = tmp_path / "sources", tmp_path / "parallel", tmp_path / "serial"
    files = _library(sources, 3)
    before = {path: BatchCache.sha256(path) for path in files}
    assert batch.main([str(sources), str(parallel), "--workers", "2"]) == 0
    assert batch.main([str(sources), str(serial), "--workers", "1"]) == 0
    _consistent_completed(sources, parallel, expected=3)
    _consistent_completed(sources, serial, expected=3)
    for source in files:
        relative = source.relative_to(sources)
        with fitz.open(parallel / relative) as first, fitz.open(serial / relative) as second:
            for left, right in zip(first, second):
                assert left.get_text() == right.get_text()
                assert left.get_pixmap(alpha=False).samples == right.get_pixmap(alpha=False).samples
    assert before == {path: BatchCache.sha256(path) for path in files}


def test_actual_parallel_invalid_pdf_does_not_stop_other_workers(tmp_path: Path):
    sources, output = tmp_path / "sources", tmp_path / "output"
    files = _library(sources, 4, invalid=1)
    assert batch.main([str(sources), str(output), "--workers", "2"]) == 2
    records = _consistent_completed(sources, output, expected=3)
    failed = [record for record in records if record["status"] == "failed"]
    assert len(failed) == 1 and Path(failed[0]["source"]) == files[1]
    assert failed[0]["message"] and failed[0]["output"] is None
    assert _manifest(output)["status"] == "failed"
    assert not (output / files[1].relative_to(sources)).exists()


def test_parallel_cancellation_is_bounded_and_resume_uses_verified_outputs(tmp_path: Path):
    sources, output = tmp_path / "sources", tmp_path / "output"
    files = _library(sources, 5)
    cancel = threading.Event()
    events = []

    def cancel_on_first_running(event):
        events.append(event)
        if event.get("type") == "job" and event.get("status") == "running":
            cancel.set()

    assert batch.main([str(sources), str(output), "--workers", "2"],
                      event_callback=cancel_on_first_running, cancel_event=cancel) == 130
    first = _manifest(output)
    started = [record for record in first["jobs"] if record["attempts"] > 0]
    assert len(started) <= 2
    assert all(record["status"] in {"completed", "cancelled"} for record in first["jobs"])
    assert sum(record["status"] == "cancelled" for record in first["jobs"]) >= 3
    assert first["status"] == "cancelled"
    completed = {record["source"]: record for record in first["jobs"] if record["status"] == "completed"}
    cancel.clear()
    assert batch.main([str(sources), str(output), "--workers", "2", "--resume"], cancel_event=cancel) == 0
    records = _consistent_completed(sources, output, expected=len(files))
    for record in records:
        assert record["attempts"] == 1
        if record["source"] in completed:
            assert record["status"] == "cached"
    assert _manifest(output)["status"] == "completed"
