from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pdf_branding.config import load_profile


ROOT = Path(__file__).parents[1]


@pytest.fixture
def valid_raw():
    raw = json.loads((ROOT / "profiles/sskem.json").read_text(encoding="utf-8"))
    raw["assets"] = {name: str((ROOT / "profiles" / path).resolve()) for name, path in raw["assets"].items()}
    return raw


@pytest.mark.parametrize("section,key,value", [
    ("layout", "header_height", "54"),
    ("layout", "safety_gap", -1),
    ("layout", "dark_page_threshold", 1.1),
    ("layout", "allow_rebuild_crop", "false"),
    ("layout", "header_adaptive_max_height", 20),
    ("qa", "min_body_text_retention", 1.01),
    ("qa", "legacy_text_is_error", 1),
    ("legacy", "terms", "ALLEN"),
    ("legacy", "designed_logo_fallback_ratio", [0.2, 0.1, 0.1, 0.5]),
    ("legacy", "first_page_cleanup_targets", []),
    ("colors", "navy", "#zz0000"),
    ("school", "short_name", " "),
    ("assets", "logo", None),
    ("assets", "ocr_eng", None),
])
def test_invalid_profile_values_fail_before_processing(tmp_path: Path, valid_raw, section, key, value):
    raw = copy.deepcopy(valid_raw)
    raw[section][key] = value
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=section + "|Header"):
        load_profile(path)


@pytest.mark.parametrize("data", [[], None, {"school": [], "assets": {}, "colors": {}}])
def test_profile_root_and_sections_are_objects(tmp_path: Path, data):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_profile(path)


def test_missing_or_corrupt_optional_white_logo_is_not_silently_ignored(tmp_path: Path, valid_raw):
    logo = tmp_path / "white.png"
    valid_raw["assets"]["logo_white"] = str(logo)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(valid_raw), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="logo_white"):
        load_profile(path)
    logo.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="logo_white"):
        load_profile(path)


def test_nonfinite_json_numbers_are_rejected(tmp_path: Path, valid_raw):
    valid_raw["qa"]["page_size_tolerance_pt"] = float("nan")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(valid_raw), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON number"):
        load_profile(path)


def test_ocr_model_is_validated_before_processing(tmp_path: Path, valid_raw):
    model = tmp_path / "eng.traineddata"
    valid_raw["assets"]["ocr_eng"] = str(model)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(valid_raw), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="OCR language model"):
        load_profile(path)
    model.write_bytes(b"not a traineddata file")
    with pytest.raises(ValueError, match="ocr_eng"):
        load_profile(path)
