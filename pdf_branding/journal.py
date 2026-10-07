"""Durable, parent-owned batch checkpoints and cross-process run exclusion."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class RunLock:
    """An OS lock releases automatically after process termination."""
    def __init__(self, output_root: Path):
        self.path = output_root / ".batch_run.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.stream = self.path.open("a+b")
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"Another batch is using {self.path.parent}") from exc
        except BaseException:
            if self.stream is not None:
                try:
                    self.stream.close()
                finally:
                    self.stream = None
            raise
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                try:
                    self.stream.close()
                finally:
                    self.stream = None


class JobJournal:
    """SQLite commits each transition; JSON exports make the state reviewable."""
    def __init__(self, output_root: Path, input_root: Path, fingerprint: str, options: dict):
        self.output_root = output_root
        self.input_root = input_root
        self.fingerprint = fingerprint
        self.run_id = uuid.uuid4().hex
        self.options = options
        self.db = sqlite3.connect(output_root / ".batch_jobs.sqlite3", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                source_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                attempts INTEGER NOT NULL, record_json TEXT NOT NULL,
                run_id TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                config_json TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                source_id TEXT, status TEXT NOT NULL, event_json TEXT NOT NULL, at TEXT NOT NULL
            );
        """)
        self.db.commit()
        previous_run = self.db.execute("SELECT config_json FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if previous_run and Path(json.loads(previous_run[0])["input_root"]).resolve() != input_root.resolve():
            self.db.close()
            raise ValueError("This output manifest belongs to a different input library; choose a separate output folder")
        self.recover_interrupted()
        config = {"input_root": str(input_root), "output_root": str(output_root),
                  "fingerprint": fingerprint, "options": options}
        now = utc_now()
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?, 'running', ?, ?, ?)",
                            (self.run_id, json.dumps(config), now, now))
        self.snapshot("running")

    def key(self, source: str | Path) -> str:
        path = Path(source)
        try:
            return path.relative_to(self.input_root).as_posix().casefold()
        except ValueError:
            return str(path.resolve()).casefold()

    def get(self, source: str | Path) -> dict | None:
        row = self.db.execute("SELECT record_json FROM jobs WHERE source_id = ?", (self.key(source),)).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, record: dict, *, increment_attempt: bool = False) -> dict:
        key = self.key(record["source"])
        previous = self.get(record["source"]) or {}
        attempts = int(previous.get("attempts", 0)) + int(increment_attempt)
        merged = {**previous, **record, "source_id": key, "attempts": attempts,
                  "run_id": self.run_id, "updated_at": utc_now()}
        blob = json.dumps(merged, ensure_ascii=False)
        with self.db:
            self.db.execute("""INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET status=excluded.status,
                attempts=excluded.attempts, record_json=excluded.record_json,
                run_id=excluded.run_id, updated_at=excluded.updated_at""",
                (key, merged["status"], attempts, blob, self.run_id, merged["updated_at"]))
            self.db.execute("INSERT INTO events(run_id, source_id, status, event_json, at) VALUES (?, ?, ?, ?, ?)",
                            (self.run_id, key, merged["status"], blob, merged["updated_at"]))
        self.snapshot("running")
        return merged

    def recover_interrupted(self) -> None:
        rows = self.db.execute("SELECT source_id, record_json FROM jobs WHERE status='running'").fetchall()
        with self.db:
            for key, blob in rows:
                record = json.loads(blob)
                record.update(status="interrupted", message="Previous process stopped during this document.", updated_at=utc_now())
                self.db.execute("UPDATE jobs SET status='interrupted', record_json=?, updated_at=? WHERE source_id=?",
                                (json.dumps(record), record["updated_at"], key))
            self.db.execute("UPDATE runs SET status='interrupted', updated_at=? WHERE status='running'", (utc_now(),))

    def snapshot(self, status: str) -> dict:
        rows = self.db.execute("SELECT record_json FROM jobs ORDER BY source_id").fetchall()
        records = [json.loads(row[0]) for row in rows]
        manifest = {"schema_version": 1, "run_id": self.run_id, "status": status,
                    "input_root": str(self.input_root), "output_root": str(self.output_root),
                    "profile_fingerprint": self.fingerprint, "options": self.options,
                    "updated_at": utc_now(), "counts": dict(Counter(r["status"] for r in records)), "jobs": records}
        write_json(self.output_root / "batch_manifest.json", manifest)
        return manifest

    def finish(self, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE runs SET status=?, updated_at=? WHERE run_id=?", (status, utc_now(), self.run_id))
        self.snapshot(status)

    def close(self) -> None:
        self.db.close()
