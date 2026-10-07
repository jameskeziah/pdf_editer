from io import BytesIO

import pymupdf as fitz
from PIL import Image

from pdf_branding.image_info import image_info
from pdf_branding.renderer import _uniform_background_clip
from pdf_branding.config import load_profile
from pdf_branding import raster_brand
from pathlib import Path


def _image_bytes():
    stream = BytesIO()
    Image.new("RGB", (20, 10), "red").save(stream, format="PNG")
    return stream.getvalue()


def test_native_image_metadata_matches_and_shared_resources_decode_once(monkeypatch):
    with fitz.open() as document:
        page = document.new_page()
        xref = page.insert_image(fitz.Rect(20, 20, 120, 70), stream=_image_bytes())
        document.new_page().insert_image(fitz.Rect(20, 40, 120, 90), xref=xref)
        expected = [[dict(item) for item in page.get_image_info(xrefs=True)] for page in document]
        original, calls = fitz.Pixmap.__init__, []

        def counted(self, *args, **kwargs):
            calls.append(args)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(fitz.Pixmap, "__init__", counted)
        actual = [image_info(document[index]) for index in (0, 1, 0, 1)]
        assert actual == expected * 2
        assert len(calls) == 1


def test_same_geometry_image_stream_mutation_invalidates_metadata():
    with fitz.open() as document:
        page = document.new_page()
        xref = page.insert_image(fitz.Rect(20, 20, 120, 70), stream=_image_bytes())
        before = image_info(page)
        document.update_stream(xref, b"\x00\x00\xff" * 20 * 10)
        after = image_info(page)
        assert before[0]["digest"] != after[0]["digest"]
        with fitz.open(stream=document.tobytes(), filetype="pdf") as independent:
            expected = independent[0].get_image_info(xrefs=True)
            # Native decoded-memory estimates can differ after serialization;
            # geometry, pixels, resources and colour metadata must agree.
            assert [{key: value for key, value in row.items() if key != "size"} for row in after] == [
                {key: value for key, value in row.items() if key != "size"} for row in expected]


def test_new_body_placement_invalidates_cached_page_geometry():
    with fitz.open() as document:
        page = document.new_page()
        xref = page.insert_image(fitz.Rect(20, 20, 120, 70), stream=_image_bytes())
        assert len(image_info(page)) == 1
        page.insert_image(fitz.Rect(20, 300, 120, 350), xref=xref)
        assert len(image_info(page)) == 2
        assert image_info(page)[1]["bbox"][1] == 300


def test_changed_crop_origin_invalidates_cached_geometry():
    with fitz.open() as document:
        page = document.new_page()
        page.insert_image(fitz.Rect(20, 20, 120, 70), stream=_image_bytes())
        before = image_info(page)
        page.set_cropbox(fitz.Rect(10, 10, 500, 790))
        after = image_info(page)
        assert before[0]["bbox"] != after[0]["bbox"]
        with fitz.open(stream=document.tobytes(), filetype="pdf") as independent:
            assert after[0]["bbox"] == independent[0].get_image_info()[0]["bbox"]


def test_failed_background_search_renders_at_most_four_strips(monkeypatch):
    with fitz.open() as document:
        page = document.new_page()
        original, calls = page.get_pixmap, []

        def counted(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(page, "get_pixmap", counted)
        assert _uniform_background_clip(page, fitz.Rect(40, 30, 150, 70), (.123, .456, .789)) is None
        assert len(calls) == 4


def test_clean_ocr_band_never_decodes_unused_resource_identities(monkeypatch):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_image(fitz.Rect(20, 20, 120, 70), stream=_image_bytes())
        page.insert_text((180, 45), "MOTION", fontsize=18)

        def forbidden(_page):
            raise AssertionError("Clean OCR must not resolve resource digests")

        monkeypatch.setattr(raster_brand, "page_image_info", forbidden)
        scan = raster_brand.scan_header(page, load_profile(Path(__file__).parents[1] / "profiles/sskem.json"))
        assert not scan.hits and not scan.unsafe_hits and scan.error is None
