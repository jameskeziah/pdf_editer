from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__


class BatchCache:
    def __init__(self, output_root: Path):
        self.path = output_root / ".pdf_branding_cache.json"
        self.data: dict[str, Any] = {"version": 2, "items": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception:
                pass
        self.data.setdefault("items", {})

    @staticmethod
    def sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def profile_hash(profile_raw: dict) -> str:
        blob = json.dumps(profile_raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def key(self, relative_path: Path) -> str:
        return relative_path.as_posix().lower()

    def is_fresh(self, relative_path: Path, source: Path, output: Path, profile_hash: str) -> bool:
        if not output.exists():
            return False
        item = self.data["items"].get(self.key(relative_path))
        if not item or not item.get("qa_passed"):
            return False
        stat = source.stat()
        if item.get("source_size") != stat.st_size or item.get("source_mtime_ns") != stat.st_mtime_ns:
            return False
        if item.get("profile_hash") != profile_hash or item.get("engine_version") != __version__:
            return False
        # Detect a manually changed/corrupt cached output cheaply by size; full output hash is stored for audits.
        if item.get("output_size") != output.stat().st_size:
            return False
        return True

    def update(self, relative_path: Path, source: Path, output: Path, profile_hash: str, qa_passed: bool) -> None:
        stat = source.stat()
        self.data["items"][self.key(relative_path)] = {
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_sha256": self.sha256(source),
            "profile_hash": profile_hash,
            "engine_version": __version__,
            "output_size": output.stat().st_size if output.exists() else 0,
            "output_sha256": self.sha256(output) if output.exists() else None,
            "qa_passed": bool(qa_passed),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
