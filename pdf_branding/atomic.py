from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class LockBusyError(RuntimeError):
    pass


class FileLock:
    """Nonblocking OS lock; interruption releases the lock automatically.

    Retain the small lock file to prevent unlink/recreate inode races.
    """
    def __init__(self, target: Path):
        self.path = target.with_name(f".{target.name}.lock")
        self._stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise LockBusyError(f"Another process is using {self.path.name.removeprefix('.').removesuffix('.lock')}") from exc
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        return self

    def __exit__(self, *exc):
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def sync_file(path: Path) -> None:
    # Windows _commit requires a writable descriptor, even after a PDF writer
    # has already closed the file.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_bytes(path, json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8"))
