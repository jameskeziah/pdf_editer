from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageChops

from pdf_branding.config import load_profile
from pdf_branding.models import DocumentMeta, DocumentPlan, DocumentProfile, DocumentType, PagePlan, PageType, PageStrategy, RenderStrategy, HeaderTemplate, RectData, RepeatedBands
from pdf_branding.renderer import render_document
from pdf_branding.qa import run_qa


def test_verified_transparent_logo_removal_retains_native_gradient_and_diagram(tmp_path):
    source = fitz.open()
    page = source.new_page(width=595, height=842)
    for y in range(20, 82):
        page.draw_rect((10, y, 250, y+1), color=None, fill=(1, .92+y/1600, .78+y/1400))
    page.insert_text((60, 150), "Academic diagram stays intact")
    page.draw_line((60, 250), (180, 250), width=.3)
    reference = fitz.open(stream=source.tobytes(), filetype="pdf")
    artwork = Image.new("RGBA", (200, 60), (0, 0, 0, 0))
    ImageDraw.Draw(artwork).text((4, 15), "ALLEN", font_size=38, fill=(40, 75, 145, 255))
    stream = BytesIO()
    artwork.save(stream, format="PNG")
    page.insert_image((20, 20, 220, 80), stream=stream.getvalue())
    digest = page.get_image_info(xrefs=True)[0]["digest"].hex()
    brand = load_profile(Path(__file__).parents[1]/"profiles/sskem.json")
    brand.raw = deepcopy(brand.raw)
    brand.raw["legacy"]["raster_logo_digests"] = [digest]
    region = RectData(16, 16, 224, 84)
    pp = PagePlan(1, PageType.DESIGNED, RenderStrategy.REPLACE_LOGO_ONLY, PageStrategy.DESIGNED_PAGE_LOGO_ONLY,
                  HeaderTemplate.NONE, .98, logo_replace_rect=region, logo_background=(1, .95, .83),
                  legacy_logo_rects=[region], protected_visual_rects=[RectData(59, 249, 181, 251)])
    plan = DocumentPlan(Path("fixture.pdf"), DocumentMeta("6", "physics", "Motion", None, "KEY POINTS", 1),
                        DocumentProfile(Path("fixture.pdf"), DocumentType.KEY_POINTS, 1), RepeatedBands(), [pp])
    with render_document(source, plan, brand) as output:
        result = tmp_path / "result.pdf"
        output.save(result, clean=False)
    with fitz.open(result) as output:
        before = reference[0].get_pixmap(clip=fitz.Rect(20, 20, 65, 80), alpha=False)
        after = output[0].get_pixmap(clip=fitz.Rect(20, 20, 65, 80), alpha=False)
        assert before.samples == after.samples  # Outside the replacement emblem.
    assert run_qa(source, result, plan, brand).passed
    reference.close()
    source.close()
