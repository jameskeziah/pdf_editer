from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw

from pdf_branding.analysis import analyze_document
from pdf_branding.config import load_profile
from pdf_branding.models import DocumentMeta, DocumentProfile, DocumentType, RectData, RenderStrategy
from pdf_branding.planner import make_document_plan
from pdf_branding.qa import _geometry_issues
from pdf_branding.renderer import render_document


PROFILE = Path(__file__).parents[1] / "profiles" / "sskem.json"


def _image_stream() -> bytes:
    image = Image.new("RGB", (100, 60), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 5, 90, 55), fill="blue")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _plan(document: fitz.Document, brand, material: str = "EXERCISE"):
    source = Path("fixture.pdf")
    meta = DocumentMeta("6", "physics", "Measurement and Motion", "07", material, len(document))
    repeated, analyses = analyze_document(document, meta, brand)
    plan = make_document_plan(source, meta, DocumentProfile(source, DocumentType.EXERCISE, 1), repeated, analyses, brand)
    return analyses, plan


def _header_and_footer(page: fitz.Page, number: int):
    page.draw_rect((30, 28, 470, 55), fill=(.8, .87, 1), color=None)
    page.insert_text((38, 48), "NCERT Basics : Class 6", fontsize=11)
    page.insert_text((485, 49), "ALLEN", fontsize=18, fontname="hebo")
    page.draw_line((30, 60), (565, 60), width=1)
    page.draw_rect((30, 798, 565, 820), fill=(.8, .87, 1), color=None)
    page.insert_text((530, 814), f"[{number}]", fontsize=8)


@pytest.mark.parametrize("visual", ["image", "drawing"])
def test_header_geometry_protects_academic_visual_before_text(visual: str):
    brand = load_profile(PROFILE)
    with fitz.open() as document:
        for number in range(3):
            page = document.new_page(width=595, height=842)
            _header_and_footer(page, number + 1)
            if visual == "image":
                page.insert_image((180, 35, 280, 85), stream=_image_stream())
            else:
                page.draw_rect((180, 35, 280, 85), fill=(0, .7, 0), color=None)
            page.insert_text((40, 125), f"Academic question {number + 1}", fontsize=12)
        analyses, plan = _plan(document, brand)
        assert all(a.protected_visual_rects and a.first_content_y < 40 for a in analyses)
        assert all(p.header_rect is None for p in plan.pages)
        assert all(not _geometry_issues(p, p.page_number) for p in plan.pages)


def test_footer_geometry_protects_image_beyond_repeated_footer():
    brand = load_profile(PROFILE)
    with fitz.open() as document:
        for number in range(3):
            page = document.new_page(width=595, height=842)
            _header_and_footer(page, number + 1)
            page.insert_text((40, 125), f"Academic question {number + 1}", fontsize=12)
            page.insert_image((180, 805, 280, 837), stream=_image_stream())
        analyses, plan = _plan(document, brand)
        assert all(a.last_content_y > 825 for a in analyses)
        assert all(p.footer_rect is None for p in plan.pages)
        assert all(p.recommended_crop_bottom < 18 for p in plan.pages)


def test_collision_detection_uses_geometry_without_reason_strings():
    brand = load_profile(PROFILE)
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40, 100), "Academic content", fontsize=12)
        _, plan = _plan(document, brand)
        planned = plan.pages[0]
        planned.reasons = []
        planned.header_rect = RectData(0, 0, page.rect.width, 110)
        issues = _geometry_issues(planned, 1)
        assert any(issue.code == "HEADER_BODY_COLLISION_PLAN" for issue in issues)


def test_analysis_records_extraction_errors(monkeypatch):
    brand = load_profile(PROFILE)
    with fitz.open() as document:
        page = document.new_page()
        page.draw_rect((50, 80, 150, 140), fill=(0, 0, 1))
        original = fitz.Page.get_text
        def fail_dict(self, option="text", *args, **kwargs):
            if option == "dict":
                raise RuntimeError("broken text extraction")
            return original(self, option, *args, **kwargs)
        monkeypatch.setattr(fitz.Page, "get_text", fail_dict)
        analyses, plan = _plan(document, brand)
        assert not analyses[0].text_extraction_ok
        assert not plan.pages[0].text_extraction_ok
        assert plan.pages[0].header_rect is None


def test_blank_page_is_preserved_without_empty_import_error():
    brand = load_profile(PROFILE)
    with fitz.open() as document:
        document.new_page(width=400, height=600)
        _, plan = _plan(document, brand)
        with render_document(document, plan, brand) as rendered:
            assert len(rendered) == 1
            assert rendered[0].rect == document[0].rect
            assert not rendered[0].get_text()
            assert plan.pages[0].strategy == RenderStrategy.PRESERVE
