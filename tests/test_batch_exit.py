from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pdf_branding import batch


@pytest.mark.parametrize("status, expected_exit", [("completed", 0), ("qa_failed", 2), ("failed", 2)])
def test_batch_exit_reports_processing_failure(tmp_path: Path, monkeypatch, status: str, expected_exit: int):
    source = tmp_path / "source" / "Race.pdf"
    output_root = tmp_path / "output"
    failed_output = output_root / "Race.qa_failed.pdf"
    meta = SimpleNamespace(class_name="6", subject="physics", chapter="Measurement and Motion", material_type="DPP")
    cache = Mock()
    cache.is_fresh.return_value = False
    cache.profile_hash.return_value = "test-profile"
    monkeypatch.setattr(batch, "BatchCache", lambda _: cache)
    monkeypatch.setattr(batch, "discover_pdfs", lambda *_: [source])
    monkeypatch.setattr(batch, "exclusion_reason", lambda *_: None)
    monkeypatch.setattr(batch, "_meta_quick", lambda *_: meta)
    monkeypatch.setattr(batch, "process_pdf", lambda *_: SimpleNamespace(
        status=status,
        output=failed_output if status == "qa_failed" else output_root / "Race.pdf" if status == "completed" else None,
        message="QA failed" if status == "qa_failed" else "",
    ))

    exit_code = batch.main([str(source.parent), str(output_root), "--overwrite"])

    assert exit_code == expected_exit
    records = json.loads((output_root / "batch_report.json").read_text(encoding="utf-8"))
    assert records[0]["status"] == status
    if status == "completed":
        cache.update.assert_called_once()
    else:
        cache.update.assert_not_called()
    if status == "qa_failed":
        assert records[0]["output"] == str(failed_output)
