from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

import pdf_branding.analysis as analysis
from pdf_branding.config import BrandingProfile, load_profile, validate_profile
from pdf_branding.engine import process_pdf
from pdf_branding.qa import run_qa
from pdf_branding.raster_brand import _standalone_brand, scan_header
from pdf_branding.renderer import render_document
from test_geometry_safety import PROFILE, _plan


def _font():
    for name in ("C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, 64)
        except OSError:
            pass
    raise RuntimeError("OCR regression requires an available bold TrueType font")


def _logo_image():
    font = _font()
    bbox = font.getbbox("ALLEN")
    image = Image.new("RGB", (bbox[2] - bbox[0] + 16, bbox[3] - bbox[1] + 16), "white")
    ImageDraw.Draw(image).text((8 - bbox[0], 8 - bbox[1]), "ALLEN", fill=(15, 55, 110), font=font)
    return image


def _stream(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _standalone_document():
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    image = _logo_image()
    rect = fitz.Rect(36, 20, 36 + image.width / 2, 20 + image.height / 2)
    page.insert_image(rect, stream=_stream(image))
    page.insert_text((40, 125), "Academic body below the independent raster logo.", fontsize=12)
    return document, rect


def _footer_document(alignment="center"):
    document = fitz.open()
    # A second page ensures centre/footer scanning is not a first-page fallback.
    document.new_page(width=595, height=842).insert_text((40, 200), "First academic page.")
    page = document.new_page(width=595, height=842)
    page.insert_text((40, 200), "Second academic page with a bitmap footer mark.")
    image = _logo_image()
    width, height = image.width / 2, image.height / 2
    x = {"left": 36, "center": (595 - width) / 2, "right": 595 - 36 - width}[alignment]
    rect = fitz.Rect(x, 770, x + width, 770 + height)
    page.insert_image(rect, stream=_stream(image))
    return document, rect


def test_real_bundled_ocr_detects_small_standalone_raster_allen():
    brand = load_profile(PROFILE)
    with _standalone_document()[0] as document:
        page = document[0]
        assert not page.search_for("ALLEN"), "Fixture must contain raster rather than searchable legacy text"
        scan = scan_header(page, brand)
        assert scan.error is None, scan.error
        assert len(scan.hits) == 1 and not scan.unsafe_hits
        expected = fitz.Rect(page.get_image_info(xrefs=True)[0]["bbox"])
        actual, digest = scan.hits[0]
        assert max(abs(a - b) for a, b in zip(actual, expected)) < .01
        assert digest == page.get_image_info(xrefs=True)[0]["digest"].hex()


def test_real_ocr_does_not_assign_logo_to_unrelated_image_at_same_x():
    brand = load_profile(PROFILE)
    document, logo_rect = _standalone_document()
    with document:
        page = document[0]
        unrelated = Image.new("RGB", (229, 62), (50, 170, 240))
        page.insert_image((logo_rect.x0, 90, logo_rect.x1, 90 + logo_rect.height), stream=_stream(unrelated))
        scan = scan_header(page, brand)
        assert scan.error is None, scan.error
        assert len(scan.hits) == 1
        assert max(abs(a - b) for a, b in zip(scan.hits[0][0], logo_rect)) < .01
        assert not scan.unsafe_hits


def test_real_object_ocr_rejects_brand_embedded_in_academic_image():
    brand = load_profile(PROFILE)
    image = Image.new("RGB", (340, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.text((8, 0), "ALLEN", font=_font(), fill="black")
    draw.text((8, 80), "LESSON", font=_font(), fill="black")
    with fitz.open() as document:
        page = document.new_page()
        page.insert_image((35, 20, 205, 100), stream=_stream(image))
        item = page.get_image_info(xrefs=True)[0]
        model = (brand.source_path.parent / brand.raw["assets"]["ocr_eng"]).resolve()
        assert not _standalone_brand(page, item, {"ALLEN"}, model)


def test_real_full_scan_brand_is_held_when_logo_boundary_is_unknown(tmp_path: Path):
    brand = load_profile(PROFILE)
    scan_image = Image.new("RGB", (595, 842), "white")
    logo = _logo_image()
    scan_image.paste(logo, (20, 20))
    ImageDraw.Draw(scan_image).text((40, 200), "Scanned academic content", fill="black")
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=_stream(scan_image))
        scan = scan_header(page, brand)
        assert scan.error is None, scan.error
        assert scan.unsafe_hits and not scan.hits
        analyses, plan = _plan(source, brand)
        assert analyses[0].image_only and analyses[0].raster_ocr_unsafe
        assert plan.pages[0].header_rect is None
        assert not plan.pages[0].legacy_logo_rects
        path = tmp_path / "held-scan.pdf"
        with render_document(source, plan, brand) as output:
            output.save(path)
        report = run_qa(source, path, plan, brand)
    assert not report.passed
    assert any(issue.code == "RASTER_BRAND_UNSAFE_GEOMETRY" for issue in report.issues)
    assert report.metrics["average_body_text_retention"] is None


@pytest.mark.parametrize("remove_logo", [False, True])
def test_independent_output_ocr_rejects_surviving_mark_and_accepts_clean_render(tmp_path: Path, remove_logo: bool):
    brand = load_profile(PROFILE)
    source, _ = _standalone_document()
    with source:
        analyses, plan = _plan(source, brand)
        assert analyses[0].raster_ocr_checked and not analyses[0].raster_ocr_error
        assert plan.pages[0].legacy_logo_rects
        path = tmp_path / "independent-ocr.pdf"
        if remove_logo:
            output = render_document(source, plan, brand)
        else:
            # Keep the actual raster word so QA must independently recognize it.
            output = fitz.open(stream=source.tobytes(), filetype="pdf")
        with output:
            output.save(path)
        report = run_qa(source, path, plan, brand)
    assert report.passed is remove_logo, report.issues
    assert any(issue.code == "RASTER_LEGACY_TEXT_REMAINS" for issue in report.issues) is not remove_logo


def test_missing_configured_ocr_model_is_rejected_before_processing(tmp_path: Path):
    original = load_profile(PROFILE)
    raw = deepcopy(original.raw)
    raw["assets"]["ocr_eng"] = str(tmp_path / "missing" / "eng.traineddata")
    brand = BrandingProfile(original.source_path, raw)
    with pytest.raises(FileNotFoundError, match="OCR language model not found"):
        validate_profile(brand)


def test_truncated_ocr_model_is_rejected_before_processing(tmp_path: Path):
    original = load_profile(PROFILE)
    raw = deepcopy(original.raw)
    model = tmp_path / "eng.traineddata"
    model.write_bytes(b"\x18\x00\x00\x00")
    raw["assets"]["ocr_eng"] = str(model)
    with pytest.raises(ValueError, match="Invalid assets.ocr_eng"):
        validate_profile(BrandingProfile(original.source_path, raw))


def test_unconfigured_ocr_returns_error_instead_of_false_negative():
    original = load_profile(PROFILE)
    raw = deepcopy(original.raw)
    raw["assets"].pop("ocr_eng")
    with _standalone_document()[0] as document:
        scan = scan_header(document[0], BrandingProfile(original.source_path, raw))
    assert scan.error == "No OCR language model configured"
    assert not scan.hits


def test_ocr_runtime_failure_stops_qa_certification(tmp_path: Path, monkeypatch):
    brand = load_profile(PROFILE)
    with _standalone_document()[0] as source:
        # Build a genuine recognized plan before injecting a runtime failure.
        _, plan = _plan(source, brand)
        assert plan.pages[0].raster_ocr_checked
        path = tmp_path / "ocr-runtime-error.pdf"
        with render_document(source, plan, brand) as output:
            output.save(path)
        def fail_ocr(*args, **kwargs):
            raise RuntimeError("OCR runtime unavailable")
        monkeypatch.setattr(fitz.Pixmap, "pdfocr_tobytes", fail_ocr)
        report = run_qa(source, path, plan, brand)
    assert not report.passed
    assert any(issue.code == "RASTER_OCR_UNAVAILABLE" and "runtime unavailable" in issue.message for issue in report.issues)


@pytest.mark.parametrize("alignment", ["left", "center", "right"])
def test_real_ocr_detects_footer_mark_on_later_page(alignment):
    brand = load_profile(PROFILE)
    source, expected = _footer_document(alignment)
    with source:
        scan = scan_header(source[1], brand)
        assert scan.error is None, scan.error
        assert len(scan.hits) == 1 and not scan.unsafe_hits
        assert max(abs(a - b) for a, b in zip(scan.hits[0][0], expected)) < .01
        analyses, _ = _plan(source, brand)
    assert analyses[1].raster_ocr_checked and not analyses[1].raster_ocr_unsafe
    assert not analyses[1].legacy_rects, "Footer recognition must not deepen header cleanup/crop"
    assert analyses[1].legacy_logo_rects and all(rect.y0 > 752 for rect in analyses[1].legacy_logo_rects)


def test_real_footer_word_in_full_scan_is_unsafe():
    brand = load_profile(PROFILE)
    image = Image.new("RGB", (595, 842), "white")
    logo = _logo_image()
    image.paste(logo, ((595 - logo.width) // 2, 770))
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=_stream(image))
        scan = scan_header(page, brand)
        assert scan.error is None, scan.error
        assert scan.unsafe_hits and not scan.hits
        assert all(rect.y0 > 752 for rect in scan.unsafe_hits)
        analyses, _ = _plan(source, brand)
    assert analyses[0].raster_ocr_unsafe
    assert not analyses[0].legacy_logo_rects


def test_decorative_footer_graphic_is_not_authorized_as_brand():
    brand = load_profile(PROFILE)
    with fitz.open() as source:
        for _ in range(3):
            page = source.new_page(width=595, height=842)
            page.insert_text((40, 200), "Academic content with a recurring decorative footer.")
            page.insert_image((30, 770, 565, 795), stream=_stream(Image.new("RGB", (535, 25), (205, 225, 250))))
        scan = scan_header(source[1], brand)
        assert scan.error is None, scan.error
        assert not scan.hits and not scan.unsafe_hits
        analyses, _ = _plan(source, brand)
    assert all(not page.legacy_logo_rects and not page.legacy_image_digests for page in analyses)


def test_verified_footer_digest_skips_ocr_but_unknown_overlap_triggers_it(monkeypatch):
    original = load_profile(PROFILE)
    source, rect = _footer_document()
    with source:
        raw = deepcopy(original.raw)
        digest = source[1].get_image_info(xrefs=True)[0]["digest"].hex()
        raw["legacy"]["raster_logo_digests"] = [digest]
        brand = BrandingProfile(original.source_path, raw)
        calls = []
        real_scan = scan_header
        def track_scan(page, profile):
            calls.append(page.number)
            return real_scan(page, profile)
        monkeypatch.setattr(analysis, "scan_header", track_scan)
        analyses, _ = _plan(source, brand)
        assert not calls
        assert not analyses[1].raster_ocr_checked and analyses[1].legacy_logo_rects
        source[1].insert_image((rect.x0 - 1, rect.y1 - 1, rect.x1 + 1, rect.y1 + 6),
                               stream=_stream(Image.new("RGB", (230, 14), (30, 130, 210))))
        analyses, _ = _plan(source, brand)
        assert calls == [1]
        assert analyses[1].raster_ocr_checked
        assert len(analyses[1].legacy_logo_rects) == 1


def test_engine_removes_verified_footer_bitmap_and_preserves_academic_content(tmp_path: Path):
    brand = load_profile(PROFILE)
    input_root = tmp_path / "input"
    source_path = input_root / "6th class" / "science" / "07 Measurement and Motion" / "Exercise _ solutions_Measurement and Motion.pdf"
    source_path.parent.mkdir(parents=True)
    source, _ = _footer_document()
    with source:
        source.save(source_path)
    original_hash = sha256(source_path.read_bytes()).hexdigest()
    output_path = tmp_path / "output" / source_path.name
    reports = tmp_path / "reports"
    result = process_pdf(source_path, output_path, input_root, brand, reports, source_path.relative_to(input_root))
    assert result.status == "completed", (result.message, result.qa.issues if result.qa else None)
    assert result.qa is not None and result.qa.passed
    assert result.plan is not None and result.plan.pages[1].raster_ocr_checked
    assert result.plan.pages[1].legacy_logo_rects[0].y0 >= 752
    assert len(result.plan.pages[1].additional_logo_rects) == 1
    assert sha256(source_path.read_bytes()).hexdigest() == original_hash
    with fitz.open(output_path) as output:
        assert "First academic page." in output[0].get_text()
        assert "Second academic page with a bitmap footer mark." in output[1].get_text()
        final_scan = scan_header(output[1], brand)
        assert final_scan.error is None, final_scan.error
        assert not final_scan.hits and not final_scan.unsafe_hits
    report_stem = reports / source_path.relative_to(input_root).parent / source_path.stem
    assert report_stem.with_name(report_stem.name + ".plan.json").is_file()
    assert report_stem.with_name(report_stem.name + ".qa.json").is_file()
