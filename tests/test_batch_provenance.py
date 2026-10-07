from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from pdf_branding import batch


@pytest.mark.parametrize("kind", ["missing", "ancestor"])
def test_invalid_input_output_topology_creates_no_job_state(tmp_path, kind):
    root = tmp_path / "library"
    if kind == "ancestor":
        root.mkdir()
        output = tmp_path
    else:
        output = tmp_path / "output"
    with pytest.raises(ValueError):
        batch.main([str(root), str(output)])
    assert not (output / "batch_manifest.json").exists()


def test_dotted_plan_names_and_content_provenance_are_preserved(tmp_path, monkeypatch):
    root, output = tmp_path / "library", tmp_path / "output"
    root.mkdir()
    for name in ["notes.v1.pdf", "notes.v2.pdf"]:
        (root / name).write_bytes(name.encode())
    meta = SimpleNamespace(class_name="6", subject="physics", chapter="Motion", material_type="NOTES")
    monkeypatch.setattr(batch, "_meta_quick", lambda *_: meta)
    monkeypatch.setattr(batch, "analyze_pdf", lambda source, *_: {"source": str(source)})
    assert batch.main([str(root), str(output), "--analyze-only"]) == 0
    for name in ["notes.v1", "notes.v2"]:
        assert (output / "_reports" / (name + ".plan.json")).is_file()
    manifest = json.loads((output / "batch_manifest.json").read_text())
    for row in manifest["jobs"]:
        assert row["source_sha256"] == hashlib.sha256(__import__("pathlib").Path(row["source"]).read_bytes()).hexdigest()


@pytest.mark.parametrize("selection", [[], {}, 2, {"sources": ["missing.pdf"]}, {"sources": ["../outside.pdf"]}])
def test_invalid_selection_cannot_silently_succeed(tmp_path, selection):
    root = tmp_path / "library"
    root.mkdir()
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection))
    with pytest.raises(ValueError):
        batch.main([str(root), str(tmp_path / "output"), "--selection-manifest", str(manifest)])


def test_observer_exception_cannot_interrupt_durable_batch(tmp_path, monkeypatch):
    root, output = tmp_path / "library", tmp_path / "output"
    root.mkdir()
    (root / "notes.pdf").write_bytes(b"fixture")
    meta = SimpleNamespace(class_name="6", subject="physics", chapter="Motion", material_type="NOTES")
    monkeypatch.setattr(batch, "_meta_quick", lambda *_: meta)
    def observer(_):
        raise RuntimeError("display stopped")
    with pytest.warns(RuntimeWarning, match="Progress callback failed"):
        assert batch.main([str(root), str(output), "--dry-run"], event_callback=observer) == 0
    assert json.loads((output / "batch_manifest.json").read_text())["status"] == "completed"
