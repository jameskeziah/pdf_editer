from __future__ import annotations

from pathlib import Path

import pytest

from pdf_branding.journal import RunLock


class _FailingStream:
    def __init__(self, stream, failure):
        self.stream, self.failure, self.seeks = stream, failure, 0

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def flush(self):
        if self.failure == "flush":
            raise OSError("injected initial lock flush failure")
        return self.stream.flush()

    def seek(self, *args):
        self.seeks += 1
        if self.failure == "release" and self.seeks == 3:
            raise OSError("injected unlock seek failure")
        return self.stream.seek(*args)


@pytest.mark.parametrize("failure", ["flush", "release"])
def test_run_lock_closes_handle_after_setup_or_release_failure(tmp_path, monkeypatch, failure):
    lock, original_open, opened = RunLock(tmp_path), Path.open, []

    def patched_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == lock.path:
            opened.append(stream)
            return _FailingStream(stream, failure)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    with pytest.raises(OSError, match="injected"):
        with lock:
            pass
    assert lock.stream is None and opened and all(stream.closed for stream in opened)
    # The underlying OS lock is released even when explicit unlock could not run.
    monkeypatch.setattr(Path, "open", original_open)
    with RunLock(tmp_path):
        pass
