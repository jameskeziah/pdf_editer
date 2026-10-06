from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _hex_to_rgb01(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB colour, got {value!r}")
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
        return p if p.exists() else None

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


def load_profile(path: str | Path) -> BrandingProfile:
    p = Path(path).resolve()
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    profile = BrandingProfile(source_path=p, raw=raw)
    if not profile.logo_path.exists():
        raise FileNotFoundError(f"Branding logo not found: {profile.logo_path}")
    return profile
