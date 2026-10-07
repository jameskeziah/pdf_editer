from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from pdf_branding import batch, gui


ROOT = Path(__file__).parents[1]


def _settings(tmp_path: Path) -> dict:
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    return {"input_root": str(source), "output_root": str(output), "profile": str(ROOT / "profiles/sskem.json"),
            "class": "6", "subject": "Physics", "chapter": "Measurement and Motion", "material": "Exercise",
            "workers": 2, "retries": 1, "plan_previews": True, "contact_sheets": True,
            "overwrite": False, "no_cache": False}


@pytest.mark.parametrize("mode,flag", [("dry", "--dry-run"), ("analyze", "--analyze-only"), ("process", None)])
def test_gui_argv_round_trips_batch_parser(tmp_path: Path, mode, flag):
    settings = _settings(tmp_path)
    argv = gui.build_argv(settings, mode, resume=True, retry_failed=True)
    args = batch.build_parser().parse_args(argv)
    assert args.class_filter == "6" and args.subject_filter == "physics"
    assert args.chapter_filter == "Measurement and Motion" and args.material_filter == "Exercise"
    assert args.workers == 2 and args.retries == 1 and args.resume and args.retry_failed
    assert args.contact_sheets
    if flag:
        assert flag in argv
    assert ("--preview-plans" in argv) == (mode == "analyze")


def test_gui_state_updates_existing_job_and_resets_on_new_run():
    state = gui.GuiState()
    state.begin()
    state.accept({"type": "planned", "total": 2})
    state.accept({"type": "job", "source": "A.pdf", "status": "running"})
    state.accept({"type": "job", "source": "A.pdf", "status": "completed"})
    state.accept({"type": "job", "source": "B.pdf", "status": "cancelled"})
    assert len(state.records) == 2 and state.completed == 2 and state.active
    state.accept({"type": "finished", "exit_code": 130, "message": "Cancelled"})
    assert not state.active and state.exit_code == 130 and state.status == "Cancelled"
    state.begin()
    assert state.active and state.records == {} and state.exit_code is None


@pytest.fixture(scope="module")
def tk_session(tk_display):
    return tk_display


@pytest.fixture
def tk_root(tk_session):
    # Reuse one Tcl interpreter; repeatedly loading/unloading Tk's native library
    # can fail on Windows even though the GUI is available. Each test still gets
    # an isolated, withdrawn top-level window and widget tree.
    import tkinter as tk
    root = tk.Toplevel(tk_session)
    root.withdraw()
    yield root
    for callback in root.tk.call("after", "info"):
        root.after_cancel(callback)
    try:
        root.destroy()
    except tk.TclError:
        pass


def _app(tmp_path, tk_root, runner, *, manifest=None):
    settings = _settings(tmp_path)
    settings_path = tmp_path / "test-gui-settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    if manifest is not None:
        (Path(settings["output_root"]) / "batch_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    app = gui.BrandingApp(tk_root, settings_path=settings_path, runner=runner)
    return app, settings


def _pump(root, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Tk worker/event state did not settle before timeout")


def test_withdrawn_gui_live_progress_cancel_and_resume(tmp_path, tk_root, monkeypatch):
    calls = []
    source_a, source_b = str(tmp_path / "A.pdf"), str(tmp_path / "B.pdf")

    def fake_runner(argv, *, event_callback, cancel_event):
        calls.append(list(argv))
        event_callback({"type": "planned", "total": 2})
        if len(calls) == 1:
            event_callback({"type": "job", "source": source_a, "status": "running", "attempts": 1})
            assert cancel_event.wait(3), "GUI did not pass cancellation to background runner"
            event_callback({"type": "job", "source": source_a, "status": "completed", "attempts": 1})
            event_callback({"type": "job", "source": source_b, "status": "cancelled", "attempts": 0})
            return 130
        assert "--resume" in argv
        assert not cancel_event.is_set()
        event_callback({"type": "job", "source": source_a, "status": "cached", "attempts": 1})
        event_callback({"type": "job", "source": source_b, "status": "completed", "attempts": 1})
        return 0

    monkeypatch.setattr(gui.webbrowser, "open", lambda *_: pytest.fail("Test must not open a browser"))
    app, settings = _app(tmp_path, tk_root, fake_runner)
    app.start("process")
    _pump(tk_root, lambda: app.state.records.get(source_a, {}).get("status") == "running")
    assert all(str(button.cget("state")) == "disabled" for button in app.run_buttons)
    assert str(app.cancel_button.cget("state")) == "normal"
    app.cancel()
    _pump(tk_root, lambda: not app.state.active and not app.thread.is_alive())
    assert app.state.exit_code == 130 and app.state.completed == 2
    assert len(app.results.get_children()) == 2
    app.start("process", resume=True)
    _pump(tk_root, lambda: len(calls) == 2 and not app.state.active and not app.thread.is_alive())
    assert app.state.exit_code == 0 and app.state.records[source_a]["status"] == "cached"
    assert app.state.records[source_b]["status"] == "completed"
    assert app.progress_var.get() == 2
    assert all(str(button.cget("state")) == "normal" for button in app.run_buttons)
    saved = json.loads(app.settings_path.read_text(encoding="utf-8"))
    assert saved["input_root"] == settings["input_root"] and saved["output_root"] == settings["output_root"]


def test_gui_does_not_start_another_worker_before_previous_thread_exits(tmp_path, tk_root):
    calls, release = [], threading.Event()

    def fake_runner(argv, *, event_callback, cancel_event):
        calls.append(list(argv))
        event_callback({"type": "finished", "exit_code": 0, "status": "completed"})
        release.wait(3)
        return 0

    app, _ = _app(tmp_path, tk_root, fake_runner)
    try:
        app.start("process")
        _pump(tk_root, lambda: bool(calls) and app.events.empty())
        app.start("process", resume=True)
        time.sleep(.05)
        assert len(calls) == 1, "A finished event must not unlock a still-running worker thread"
    finally:
        release.set()
        if app.thread:
            app.thread.join(timeout=2)
        _pump(tk_root, lambda: not app.state.active)


def test_gui_invalid_numeric_worker_setting_shows_validation_error(tmp_path, tk_root, monkeypatch):
    from tkinter import messagebox
    messages = []
    monkeypatch.setattr(messagebox, "showerror", lambda *args: messages.append(args))
    app, _ = _app(tmp_path, tk_root, lambda *_args, **_kwargs: pytest.fail("Invalid settings must not launch a worker"))
    app.vars["workers"].set("not a number")
    app.start("process")
    assert messages and not app.state.active and app.thread is None


@pytest.mark.parametrize("manifest", [[], {"jobs": None}, {"jobs": [None, {"status": "completed"}]}])
def test_gui_malformed_saved_manifest_does_not_crash(tmp_path, tk_root, manifest):
    app, _ = _app(tmp_path, tk_root, lambda *_args, **_kwargs: 0, manifest=manifest)
    assert app.state.records == {}
