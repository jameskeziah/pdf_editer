from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageChops

from pdf_branding.native_cleanup import image_background_pixmap, remove_region_text
from pdf_branding.renderer import _apply_redactions
from pdf_branding.models import RectData


def _pixels(page):
    pixmap = page.get_pixmap(colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def test_margin_text_cleanup_keeps_implicit_graphics_state_and_diagram_pixels(tmp_path: Path):
    source = fitz.open()
    page = source.new_page()
    page.insert_text((16, 700), "PNCF legacy module", rotate=90, fontsize=7)
    page.insert_text((60, 100), "Academic body and table")
    # A thin stroke, filled diagram and page frame share inherited PDF state.
    page.draw_line((190, 350), (300, 350), width=.25)
    page.draw_rect((90, 180, 260, 240), fill=(.1, .3, .8), width=.35)
    page.draw_line((24, 30), (24, 810), width=2.25)
    before = _pixels(page)
    output = fitz.open(stream=source.tobytes(), filetype="pdf")
    _apply_redactions(output[0], [RectData(8, 500, 25, 710)], (1, 1, 1))
    assert not output[0].search_for("PNCF")
    assert output[0].search_for("Academic body and table")
    assert ImageChops.difference(before.crop((40, 60, 570, 480)), _pixels(output[0]).crop((40, 60, 570, 480))).getbbox() is None
    output.save(tmp_path / "result.pdf", clean=False)
    with fitz.open(tmp_path / "result.pdf") as reopened:
        assert ImageChops.difference(_pixels(output[0]), _pixels(reopened[0])).getbbox() is None
    output.close()
    source.close()


def test_native_cleanup_declines_partial_object_instead_of_deleting_academic_text():
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 100), "Legacy name followed by academic body content")
        assert not remove_region_text(page, [fitz.Rect(45, 80, 110, 110)])
        assert page.search_for("academic body content")


def test_text_clip_is_kept_and_blank_region_needs_no_rewrite():
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 100), "Academic text")
        original = page.read_contents()
        assert remove_region_text(page, [fitz.Rect(0, 700, 500, 750)])
        assert page.read_contents() == original


def test_background_reference_preserves_all_original_shared_image_placements():
    with fitz.open() as document:
        page = document.new_page()
        region = fitz.Rect(20, 20, 120, 50)
        image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 10))
        image.clear_with(0)
        xref = page.insert_image(region, pixmap=image)
        document.new_page().insert_image(fitz.Rect(90, 300, 290, 360), xref=xref)
        before = [document[index].get_pixmap().samples for index in range(len(document))]
        reference = image_background_pixmap(document[0], xref, region, 2)
        assert set(reference.samples) == {255}
        assert [document[index].get_pixmap().samples for index in range(len(document))] == before
