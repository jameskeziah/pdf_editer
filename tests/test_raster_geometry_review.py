from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw

from pdf_branding.config import load_profile
from pdf_branding.analysis import analyze_document
from pdf_branding.models import DocumentMeta, DocumentPlan, DocumentProfile, DocumentType, HeaderTemplate, PagePlan, PageStrategy, PageType, RectData, RenderStrategy, RepeatedBands
from pdf_branding.native_cleanup import make_image_transparent
from pdf_branding.renderer import render_document


ROOT = Path(__file__).resolve().parents[1]


def _logo_bytes() -> bytes:
    image = Image.new("RGB", (200, 60), (25, 45, 90))
    ImageDraw.Draw(image).text((10, 10), "ALLEN", font_size=35, fill="white")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _pixels(page: fitz.Page, region: fitz.Rect) -> bytes:
    return page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=region, colorspace=fitz.csRGB, alpha=False).samples


def test_shared_header_and_body_image_on_same_page_is_not_globally_erased():
    with fitz.open() as document:
        page = document.new_page()
        header, body = fitz.Rect(20, 20, 120, 50), fitz.Rect(90, 300, 290, 360)
        xref = page.insert_image(header, stream=_logo_bytes())
        page.insert_image(body, xref=xref)
        before_header, before_body = _pixels(page, header), _pixels(page, body)
        assert not make_image_transparent(document, xref, {0: [header]})
        assert _pixels(page, header) == before_header
        assert _pixels(page, body) == before_body


def test_shared_image_on_another_academic_page_is_not_globally_erased():
    with fitz.open() as document:
        header, body = fitz.Rect(20, 20, 120, 50), fitz.Rect(90, 300, 290, 360)
        first = document.new_page()
        xref = first.insert_image(header, stream=_logo_bytes())
        document.new_page().insert_image(body, xref=xref)
        before = _pixels(document[1], body)
        assert not make_image_transparent(document, xref, {0: [header]})
        assert _pixels(document[1], body) == before


def test_global_neutralization_requires_explicit_scope_and_accepts_only_authorized_uses():
    with fitz.open() as document:
        header = fitz.Rect(20, 20, 120, 50)
        first = document.new_page()
        xref = first.insert_image(header, stream=_logo_bytes())
        document.new_page().insert_image(header, xref=xref)
        assert not make_image_transparent(document, xref)
        assert make_image_transparent(document, xref, {0: [header], 1: [header]})
        assert set(_pixels(document[0], header)) == {255}
        assert set(_pixels(document[1], header)) == {255}


def test_image_reused_by_annotation_appearance_is_preserved():
    with fitz.open() as document:
        page = document.new_page()
        header, body = fitz.Rect(20, 20, 120, 50), fitz.Rect(90, 300, 290, 360)
        xref = page.insert_image(header, stream=_logo_bytes())
        annotation = page.add_freetext_annot(body, "Academic image")
        _, normal = document.xref_get_key(annotation.xref, "AP/N")
        appearance_xref = int(normal.split()[0])
        document.xref_set_key(appearance_xref, "Resources", f"<< /XObject << /Figure {xref} 0 R >> >>")
        document.xref_set_key(appearance_xref, "BBox", "[0 0 200 60]")
        document.update_stream(appearance_xref, b"q 200 0 0 60 0 0 cm /Figure Do Q")
        before = _pixels(page, body)
        assert not make_image_transparent(document, xref, {0: [header]})
        assert _pixels(page, body) == before


def test_renderer_preserves_unsafe_shared_placements_and_flags_qa_hold():
    with fitz.open() as source:
        page = source.new_page()
        header, body = fitz.Rect(20, 20, 120, 50), fitz.Rect(90, 300, 290, 360)
        xref = page.insert_image(header, stream=_logo_bytes())
        page.insert_image(body, xref=xref)
        digest = page.get_image_info(xrefs=True)[0]["digest"].hex()
        brand = load_profile(ROOT / "profiles" / "sskem.json")
        brand.raw = deepcopy(brand.raw)
        brand.raw["legacy"]["raster_logo_digests"] = [digest]
        region = RectData(16, 16, 124, 54)
        pp = PagePlan(1, PageType.DESIGNED, RenderStrategy.REPLACE_LOGO_ONLY,
                      PageStrategy.DESIGNED_PAGE_LOGO_ONLY, HeaderTemplate.NONE, .98,
                      logo_replace_rect=region, logo_background=(1, 1, 1),
                      legacy_logo_rects=[region], legacy_image_digests=[digest],
                      protected_visual_rects=[RectData.from_rect(body)])
        plan = DocumentPlan(Path("fixture.pdf"), DocumentMeta("6", "physics", "Motion", None, "KEY POINTS", 1),
                            DocumentProfile(Path("fixture.pdf"), DocumentType.KEY_POINTS, 1), RepeatedBands(), [pp])
        before_header, before_body = _pixels(page, header), _pixels(page, body)
        with render_document(source, plan, brand) as output:
            assert _pixels(output[0], header) == before_header
            assert _pixels(output[0], body) == before_body
        assert pp.unsafe_image_cleanup


def test_repeated_corner_academic_image_is_not_authorized_by_position_alone():
    artwork = Image.new("RGB", (120, 60), "white")
    drawing = ImageDraw.Draw(artwork)
    drawing.line((10, 50, 10, 5), fill="black", width=2)
    drawing.line((10, 50, 110, 50), fill="black", width=2)
    drawing.line((10, 40, 60, 15, 110, 30), fill="blue", width=3)
    stream = BytesIO()
    artwork.save(stream, format="PNG")
    with fitz.open() as document:
        region = fitz.Rect(20, 10, 80, 40)
        xref = 0
        for number in range(2):
            page = document.new_page(width=595, height=842)
            if xref:
                page.insert_image(region, xref=xref)
            else:
                xref = page.insert_image(region, stream=stream.getvalue())
            page.insert_text((100, 35), "NCERT Basics : Class 6", fontsize=9)
            page.insert_text((60, 150), f"Question {number + 1}: explain the graph")
        brand = load_profile(ROOT / "profiles" / "sskem.json")
        brand.raw = deepcopy(brand.raw)
        brand.raw["assets"].pop("ocr_eng", None)
        brand.raw["legacy"]["raster_logo_digests"] = []
        meta = DocumentMeta("6", "physics", "Measurement and Motion", None, "NOTES", 2)
        repeated, analyses = analyze_document(document, meta, brand)
        assert repeated.header_confidence >= .65
        for page in analyses:
            assert not page.legacy_image_digests
            assert any(fitz.Rect(*protected.as_tuple()).contains(region) for protected in page.protected_visual_rects)
