from __future__ import annotations

import json
from pathlib import Path
import shutil
import time

from PIL import Image
import pymupdf as fitz
import pytest

from pdf_branding import batch, gui
from pdf_branding.cache import BatchCache
from pdf_branding.config import load_profile


ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "profiles/sskem.json"
RELATIVE_SOURCE = Path("6th class/science/07 Measurement and Motion/NCERT _ solutions_Measurement and Motion.pdf")


@pytest.fixture
def withdrawn_root(tk_display):
    tk = pytest.importorskip("tkinter")
    root = tk.Toplevel(tk_display)
    root.withdraw()
    yield root
    for callback in root.tk.call("after", "info"):
        root.after_cancel(callback)
    try:
        root.destroy()
    except tk.TclError:
        pass


def _pump(app: gui.BrandingApp, predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError(f"GUI pipeline did not settle: {app.state!r}\n{app.log.get('1.0', 'end')}")


def _finished(app: gui.BrandingApp):
    return not app.state.active and app.thread is not None and not app.thread.is_alive()


def _select_review(app: gui.BrandingApp, record: dict):
    rows = app.results.get_children()
    assert len(rows) == 1
    row = rows[0]
    assert app.results.item(row, "values")[0] == record["status"]
    app.photo = None
    app.results.selection_set(row)
    app.results.event_generate("<<TreeviewSelect>>")
    _pump(app, lambda: app.photo is not None, timeout=5)
    assert app.selected_record() == record
    assert app.preview_files and all(path.is_file() for path in app.preview_files)
    assert app.preview_label.cget("text") == f"Contact sheet 1/{len(app.preview_files)}"
    assert any(app.canvas.type(item) == "image" for item in app.canvas.find_all())


def test_withdrawn_gui_real_pipeline_review_and_cached_resume(tmp_path, withdrawn_root, monkeypatch):
    """Exercise actual Tk widgets and batch processing; no visible UI claim."""
    original = ROOT / "foundation_notes" / RELATIVE_SOURCE
    if not original.is_file():
        pytest.skip("Bundled real NCERT source is unavailable")
    original_hash = BatchCache.sha256(original)
    source_root, output_root = tmp_path / "foundation_notes", tmp_path / "output"
    source = source_root / RELATIVE_SOURCE
    source.parent.mkdir(parents=True)
    shutil.copy2(original, source)
    assert BatchCache.sha256(source) == original_hash
    settings = {
        "input_root": str(source_root), "output_root": str(output_root), "profile": str(PROFILE),
        "class": "6", "subject": "Physics", "chapter": "Measurement and Motion", "material": "",
        "workers": 1, "retries": 0, "plan_previews": True, "contact_sheets": True,
        "overwrite": False, "no_cache": False,
    }
    settings_path = tmp_path / "gui-settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(gui.webbrowser, "open", lambda *_: pytest.fail("Pipeline test must not open a browser"))
    if hasattr(gui.os, "startfile"):
        monkeypatch.setattr(gui.os, "startfile", lambda *_: pytest.fail("Pipeline test must not open another app"))
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "showerror", lambda *args: pytest.fail(f"GUI validation failed: {args}"))
    app = gui.BrandingApp(withdrawn_root, settings_path=settings_path)
    assert app.runner is batch.main
    try:
        # Invoke the same Process + QA and Resume buttons used by the app.
        app.run_buttons[2].invoke()
        _pump(app, lambda: _finished(app))
        assert app.state.exit_code == 0 and app.state.total == app.state.completed == 1
        record = app.state.records[str(source.resolve())]
        assert record["status"] == "completed" and record["attempts"] == 1
        assert record["source_sha256"] == original_hash
        assert f"completed: {source.name}" in app.log.get("1.0", "end")
        assert app.progress_var.get() == 1
        assert "exit code 0" in app.status_var.get()
        output = Path(record["output"])
        assert output == output_root / RELATIVE_SOURCE and output.is_file()
        output_hash, output_mtime = BatchCache.sha256(output), output.stat().st_mtime_ns
        reports = output_root / "_reports" / RELATIVE_SOURCE.parent
        qa = json.loads((reports / (source.stem + ".qa.json")).read_text(encoding="utf-8"))
        assert qa["passed"] is True and Path(qa["output"]) == output
        plan = json.loads((reports / (source.stem + ".plan.json")).read_text(encoding="utf-8"))
        with fitz.open(source) as before, fitz.open(output) as after:
            assert len(plan["pages"]) == len(before) == len(after) == 2
        index = Path(record["review_index"])
        assert index.is_file()
        review = json.loads((index.parent / "review.json").read_text(encoding="utf-8"))
        assert Path(review["source"]) == source.resolve() and Path(review["final"]) == output
        assert review["qa_passed"] is True
        assert [page["page_number"] for page in review["pages"]] == [1, 2]
        for page in review["pages"]:
            for panel in ("source", "plan", "final"):
                with Image.open(index.parent / page[panel]) as image:
                    image.load()
                    assert image.width > 500 and image.height > 500
        _select_review(app, record)
        contact_hashes = {path: BatchCache.sha256(path) for path in app.preview_files}
        for path in app.preview_files:
            with Image.open(path) as image:
                image.load()
                assert image.size == (1320, 1840)
        assert BatchCache.sha256(original) == BatchCache.sha256(source) == original_hash

        app.run_buttons[3].invoke()
        _pump(app, lambda: _finished(app))
        assert app.state.exit_code == 0 and app.state.completed == 1
        cached = app.state.records[str(source.resolve())]
        assert cached["status"] == "cached" and cached["attempts"] == 1
        assert cached["source_sha256"] == original_hash and cached["review_index"] == str(index)
        assert f"cached: {source.name}" in app.log.get("1.0", "end")
        _select_review(app, cached)
        assert BatchCache.sha256(output) == output_hash and output.stat().st_mtime_ns == output_mtime
        assert contact_hashes == {path: BatchCache.sha256(path) for path in app.preview_files}
        cache = BatchCache(output_root)
        assert cache.is_fresh(RELATIVE_SOURCE, source, output, cache.profile_hash(load_profile(PROFILE)))
        manifest = json.loads((output_root / "batch_manifest.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "completed" and len(manifest["jobs"]) == 1
        assert manifest["jobs"][0]["status"] == "cached"
        assert json.loads(settings_path.read_text(encoding="utf-8"))["output_root"] == str(output_root)
    finally:
        app.cancel_event.set()
        if app.thread is not None:
            app.thread.join(timeout=15)
        assert app.thread is None or not app.thread.is_alive(), "Actual runner must release its resources"
        assert BatchCache.sha256(original) == BatchCache.sha256(source) == original_hash
