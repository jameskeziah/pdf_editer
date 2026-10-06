from __future__ import annotations

import pymupdf as fitz

from pdf_branding.analysis import sample_local_background
from pdf_branding.renderer import _repair_native_flat_background, _uniform_background_clip


def _pixel(page: fitz.Page, x: int, y: int) -> tuple[int, int, int]:
    pix = page.get_pixmap(alpha=False)
    offset = (y * pix.width + x) * pix.n
    return tuple(pix.samples[offset + channel] for channel in range(3))


def test_native_repair_preserves_cmyk_transparency_composition_and_text():
    source = fitz.open()
    original = source.new_page(width=595, height=842)
    state = source.get_new_xref()
    source.update_object(state, "<< /Type /ExtGState /ca .72 /CA .72 >>")
    source.xref_set_key(original.xref, "Group", "<< /S /Transparency /CS /DeviceCMYK >>")
    resources = int(source.xref_get_key(original.xref, "Resources")[1].split()[0])
    source.xref_set_key(resources, "ExtGState", f"<< /Darken {state} 0 R >>")
    content = source.get_new_xref()
    source.update_object(content, "<< >>")
    source.update_stream(content, (
        "0 .33 .72 0 k 0 0 595 842 re f "
        "q /Darken gs 0 0 0 rg 0 0 595 842 re f Q "
        "1 1 1 rg 18 794 77 28 re f"
    ).encode("ascii"))
    original.set_contents(content)
    original.insert_text((18, 35), "ALLEN", fontsize=17, color=(1, 1, 1))
    # This nearby text would map inside the output page if off-clip text leaked.
    original.insert_text((140, 30), "Nearby academic text", fontsize=8)
    r = fitz.Rect(3, 7, 107, 69)
    bg = sample_local_background(original, r)
    output = fitz.open()
    repaired = output.new_page(width=595, height=842)
    repaired.show_pdf_page(repaired.rect, source, 0)
    assert _repair_native_flat_background(repaired, source, 0, r, bg)
    assert _pixel(repaired, 20, 30) == _pixel(repaired, 125, 10)
    assert repaired.get_text() == original.get_text()
    assert repaired.get_text().count("ALLEN") == 1
    output.close()
    source.close()


def test_native_background_clip_rejects_local_foreground_paths():
    source = fitz.open()
    page = source.new_page(width=595, height=842)
    bg = (70 / 255, 50 / 255, 26 / 255)
    page.draw_rect(page.rect, color=None, fill=bg)
    r = fitz.Rect(3, 7, 107, 69)
    # Even matching-color local artwork is rejected: only blank source fields
    # may be stretched into the old logo zone.
    page.draw_rect((110, 0, 130, 80), color=None, fill=bg)
    page.draw_rect((3, 72, 107, 87), color=None, fill=bg)
    assert _uniform_background_clip(page, r, bg) is None
    source.close()


def test_native_repair_rejects_nonuniform_logo_field():
    source = fitz.open()
    page = source.new_page(width=595, height=842)
    bg = (70 / 255, 50 / 255, 26 / 255)
    page.draw_rect(page.rect, color=None, fill=bg)
    r = fitz.Rect(3, 7, 107, 69)
    page.draw_rect(r, color=None, fill=(.1, .2, .5))
    output = fitz.open()
    repaired = output.new_page(width=595, height=842)
    repaired.show_pdf_page(repaired.rect, source, 0)
    before = repaired.get_pixmap(clip=r, alpha=False).samples
    assert not _repair_native_flat_background(repaired, source, 0, r, bg)
    assert repaired.get_pixmap(clip=r, alpha=False).samples == before
    output.close()
    source.close()
