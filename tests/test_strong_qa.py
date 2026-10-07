from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw

from pdf_branding.config import load_profile
from pdf_branding.models import (
    DocumentMeta, DocumentPlan, DocumentProfile, DocumentType, HeaderTemplate,
    PagePlan, PageStrategy, PageType, RectData, RenderStrategy, RepeatedBands,
)
from pdf_branding.qa import run_qa
from pdf_branding.renderer import render_document


PROFILE = Path(__file__).parents[1] / "profiles" / "sskem.json"


def _plan(page_plan: PagePlan) -> DocumentPlan:
    source = Path("source.pdf")
    return DocumentPlan(source, DocumentMeta("6", "physics", "Measurement and Motion", "07", "NOTES", 1),
                        DocumentProfile(source, DocumentType.NOTES, 1), RepeatedBands(), [page_plan])


def _page(**kwargs) -> PagePlan:
    return PagePlan(1, PageType.STANDARD, RenderStrategy.PRESERVE,
                    PageStrategy.LEAVE_UNTOUCHED, HeaderTemplate.NONE, 1, **kwargs)


def _stream(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _copy(source: fitz.Document):
    output = fitz.open()
    output.insert_pdf(source)
    return output


@pytest.mark.parametrize("damage", [False, True])
def test_scan_has_visual_proof_and_no_fabricated_text_retention(tmp_path: Path, damage: bool):
    brand = load_profile(PROFILE)
    image = Image.new("RGB", (595, 842), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 100), "A scanned lesson with a diagram", fill="black")
    draw.ellipse((180, 180, 310, 270), fill="blue")
    with fitz.open() as source:
        original = source.new_page(width=595, height=842)
        original.insert_image(original.rect, stream=_stream(image))
        planned = _page(image_only=True, protected_visual_rects=[RectData(0, 0, 595, 842)])
        path = tmp_path / "scan.pdf"
        with _copy(source) as output:
            if damage:
                output[0].add_redact_annot((175, 175, 315, 275), fill=(1, 1, 1))
                output[0].apply_redactions()
            output.save(path)
        report = run_qa(source, path, _plan(planned), brand)
    assert report.metrics["average_body_text_retention"] is None
    assert report.metrics["text_retention_pages_verified"] == 0
    assert report.metrics["visual_only_pages_verified"] == 1
    assert report.passed is not damage
    if damage:
        assert any(issue.code == "DIAGRAM_RENDER_CHANGED" for issue in report.issues)


def test_tiny_diagram_loss_is_not_diluted_by_whole_page(tmp_path: Path):
    brand = load_profile(PROFILE)
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.insert_text((40, 80), "Academic body text stays intact.")
        page.draw_rect((50, 100, 58, 108), fill=(0, .7, 0), color=None)
        planned = _page(protected_visual_rects=[RectData(49, 99, 59, 109)])
        path = tmp_path / "diagram.pdf"
        with _copy(source) as output:
            output[0].add_redact_annot((49, 99, 59, 109), fill=(1, 1, 1))
            output[0].apply_redactions()
            output.save(path)
        report = run_qa(source, path, _plan(planned), brand)
    assert not report.passed
    assert report.metrics["average_body_text_retention"] == 1
    assert report.metrics["max_body_render_change_ratio"] < .0005
    assert any(issue.code == "DIAGRAM_RENDER_CHANGED" for issue in report.issues)


@pytest.mark.parametrize("shift_x,shift_y", [(8, 0), (0, 8)])
def test_positions_check_both_axes(tmp_path: Path, shift_x: int, shift_y: int):
    brand = load_profile(PROFILE)
    with fitz.open() as source, fitz.open() as output:
        source.new_page(width=595, height=842).insert_text((40, 100), "Academic word positions stay fixed.")
        output.new_page(width=595, height=842).insert_text((40 + shift_x, 100 + shift_y), "Academic word positions stay fixed.")
        path = tmp_path / "moved.pdf"
        output.save(path)
        report = run_qa(source, path, _plan(_page()), brand)
    assert not report.passed
    assert report.metrics["average_body_text_retention"] == 1
    assert any(issue.code == "BODY_POSITION_SHIFT" for issue in report.issues)


def test_failed_source_word_extraction_is_error(tmp_path: Path, monkeypatch):
    brand = load_profile(PROFILE)
    with fitz.open() as source:
        source.new_page().insert_text((40, 100), "Academic content cannot be silently omitted.")
        path = tmp_path / "extraction.pdf"
        with _copy(source) as output:
            output.save(path)
        original = fitz.Page.get_text
        def fail_source_words(self, option="text", *args, **kwargs):
            if self.parent is source and option == "words":
                raise RuntimeError("word extraction failed")
            return original(self, option, *args, **kwargs)
        monkeypatch.setattr(fitz.Page, "get_text", fail_source_words)
        report = run_qa(source, path, _plan(_page()), brand)
    assert not report.passed
    assert report.metrics["average_body_text_retention"] is None
    assert any(issue.code == "TEXT_EXTRACTION_FAILED" for issue in report.issues)


@pytest.mark.parametrize("repair", [False, True])
def test_independent_raster_logo_template_accepts_repair_and_rejects_survivor(tmp_path: Path, repair: bool):
    brand = load_profile(PROFILE)
    bg8 = (70, 50, 26)
    bg = tuple(value / 255 for value in bg8)
    bitmap = Image.new("RGB", (208, 124), bg8)
    draw = ImageDraw.Draw(bitmap)
    # Non-searchable source glyph strokes on both sides of the new emblem.
    draw.rectangle((12, 28, 35, 83), fill=(240, 240, 240))
    draw.rectangle((173, 28, 195, 83), fill=(240, 240, 240))
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.draw_rect(page.rect, fill=bg, color=None)
        page.insert_image((3, 7, 107, 69), stream=_stream(bitmap))
        planned = PagePlan(1, PageType.DESIGNED, RenderStrategy.REPLACE_LOGO_ONLY,
                           PageStrategy.DESIGNED_PAGE_LOGO_ONLY, HeaderTemplate.NONE, 1,
                           logo_replace_rect=RectData(3, 7, 107, 69), logo_background=bg)
        plan = _plan(planned)
        path = tmp_path / "logo.pdf"
        with render_document(source, plan, brand) if repair else _copy(source) as output:
            output.save(path)
        report = run_qa(source, path, plan, brand)
    assert report.metrics["raster_logo_template_checks"] == 1
    assert report.passed is repair, report.issues
    assert any(issue.code == "RASTER_LEGACY_LOGO_REMAINS" for issue in report.issues) is not repair


def test_render_failure_cannot_certify_image_only_page(tmp_path: Path, monkeypatch):
    brand = load_profile(PROFILE)
    with fitz.open() as source:
        source.new_page().draw_rect((50, 100, 100, 150), fill=(0, 0, 1))
        path = tmp_path / "render-error.pdf"
        with _copy(source) as output:
            output.save(path)
        def fail_render(*args, **kwargs):
            raise RuntimeError("cannot render this page")
        monkeypatch.setattr(fitz.Page, "get_pixmap", fail_render)
        report = run_qa(source, path, _plan(_page(image_only=True)), brand)
    assert not report.passed
    assert any(issue.code == "RENDER_VERIFICATION_FAILED" for issue in report.issues)
