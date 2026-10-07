from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pdf_branding.cache import BatchCache
from pdf_branding.config import BrandingProfile


def test_cache_checks_same_size_output_and_timestamp_preserved_source(tmp_path: Path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    source.write_bytes(b"source A")
    output.write_bytes(b"output A")
    cache = BatchCache(tmp_path)
    rel = Path("source.pdf")
    cache.update(rel, source, output, "profile", True)
    assert cache.is_fresh(rel, source, output, "profile")
    output.write_bytes(b"output B")
    assert not cache.is_fresh(rel, source, output, "profile")
    output.write_bytes(b"output A")
    stat = source.stat()
    source.write_bytes(b"source B")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert not cache.is_fresh(rel, source, output, "profile")


def test_profile_fingerprint_tracks_both_logo_contents(tmp_path: Path):
    (tmp_path / "logo.png").write_bytes(b"color A")
    (tmp_path / "white.png").write_bytes(b"white A")
    raw = {"assets": {"logo": "logo.png", "logo_white": "white.png"}}
    profile = BrandingProfile(tmp_path / "profile.json", raw)
    first = BatchCache.profile_hash(profile)
    assert first == BatchCache.profile_hash(raw, profile.source_path)
    (tmp_path / "white.png").write_bytes(b"white B")
    second = BatchCache.profile_hash(profile)
    assert first != second
    (tmp_path / "logo.png").write_bytes(b"color B")
    assert second != BatchCache.profile_hash(profile)
    assert isinstance(BatchCache.profile_hash(raw), str)  # Existing raw-only API.


def test_profile_fingerprint_tracks_ocr_model_contents(tmp_path: Path):
    model = tmp_path / "eng.traineddata"
    model.write_bytes(b"model A")
    profile = BrandingProfile(tmp_path / "profile.json", {"assets": {"ocr_eng": model.name}})
    first = BatchCache.profile_hash(profile)
    model.write_bytes(b"model B")
    assert first != BatchCache.profile_hash(profile)


@pytest.mark.parametrize("data", [[], {"items": []}, {"items": None}, {"items": {"source.pdf": None}}])
def test_malformed_cache_is_a_safe_miss(tmp_path: Path, data):
    (tmp_path / ".pdf_branding_cache.json").write_text(json.dumps(data), encoding="utf-8")
    cache = BatchCache(tmp_path)
    assert not cache.is_fresh(Path("source.pdf"), tmp_path / "missing", tmp_path / "missing2", "profile")


def test_cache_requires_publication_recovery_before_skip(tmp_path: Path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    cache = BatchCache(tmp_path)
    cache.update(Path("source.pdf"), source, output, "profile", True)
    output.with_name(f".{output.name}.publication.json").write_text("{}", encoding="utf-8")
    assert not cache.is_fresh(Path("source.pdf"), source, output, "profile")


def test_two_cache_writers_preserve_each_others_entries(tmp_path: Path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    first, second = BatchCache(tmp_path), BatchCache(tmp_path)
    first.update(Path("first.pdf"), source, output, "profile", True)
    second.update(Path("second.pdf"), source, output, "profile", True)
    first.save()
    second.save()
    assert set(BatchCache(tmp_path).data["items"]) == {"first.pdf", "second.pdf"}
