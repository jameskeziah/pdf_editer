from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _hex_to_rgb01(value: str) -> tuple[float, float, float]:
    if not isinstance(value, str) or re.fullmatch(r"#?[0-9a-fA-F]{6}", value) is None:
        raise ValueError(f"Expected #RRGGBB colour, got {value!r}")
    value = value.lstrip("#")
    return tuple(int(value[i:i+2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


@dataclass(slots=True)
class BrandingProfile:
    source_path: Path
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw.get("id", self.source_path.stem))

    @property
    def school_short_name(self) -> str:
        return self.raw["school"]["short_name"]

    @property
    def school_line2(self) -> str:
        return self.raw["school"]["line2"]

    @property
    def logo_path(self) -> Path:
        p = Path(self.raw["assets"]["logo"])
        if not p.is_absolute():
            p = (self.source_path.parent / p).resolve()
        return p

    @property
    def white_logo_path(self) -> Path | None:
        raw = self.raw.get("assets", {}).get("logo_white")
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = (self.source_path.parent / p).resolve()
        return p

    def color(self, name: str) -> tuple[float, float, float]:
        return _hex_to_rgb01(self.raw["colors"][name])

    def layout(self, name: str, default: float | bool | int | None = None):
        return self.raw.get("layout", {}).get(name, default)

    @property
    def legacy_terms(self) -> tuple[str, ...]:
        return tuple(self.raw.get("legacy", {}).get("terms", ["ALLEN"]))

    @property
    def side_text_terms(self) -> tuple[str, ...]:
        return tuple(self.raw.get("legacy", {}).get("side_text_terms", []))

    @property
    def qa(self) -> dict[str, Any]:
        return self.raw.get("qa", {})


def _number(value: Any, label: str, minimum: float, maximum: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f"{label} must be a finite number from {minimum:g} to {maximum:g}")


def _string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{label} must be a nonempty string of at most 1000 characters")


def validate_profile(profile: BrandingProfile) -> None:
    raw = profile.raw
    if not isinstance(raw, dict):
        raise ValueError("Branding profile must be a JSON object")
    for name in ("school", "assets", "colors"):
        if not isinstance(raw.get(name), dict):
            raise ValueError(f"Profile {name} must be an object")
    for name in ("layout", "legacy", "qa"):
        if name in raw and not isinstance(raw[name], dict):
            raise ValueError(f"Profile {name} must be an object")
    if "id" in raw:
        _string(raw["id"], "id")
    for name in ("short_name", "line2"):
        _string(raw["school"].get(name), f"school.{name}")
    if "full_name" in raw["school"]:
        _string(raw["school"]["full_name"], "school.full_name")
    for name in ("navy", "blue", "red", "gray", "white"):
        if name not in raw["colors"]:
            raise ValueError(f"Missing colors.{name}")
    for name, value in raw["colors"].items():
        try:
            _hex_to_rgb01(value)
        except ValueError as exc:
            raise ValueError(f"Invalid colors.{name}: {exc}") from exc
    for name in ("logo", "logo_white"):
        if name == "logo" or name in raw["assets"]:
            _string(raw["assets"].get(name), f"assets.{name}")
    if "ocr_eng" in raw["assets"]:
        _string(raw["assets"]["ocr_eng"], "assets.ocr_eng")
        model = Path(raw["assets"]["ocr_eng"])
        if not model.is_absolute():
            model = profile.source_path.parent / model
        if model.name != "eng.traineddata":
            raise ValueError("assets.ocr_eng must name eng.traineddata")
        if not model.is_file():
            raise FileNotFoundError(f"OCR language model not found: {model}")
        # Tesseract traineddata starts with a component count and offset table.
        # Reject truncated/obviously invalid models before publishing any PDFs.
        import struct
        with model.open("rb") as stream:
            header = stream.read(4)
        count = struct.unpack("<I", header)[0] if len(header) == 4 else 0
        if not 1 <= count <= 256 or model.stat().st_size <= 4 + count * 8:
            raise ValueError(f"Invalid assets.ocr_eng language model: {model}")
    layout = raw.get("layout", {})
    ratios = {"dark_page_threshold", "designed_page_large_image_ratio", "designed_page_title_top_ratio"}
    for name, value in layout.items():
        if name == "allow_rebuild_crop":
            if type(value) is not bool:
                raise ValueError("layout.allow_rebuild_crop must be a boolean")
        else:
            _number(value, f"layout.{name}", 0, 1 if name in ratios else 1000)
    minimum = layout.get("header_min_height", 42)
    height = layout.get("header_height", 54)
    maximum = layout.get("header_adaptive_max_height", 76)
    if not 0 < minimum <= height <= maximum:
        raise ValueError("Header heights must satisfy 0 < header_min_height <= header_height <= header_adaptive_max_height")
    legacy = raw.get("legacy", {})
    if "raster_logo_digests" in legacy:
        values = legacy["raster_logo_digests"]
        if not isinstance(values, list) or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None for value in values):
            raise ValueError("legacy.raster_logo_digests must be a list of lowercase 32-character image digests")
    for name in ("terms", "side_text_terms"):
        if name not in legacy:
            continue
        values = legacy[name]
        if not isinstance(values, list) or (name == "terms" and not values):
            raise ValueError(f"legacy.{name} must be a list of nonempty strings")
        for value in values:
            _string(value, f"legacy.{name} item")
    if "allow_designed_fallback" in legacy and type(legacy["allow_designed_fallback"]) is not bool:
        raise ValueError("legacy.allow_designed_fallback must be a boolean")
    for name, value in legacy.items():
        if name.startswith("logo_"):
            _number(value, f"legacy.{name}", 0, 1000)
    for dimension in ("width", "height"):
        low, high = legacy.get(f"logo_min_{dimension}", 0), legacy.get(f"logo_max_{dimension}", 1000)
        if low > high:
            raise ValueError(f"legacy.logo_min_{dimension} must not exceed logo_max_{dimension}")
    if "designed_logo_fallback_ratio" in legacy:
        ratio = legacy["designed_logo_fallback_ratio"]
        if not isinstance(ratio, list) or len(ratio) != 4:
            raise ValueError("legacy.designed_logo_fallback_ratio must contain four numbers")
        for value in ratio:
            _number(value, "legacy.designed_logo_fallback_ratio item", 0, 1)
        if not ratio[0] < ratio[2] or not ratio[1] < ratio[3]:
            raise ValueError("legacy.designed_logo_fallback_ratio must describe a positive rectangle")
    if "first_page_cleanup_targets" in legacy:
        targets = legacy["first_page_cleanup_targets"]
        if not isinstance(targets, dict):
            raise ValueError("legacy.first_page_cleanup_targets must be an object")
        for name, value in targets.items():
            _string(name, "legacy.first_page_cleanup_targets key")
            _number(value, f"legacy.first_page_cleanup_targets.{name}", 0, 1000)
    qa = raw.get("qa", {})
    for name, value in qa.items():
        if name.endswith("_is_error"):
            if type(value) is not bool:
                raise ValueError(f"qa.{name} must be a boolean")
        else:
            _number(value, f"qa.{name}", 0, 1 if "retention" in name or "ratio" in name else 1000)
    # Decode assets now, before creating any output or starting a long batch.
    import pymupdf as fitz
    for name, asset in (("logo", profile.logo_path), ("logo_white", profile.white_logo_path)):
        if asset is None:
            continue
        if not asset.is_file():
            raise FileNotFoundError(f"Branding {name} not found: {asset}")
        try:
            pixmap = fitz.Pixmap(str(asset))
            if pixmap.width < 1 or pixmap.height < 1:
                raise ValueError("empty image")
        except Exception as exc:
            raise ValueError(f"Invalid branding {name} image {asset}: {exc}") from exc


def load_profile(path: str | Path) -> BrandingProfile:
    p = Path(path).resolve()
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid JSON number: {value}")))
    profile = BrandingProfile(source_path=p, raw=raw)
    validate_profile(profile)
    return profile
