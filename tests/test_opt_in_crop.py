from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pymupdf as fitz

from pdf_branding.config import BrandingProfile, load_profile
from pdf_branding.models import RectData, RenderStrategy
from pdf_branding.qa import run_qa
from pdf_branding.renderer import render_document
from test_geometry_safety import PROFILE, _header_and_footer, _image_stream, _plan


def _brand(allow_crop: bool = True):
    original = load_profile(PROFILE)
    raw = deepcopy(original.raw)
    raw["layout"].update(allow_rebuild_crop=allow_crop, header_height=76)
    return BrandingProfile(original.source_path, raw)


def _source():
    document = fitz.open()
    for number in range(3):
        page = document.new_page(width=595, height=842)
        _header_and_footer(page, number + 1)
        page.insert_text((40, 125), f"{number + 1}. Measure the object shown in the diagram.", fontsize=12)
        page.insert_image((180, 180, 350, 290), stream=_image_stream())
        page.insert_text((40, 330), "The academic diagram and sentence must survive.", fontsize=12)
        page.insert_text((15, 650), "PNCF LIVE Module SET-1", fontsize=6, rotate=90)
    return document


def test_crop_is_disabled_by_default():
    brand = load_profile(PROFILE)
    assert brand.layout("allow_rebuild_crop") is False
    with _source() as source:
        _, plan = _plan(source, brand)
        assert all(page.strategy != RenderStrategy.REBUILD_CROP for page in plan.pages)
        assert all(page.source_to_output_matrix is None for page in plan.pages)


def test_opt_in_crop_maps_words_images_and_side_redactions(tmp_path: Path):
    brand = _brand()
    with _source() as source:
        _, plan = _plan(source, brand)
        assert all(page.strategy == RenderStrategy.REBUILD_CROP for page in plan.pages)
        assert all(page.source_to_output_matrix[0] < 1 for page in plan.pages)
        path = tmp_path / "cropped.pdf"
        with render_document(source, plan, brand) as output:
            output.save(path)
        with fitz.open(path) as output:
            for number, page in enumerate(output):
                assert not page.search_for("PNCF")
                assert not page.search_for("ALLEN")
                assert page.search_for("academic diagram")
                original = source[number].search_for("academic diagram")[0]
                expected = fitz.Rect(*plan.pages[number].map_source_rect(RectData.from_rect(original)).as_tuple())
                actual = page.search_for("academic diagram")[0]
                assert max(abs(a - b) for a, b in zip(expected, actual)) < .01
        report = run_qa(source, path, plan, brand)
    assert report.passed, report.issues
    assert report.metrics["average_body_text_retention"] == 1
    assert report.metrics["max_body_position_shift_pt"] < .01
    assert report.metrics["max_body_render_change_ratio"] == 0


def test_crop_checks_images_before_text_and_avoids_cutting_them():
    brand = _brand()
    with _source() as source:
        for page in source:
            page.insert_image((180, 40, 350, 95), stream=_image_stream())
        _, plan = _plan(source, brand)
        assert all(page.strategy != RenderStrategy.REBUILD_CROP for page in plan.pages)
        assert all(page.header_rect is None for page in plan.pages)


def test_invalid_crop_that_clips_diagram_fails_qa(tmp_path: Path):
    brand = _brand()
    with _source() as source:
        _, plan = _plan(source, brand)
        path = tmp_path / "crop-diagram-loss.pdf"
        with render_document(source, plan, brand) as output:
            output.save(path)
        # A tampered plan must be rejected even if its body-word counter happens
        # to omit the clipped source area or the actual output still looks intact.
        plan.pages[0].source_clip_rect.y0 = 300
        report = run_qa(source, path, plan, brand)
    assert not report.passed
    assert any(issue.code == "CROP_CONTENT_CLIPPED" and issue.page_number == 1 for issue in report.issues)
