from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__
from .atomic import FileLock, atomic_write_json
from .config import BrandingProfile


class BatchCache:
    def __init__(self, output_root: Path):
        self.path = output_root / ".pdf_branding_cache.json"
        self.data: dict[str, Any] = {"version": 3, "items": {}}
        self._dirty: set[str] = set()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
                self.data["items"] = {
                    key: value for key, value in loaded["items"].items()
                    if isinstance(key, str) and isinstance(value, dict)
                }
        except (OSError, ValueError):
            pass

    @staticmethod
    def sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def profile_hash(profile_raw: BrandingProfile | dict, source_path: Path | None = None) -> str:
        """Hash settings and assets; pass a profile to resolve relative asset paths.

        The original dictionary-only API remains valid. Its optional source_path
        supplies the profile directory; a BrandingProfile supplies it automatically.
        """
        if isinstance(profile_raw, BrandingProfile):
            raw = profile_raw.raw
            source_path = profile_raw.source_path
        else:
            raw = profile_raw
        h = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        assets = raw.get("assets", {}) if isinstance(raw, dict) else {}
        for name, value in sorted(assets.items()):
            if not isinstance(value, str) or not value:
                continue
            asset = Path(value)
            if not asset.is_absolute():
                if source_path is None:
                    continue  # A legacy raw-only call has no profile-relative directory.
                asset = source_path.parent / asset
            h.update(name.encode("utf-8"))
            h.update(bytes.fromhex(BatchCache.sha256(asset.resolve())))
        return h.hexdigest()

    def key(self, relative_path: Path) -> str:
        return relative_path.as_posix().lower()

    def is_fresh(self, relative_path: Path, source: Path, output: Path, profile_hash: str) -> bool:
        if output.with_name(f".{output.name}.publication.json").exists():
            return False  # Let process_pdf recover before a cached skip.
        item = self.data["items"].get(self.key(relative_path))
        if not isinstance(item, dict) or item.get("qa_passed") is not True:
            return False
        if item.get("profile_hash") != profile_hash or item.get("engine_version") != __version__:
            return False
        try:
            source_stat, output_stat = source.stat(), output.stat()
            if (item.get("source_size") != source_stat.st_size
                    or item.get("source_mtime_ns") != source_stat.st_mtime_ns
                    or item.get("output_size") != output_stat.st_size):
                return False
            # Hash both files: sizes and timestamps cannot detect same-size corruption.
            return (item.get("source_sha256") == self.sha256(source)
                    and item.get("output_sha256") == self.sha256(output))
        except OSError:
            return False

    def update(self, relative_path: Path, source: Path, output: Path, profile_hash: str, qa_passed: bool) -> None:
        stat = source.stat()
        key = self.key(relative_path)
        self.data["items"][key] = {
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_sha256": self.sha256(source),
            "profile_hash": profile_hash,
            "engine_version": __version__,
            "output_size": output.stat().st_size if output.exists() else 0,
            "output_sha256": self.sha256(output) if output.exists() else None,
            "qa_passed": bool(qa_passed),
        }
        self._dirty.add(key)

    def save(self) -> None:
        with FileLock(self.path):
            items = {}
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
                    items = loaded["items"]
            except (OSError, ValueError):
                pass
            keys = self._dirty if self._dirty else self.data["items"].keys()
            items.update({key: self.data["items"][key] for key in keys})
            self.data = {"version": 3, "items": items}
            atomic_write_json(self.path, self.data)
            self._dirty.clear()
