from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from .atomic import atomic_write_json, sync_file


class PublicationError(RuntimeError):
    def __init__(self, message: str, *, recovery_required: bool = False):
        super().__init__(message)
        self.recovery_required = recovery_required


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def journal_path(anchor: Path) -> Path:
    return anchor.with_name(f".{anchor.name}.publication.json")


def _owned_path(target: Path, token: str, kind: str) -> Path:
    return target.with_name(f".{target.name}.{token}.{kind}{target.suffix}")


def _load_journal(anchor: Path, allowed_targets: list[Path]) -> dict:
    path = journal_path(anchor)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("unsupported journal structure/version")
        token = data.get("transaction_id")
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
            raise ValueError("invalid transaction identifier")
        if data.get("state") not in ("prepared", "committed") or _key(Path(data["anchor"])) != _key(anchor):
            raise ValueError("journal state/anchor mismatch")
        entries = data.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValueError("journal entries must be a nonempty list")
        allowed, seen = {_key(target) for target in allowed_targets}, set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid journal entry")
            target = Path(entry["target"])
            target_key = _key(target)
            if target_key not in allowed or target_key in seen:
                raise ValueError("journal target is unexpected or duplicated")
            seen.add(target_key)
            for kind in ("stage", "backup"):
                if _key(Path(entry[kind])) != _key(_owned_path(target, token, kind)):
                    raise ValueError("journal temporary path does not belong to its target")
            if type(entry.get("had_previous")) is not bool:
                raise ValueError("invalid previous-file flag")
            hashes = [entry.get("staged_sha256")]
            if entry["had_previous"]:
                hashes.append(entry.get("previous_sha256"))
            elif entry.get("previous_sha256") is not None:
                raise ValueError("unexpected previous-file hash")
            if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
                raise ValueError("invalid journal content hash")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PublicationError(f"Cannot safely recover publication journal {path}: {exc}", recovery_required=True) from exc


def _rollback(entries: list[dict]) -> None:
    errors = []
    for entry in reversed(entries):
        target, backup = Path(entry["target"]), Path(entry["backup"])
        try:
            current = _hash(target) if target.exists() else None
            previous, staged = entry["previous_sha256"], entry["staged_sha256"]
            # An external edit after interruption must never be overwritten.
            if current not in (None, previous, staged):
                raise ValueError("target changed outside this transaction; preserved for inspection")
            if entry["had_previous"]:
                if current == previous:
                    continue  # Already rolled back; recovery is idempotent.
                if not backup.is_file() or _hash(backup) != previous:
                    raise ValueError("verified previous-file backup is missing or changed")
                os.replace(backup, target)
            elif current == staged:
                target.unlink()
        except (OSError, ValueError) as exc:
            errors.append(f"{target}: {exc}")
    if errors:
        raise PublicationError("Publication rollback needs recovery: " + "; ".join(errors), recovery_required=True)


def _cleanup(data: dict, path: Path) -> None:
    # Remove the journal last. If cleanup is interrupted, its remaining owned
    # paths can be safely checked and removed by the next lock holder.
    for entry in data["entries"]:
        for kind in ("stage", "backup"):
            Path(entry[kind]).unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def recover_publication(anchor: Path, allowed_targets: list[Path]) -> str:
    """Recover one interrupted transaction; caller must hold the anchor OS lock."""
    path = journal_path(anchor)
    if not path.exists():
        return ""
    data = _load_journal(anchor, allowed_targets)
    if data["state"] == "prepared":
        _rollback(data["entries"])
        message = "Rolled back an interrupted PDF/report publication"
    else:
        for entry in data["entries"]:
            target = Path(entry["target"])
            if not target.is_file() or _hash(target) != entry["staged_sha256"]:
                raise PublicationError(f"Committed publication changed before cleanup: {target}", recovery_required=True)
        message = "Completed cleanup of a committed PDF/report publication"
    try:
        _cleanup(data, path)
    except OSError as exc:
        raise PublicationError(f"Publication files were recovered but cleanup failed: {exc}", recovery_required=True) from exc
    return message


class PublicationTransaction:
    """Stage every artifact, then journal and replace reports before the PDF.

    Each stage/backup is unique and beside its target, so replacement stays on the
    same filesystem. A prepared journal always means rollback; a durable committed
    journal means finish cleanup. No successful result is returned before commit.
    """
    def __init__(self, anchor: Path, allowed_targets: list[Path]):
        self.anchor = anchor.resolve()
        self.allowed = {_key(path) for path in allowed_targets}
        self.token = uuid.uuid4().hex
        self.path = journal_path(self.anchor)
        self.stages: dict[Path, Path] = {}
        self.backups: list[Path] = []
        self.pending = False
        self.committed = False
        self.warning = ""

    def __enter__(self):
        return self

    def stage_path(self, target: Path) -> Path:
        target = target.resolve()
        if _key(target) not in self.allowed or target in self.stages:
            raise ValueError(f"Unexpected or duplicate publication target: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = _owned_path(target, self.token, "stage")
        with stage.open("xb"):
            pass
        self.stages[target] = stage
        return stage

    def retarget_stage(self, previous_target: Path, target: Path) -> Path:
        previous_target, target = previous_target.resolve(), target.resolve()
        if previous_target == target:
            return self.stages[target]
        if _key(target) not in self.allowed or target in self.stages:
            raise ValueError(f"Unexpected publication target: {target}")
        stage = self.stages[previous_target]
        replacement = _owned_path(target, self.token, "stage")
        os.replace(stage, replacement)
        del self.stages[previous_target]
        self.stages[target] = replacement
        return replacement

    def commit(self) -> None:
        if self.pending or self.committed or not self.stages:
            raise ValueError("Publication transaction cannot be committed in its current state")
        entries = []
        # Publish reports first; the final PDF is the last externally visible item.
        for target, stage in sorted(self.stages.items(), key=lambda pair: (pair[0].suffix.lower() == ".pdf", str(pair[0]))):
            sync_file(stage)
            backup = _owned_path(target, self.token, "backup")
            exists = target.exists()
            previous_hash = None
            if exists:
                with target.open("rb") as reader, backup.open("xb") as writer:
                    self.backups.append(backup)
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                previous_hash = _hash(backup)
                if previous_hash != _hash(target):
                    raise PublicationError(f"Publication target changed during backup: {target}")
            entries.append({
                "target": str(target), "stage": str(stage), "backup": str(backup),
                "had_previous": exists, "previous_sha256": previous_hash,
                "staged_sha256": _hash(stage),
            })
        data = {"version": 1, "transaction_id": self.token, "anchor": str(self.anchor), "state": "prepared", "entries": entries}
        atomic_write_json(self.path, data)
        self.pending = True
        try:
            for entry in entries:
                os.replace(entry["stage"], entry["target"])
            data["state"] = "committed"
            atomic_write_json(self.path, data)
        except Exception as exc:
            try:
                _rollback(entries)
                _cleanup(data, self.path)
                self.pending = False
            except Exception as recovery_exc:
                raise PublicationError(f"Publication failed: {exc}; {recovery_exc}", recovery_required=True) from exc
            raise PublicationError(f"Publication failed and previous files were restored: {exc}") from exc
        # A KeyboardInterrupt/SystemExit leaves the prepared journal and verified
        # backups intact for recovery when a later worker acquires the OS lock.
        self.pending = False
        self.committed = True
        try:
            _cleanup(data, self.path)
        except OSError as exc:
            self.warning = f"Publication committed; temporary cleanup will resume next run: {exc}"

    def __exit__(self, *exc):
        if self.pending:
            return
        # Pre-publication failures affect only private staging files.
        errors = []
        for path in list(self.stages.values()) + self.backups:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                errors.append(str(error))
        if errors and exc[0] is None and not self.committed:
            raise PublicationError("Staging cleanup failed: " + "; ".join(errors))
