from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from pdf_branding.analysis import analyze_document
from pdf_branding.cache import BatchCache
from pdf_branding.config import load_profile
from pdf_branding.engine import analyze_pdf, process_pdf
from pdf_branding.metadata import build_meta, infer_subject
from pdf_branding.models import PageType, RenderStrategy
from pdf_branding.planner import make_document_plan
from pdf_branding.preview import render_plan_previews
from pdf_branding.metadata import detect_document_profile


ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "profiles" / "sskem.json"


def _insert_legacy_header(p: fitz.Page, centered: bool = False) -> None:
    if centered:
        p.draw_rect((205, 14, 390, 50), fill=(0.15, 0.3, 0.65), color=None)
        p.insert_text((240, 42), "ALLEN", fontsize=25, fontname="hebo", color=(1, 1, 1))
    else:
        p.insert_text((36, 25), "ALLEN", fontsize=17, fontname="hebo")
        p.insert_text((505, 25), "Physics", fontsize=10, fontname="hebo")
    p.draw_line((30, 46), (565, 46), color=(0, 0, 0.45), width=1)


def make_test_fixture(path: Path, pages: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        _insert_legacy_header(p, centered=(i == 0))
        if i == 0:
            p.insert_text((175, 82), "Measurement and Motion", fontsize=18, fontname="hebo")
            p.insert_text((270, 109), "Test", fontsize=12, fontname="hebo")
            p.insert_text((45, 148), "This test contains 20 questions.", fontsize=11)
        else:
            p.insert_text((45, 82), f"Question {i}: body content must survive at the same position.", fontsize=11)
            p.insert_text((45, 111), "More academic body text here for coordinate validation.", fontsize=11)
        p.insert_text((18, 630), "LIVE Module PNCF", fontsize=8, rotate=90)
        p.draw_line((30, 812), (565, 812), color=(0, 0, 0.4), width=1)
        p.insert_text((500, 830), f"Page {i+1}", fontsize=8)
    doc.save(path)
    doc.close()


def make_ncert_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(2):
        p = doc.new_page(width=595, height=842)
        _insert_legacy_header(p)
        p.draw_rect((45, 55, 550, 83), fill=(0.28, 0.10, 0.52), color=None)
        p.insert_text((155, 76), "NCERT QUESTIONS WITH SOLUTIONS", fontsize=14, fontname="hebo", color=(1, 1, 1))
        p.insert_text((48, 106), f"{1+i}. Give two examples of modes of transport used on land, water and air.", fontsize=10)
        p.insert_text((72, 132), "Solution", fontsize=10, fontname="hebo")
        p.insert_text((72, 154), "The academic answer text must remain intact and unmoved.", fontsize=10)
    doc.save(path)
    doc.close()


def make_keypoints_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(2):
        p = doc.new_page(width=595, height=842)
        p.draw_rect(p.rect, fill=(0.21, 0.12, 0.06), color=None)
        if i == 0:
            # Searchable ALLEN on a dark designed page.
            p.insert_text((18, 36), "ALLEN", fontsize=18, fontname="hebo", color=(1, 1, 1))
        p.insert_text((300, 85), "Measurement & Motion", fontsize=22, fontname="hebo", color=(1, 0.9, 0.1))
        p.insert_text((180, 190), "Measurement", fontsize=18, color=(1, 0.9, 0.1))
        p.insert_text((180, 220), "A measurement compares an unknown quantity with a standard quantity.", fontsize=10, color=(1, 1, 1))
    doc.save(path)
    doc.close()


def test_science_subject_mapping():
    root = Path("C:/x/foundation_notes")
    p = root / "8th class" / "science" / "03 Coal And Petroleum" / "Coal And Petroleum.pdf"
    assert infer_subject(p, root, "8") == "chemistry"
    p2 = root / "9th class" / "science" / "08 Gravitation" / "Gravitation.pdf"
    assert infer_subject(p2, root, "9") == "physics"


def test_direct_chapter_root_still_classifies(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion.pdf"
    make_test_fixture(src, 1)
    profile = load_profile(PROFILE)
    # Input root is intentionally the chapter itself; full path context must still recover class/subject.
    with fitz.open(src) as doc:
        meta = build_meta(src, src.parent, doc)
        assert meta is not None
        assert meta.class_name == "6"
        assert meta.subject == "physics"


def test_repeated_header_and_safe_pageplan(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_test_fixture(src)
    profile = load_profile(PROFILE)
    with fitz.open(src) as doc:
        meta = build_meta(src, tmp_path, doc)
        assert meta is not None
        dprof = detect_document_profile(src, doc, meta)
        repeated, analyses = analyze_document(doc, meta, profile)
        assert 35 < repeated.header_bottom < 70
        plan = make_document_plan(src, meta, dprof, repeated, analyses, profile)
        assert len(plan.pages) == 4
        for pa, analysis in zip(plan.pages, analyses):
            if pa.header_rect and analysis.first_content_y is not None:
                assert pa.header_rect.y1 <= analysis.first_content_y - profile.layout("safety_gap", 7) + 0.01
            assert pa.strategy != RenderStrategy.REBUILD_CROP


def test_render_removes_legacy_preserves_body_and_coordinates(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_test_fixture(src)
    out = tmp_path / "out" / "branded.pdf"
    profile = load_profile(PROFILE)
    result = process_pdf(src, out, tmp_path, profile, tmp_path / "reports", Path("fixture.pdf"))
    assert result.status == "completed", result.message
    assert result.qa is not None and result.qa.passed
    assert result.qa.metrics["max_body_position_shift_pt"] <= 0.2
    with fitz.open(out) as doc:
        assert not doc[0].search_for("ALLEN")
        assert "This test contains 20 questions" in doc[0].get_text("text")
        assert "Question 1: body content must survive" in doc[1].get_text("text")
        assert not doc[1].search_for("LIVE Module")


def test_ncert_heading_is_not_deleted(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "NCERT _ solutions_Measurement and Motion.pdf"
    make_ncert_fixture(src)
    out = tmp_path / "out" / "ncert.pdf"
    profile = load_profile(PROFILE)
    result = process_pdf(src, out, tmp_path, profile)
    assert result.status == "completed", result.message
    with fitz.open(out) as doc:
        text = doc[0].get_text("text")
        assert "NCERT QUESTIONS WITH SOLUTIONS" in text
        assert "academic answer text must remain intact" in text
        assert not doc[0].search_for("ALLEN")


def test_keypoints_uses_logo_only_strategy(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion_Key Points.pdf"
    make_keypoints_fixture(src)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    assert all(p.page_type == PageType.DESIGNED for p in plan.pages)
    assert plan.pages[0].strategy == RenderStrategy.REPLACE_LOGO_ONLY
    assert plan.pages[0].header_rect is None
    out = tmp_path / "out" / "keypoints.pdf"
    result = process_pdf(src, out, tmp_path, profile)
    assert result.status == "completed", result.message
    with fitz.open(out) as doc:
        assert not doc[0].search_for("ALLEN")
        assert "Measurement & Motion" in doc[0].get_text("text")


def test_plan_preview_generation(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_test_fixture(src)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    outputs = render_plan_previews(src, plan, tmp_path / "previews")
    assert outputs
    assert all(p.exists() and p.stat().st_size > 1000 for p in outputs)


def test_cache_roundtrip(tmp_path: Path):
    src = tmp_path / "source.pdf"
    doc = fitz.open(); p = doc.new_page(); p.insert_text((40, 80), "hello"); doc.save(src); doc.close()
    out = tmp_path / "out.pdf"
    out.write_bytes(src.read_bytes())
    cache = BatchCache(tmp_path / "cache_root")
    profile = load_profile(PROFILE)
    ph = cache.profile_hash(profile.raw)
    rel = Path("source.pdf")
    cache.update(rel, src, out, ph, True)
    cache.save()
    cache2 = BatchCache(tmp_path / "cache_root")
    assert cache2.is_fresh(rel, src, out, ph)

import pytest
from pdf_branding.planner import choose_header_template
from pdf_branding.models import HeaderTemplate, PageStrategy, PageType


@pytest.mark.parametrize(
    "filename,expected_material,expected_template",
    [
        ("Measurement and Motion.pdf", "NOTES", HeaderTemplate.NOTES),
        ("Measurement and Motion_Key Points.pdf", "KEY POINTS", HeaderTemplate.NONE),
        ("Exercise _ solutions_Measurement and Motion.pdf", "EXERCISE", HeaderTemplate.PRACTICE),
        ("NCERT Practice _ solutions_Measurement and Motion.pdf", "NCERT PRACTICE", HeaderTemplate.PRACTICE),
        ("Practice Sheet _ solutions_Measurement and Motion.pdf", "PRACTICE SHEET", HeaderTemplate.PRACTICE),
        ("Race _ solutions_Measurement and Motion.pdf", "DPP", HeaderTemplate.TEST),
        ("Test _ solutions_Measurement and Motion.pdf", "TEST", HeaderTemplate.TEST),
    ],
)
def test_material_families_route_to_expected_templates(tmp_path: Path, filename: str, expected_material: str, expected_template: HeaderTemplate):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / filename
    make_test_fixture(src, 2)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    assert plan.meta.material_type == expected_material
    if expected_material == "KEY POINTS":
        assert all(p.header_template == HeaderTemplate.NONE for p in plan.pages)
    else:
        assert any(p.header_template == expected_template for p in plan.pages)


def make_realistic_repeated_header_test_fixture(path: Path) -> None:
    """Approximate the real ALLEN Test family seen in plan previews."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page(width=595, height=842)
        p.draw_rect((25, 25, 570, 820), color=(0, 0, 0.35), width=2)
        if i == 0:
            # Large centered first-page logo extending much deeper than the compact new header.
            p.insert_text((205, 69), "ALLEN", fontsize=34, fontname="hebo", color=(0.12, 0.28, 0.62))
            p.insert_text((180, 126), "Measurement and Motion", fontsize=16, fontname="hebo")
            p.insert_text((274, 153), "Test", fontsize=11, fontname="hebo")
            p.insert_text((35, 184), "Time - 30 Minutes", fontsize=10, fontname="hebo")
            p.insert_text((35, 211), "Important Instructions", fontsize=10, fontname="hebo")
            p.insert_text((50, 239), "This test contains 20 questions.", fontsize=10)
        else:
            p.draw_rect((30, 28, 470, 55), fill=(0.80, 0.87, 1.0), color=None)
            p.insert_text((38, 48), "NCERT Basics : Class 6", fontsize=11)
            p.insert_text((485, 49), "ALLEN", fontsize=18, fontname="hebo", color=(0.12, 0.28, 0.62))
            p.draw_line((30, 60), (565, 60), color=(0, 0, 0.45), width=1)
            p.insert_text((36, 88), f"{3 + i}. Body question begins here and must not move.", fontsize=10)
            p.insert_text((72, 116), "Additional academic text.", fontsize=10)
        p.insert_text((15, 650), "PNCF 2024-25 LIVE Module SET-1 Measurement and Motion", fontsize=6, rotate=90)
        p.draw_rect((30, 796, 565, 817), fill=(0.80, 0.87, 1.0), color=None)
        p.insert_text((530, 812), f"[{i+1}]", fontsize=8, fontname="hebo")
    doc.save(path)
    doc.close()


def test_repeated_legacy_text_is_not_body_boundary(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_realistic_repeated_header_test_fixture(src)
    profile = load_profile(PROFILE)
    with fitz.open(src) as doc:
        meta = build_meta(src, tmp_path, doc)
        assert meta is not None
        repeated, analyses = analyze_document(doc, meta, profile)
        assert repeated.header_signatures
        # Pages 2+ must see the real question as body, not "NCERT Basics : Class 6".
        assert analyses[1].first_content_y is not None and analyses[1].first_content_y > 75
        assert analyses[2].first_content_y is not None and analyses[2].first_content_y > 75


def test_cleanup_band_and_visual_header_are_independent(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_realistic_repeated_header_test_fixture(src)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    first = plan.pages[0]
    second = plan.pages[1]
    assert first.cleanup_rects and first.header_rect is not None
    # V3.4 lets the visible header adapt upward (to a capped maximum) while the
    # cleanup band may still extend farther through old artwork.
    assert first.cleanup_rects[0].y1 >= first.header_rect.y1
    assert first.header_rect.y1 <= profile.layout("header_adaptive_max_height", 76) + 0.01
    # Repeated-page source headers must also receive a top cleanup/header plan.
    assert second.cleanup_rects and second.header_rect is not None
    assert second.cleanup_rects[0].y1 >= 58
    assert second.cleanup_rects[0].y1 < 82


def test_realistic_test_render_removes_repeated_headers_without_moving_body(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_realistic_repeated_header_test_fixture(src)
    out = tmp_path / "out" / "realistic_test.pdf"
    profile = load_profile(PROFILE)
    with fitz.open(src) as before:
        original_hits = before[1].search_for("Body question begins here")
        assert original_hits
        original_y = original_hits[0].y0
    result = process_pdf(src, out, tmp_path, profile)
    assert result.status == "completed", result.message
    with fitz.open(out) as doc:
        assert not doc[0].search_for("ALLEN")
        assert not doc[1].search_for("ALLEN")
        assert not doc[1].search_for("NCERT Basics")
        hits = doc[1].search_for("Body question begins here")
        assert hits
        assert abs(hits[0].y0 - original_y) <= 0.2


def make_unsearchable_test_logo_fixture(path: Path) -> None:
    """Approximate a first Test page whose ALLEN artwork is not searchable text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.draw_rect((25, 25, 570, 820), color=(0, 0, 0.35), width=2)
    # Vector-only pseudo logo: intentionally contains no searchable ALLEN text.
    p.draw_rect((205, 42, 390, 90), fill=(0.12, 0.28, 0.62), color=None)
    p.draw_rect((220, 50, 375, 82), fill=(1, 1, 1), color=None)
    # First academic title begins safely below the logo artwork.
    p.draw_rect((45, 102, 550, 139), fill=(0.80, 0.87, 1.0), color=None)
    p.insert_text((178, 127), "Measurement and Motion", fontsize=16, fontname="hebo")
    p.insert_text((274, 157), "Test", fontsize=11, fontname="hebo")
    p.insert_text((35, 188), "This test contains 20 questions.", fontsize=10)
    p.draw_rect((30, 796, 565, 817), fill=(0.80, 0.87, 1.0), color=None)
    p.insert_text((530, 812), "[1]", fontsize=8, fontname="hebo")
    doc.save(path)
    doc.close()


def test_first_page_visual_logo_fallback_when_allen_is_unsearchable(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_unsearchable_test_logo_fixture(src)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    first = plan.pages[0]
    assert first.cleanup_rects and first.header_rect is not None
    # V3.4 fallback fully covers the visual logo zone while the adaptive header
    # itself remains capped below the academic title.
    assert first.cleanup_rects[0].y1 >= 90
    assert first.cleanup_rects[0].y1 >= first.header_rect.y1
    assert first.header_rect.y1 <= profile.layout("header_adaptive_max_height", 76) + 0.01
    assert first.cleanup_rects[0].y1 < 102


def test_repeated_graphic_footer_band_is_replaced_in_place(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_realistic_repeated_header_test_fixture(src)
    profile = load_profile(PROFILE)
    with fitz.open(src) as doc:
        meta = build_meta(src, tmp_path, doc)
        assert meta is not None
        repeated, analyses = analyze_document(doc, meta, profile)
        assert repeated.footer_top is not None
        assert 785 <= repeated.footer_top <= 810
        dprof = detect_document_profile(src, doc, meta)
        plan = make_document_plan(src, meta, dprof, repeated, analyses, profile)
        # At least the repeated pages must wipe the original blue page-number band,
        # rather than adding a second footer below it.
        assert plan.pages[1].footer_rect is not None
        assert plan.pages[1].footer_rect.y0 <= 810



def make_designed_chapter_opener_fixture(path: Path) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    image_path = path.parent / "chapter_scene.png"
    Image.new("RGB", (520, 300), (120, 235, 235)).save(image_path)
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((120, 105), "1   Measurement and Motion", fontsize=24, fontname="hebo")
    p.insert_image((95, 145, 500, 405), filename=str(image_path))
    p.insert_text((80, 445), "1. Introduction", fontsize=13, fontname="hebo")
    p.insert_text((95, 475), "Physics is about the world around you and how everything works.", fontsize=10)
    p.insert_text((15, 620), "PNCF 2024-25 LIVE Module SET-1 Measurement and Motion", fontsize=6, rotate=90)
    doc.save(path)
    doc.close()


def test_designed_chapter_opener_forbids_generic_header(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion.pdf"
    make_designed_chapter_opener_fixture(src)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    first = plan.pages[0]
    assert first.page_type == PageType.DESIGNED
    assert first.page_strategy == PageStrategy.DESIGNED_PAGE_PRESERVE
    assert first.header_rect is None
    assert first.header_template == HeaderTemplate.NONE
    assert first.vertical_text_rects


def make_raster_footer_fixture(path: Path) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    band = path.parent / "footer_band.png"
    Image.new("RGB", (700, 30), (210, 225, 255)).save(band)
    doc = fitz.open()
    for i in range(4):
        p = doc.new_page(width=595, height=842)
        p.draw_rect((30, 28, 470, 55), fill=(0.80, 0.87, 1.0), color=None)
        p.insert_text((38, 48), "NCERT Basics : Class 6", fontsize=11)
        p.insert_text((485, 49), "ALLEN", fontsize=18, fontname="hebo")
        p.draw_line((30, 60), (565, 60), color=(0, 0, 0.45), width=1)
        p.insert_text((40, 105), f"{i+1}. Academic body text that must remain in place.", fontsize=10)
        # Raster-only source footer: no vector rectangle is available to get_drawings().
        p.insert_image((30, 796, 565, 817), filename=str(band))
        p.insert_text((530, 812), f"[{i+1}]", fontsize=8, fontname="hebo")
    doc.save(path)
    doc.close()


def test_repeated_raster_footer_is_detected_and_planned(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Exercise _ solutions_Measurement and Motion.pdf"
    make_raster_footer_fixture(src)
    profile = load_profile(PROFILE)
    with fitz.open(src) as doc:
        meta = build_meta(src, tmp_path, doc)
        assert meta is not None
        repeated, analyses = analyze_document(doc, meta, profile)
        assert repeated.footer_top is not None
        assert repeated.footer_confidence >= 0.55
        assert 788 <= repeated.footer_top <= 805
        dprof = detect_document_profile(src, doc, meta)
        plan = make_document_plan(src, meta, dprof, repeated, analyses, profile)
        assert all(p.footer_rect is not None for p in plan.pages)
        assert all(p.footer_rect.y0 <= 805 for p in plan.pages if p.footer_rect)


def test_explicit_page_strategies_are_emitted(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Test _ solutions_Measurement and Motion.pdf"
    make_unsearchable_test_logo_fixture(src)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    assert plan.pages[0].page_strategy == PageStrategy.FIRST_PAGE_LARGE_LOGO_REPLACEMENT


def test_designed_chapter_opener_renders_without_generic_header(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion.pdf"
    make_designed_chapter_opener_fixture(src)
    out = tmp_path / "out" / "notes.pdf"
    profile = load_profile(PROFILE)
    result = process_pdf(src, out, tmp_path, profile)
    assert result.status == "completed", result.message
    with fitz.open(out) as doc:
        text = doc[0].get_text("text")
        assert "Measurement and Motion" in text
        assert "SHREE SAMARTH KRUPA" not in text
        assert not doc[0].search_for("PNCF")


def test_raster_footer_is_truly_redacted_and_replaced(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Exercise _ solutions_Measurement and Motion.pdf"
    make_raster_footer_fixture(src)
    out = tmp_path / "out" / "exercise.pdf"
    profile = load_profile(PROFILE)
    result = process_pdf(src, out, tmp_path, profile)
    assert result.status == "completed", result.message
    with fitz.open(out) as doc:
        assert not doc[0].search_for("[1]")
        assert doc[0].search_for("1 / 4")
        assert not doc[0].search_for("ALLEN")


def make_realistic_designed_opener_with_later_repeated_headers(path: Path) -> None:
    """Mimics the real Measurement and Motion file: designed page 1, standard later pages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    # No raster image on purpose: the detector must also use title prominence/layout.
    p.draw_rect((76, 72, 540, 155), fill=(0.95, 0.97, 1.0), color=(0.1, 0.1, 0.45), width=1.2)
    p.insert_text((120, 122), "1   Measurement and Motion", fontsize=24, fontname="hebo")
    p.draw_rect((80, 180, 410, 395), fill=(0.55, 0.92, 0.92), color=None)
    p.insert_text((80, 438), "1. Introduction", fontsize=13, fontname="hebo")
    p.insert_text((95, 470), "Physics is about the world around you and how everything works.", fontsize=10)
    p.insert_text((15, 620), "PNCF 2024-25 LIVE Module SET-1 Measurement and Motion", fontsize=6, rotate=90)
    # Later pages use the recurring legacy source header/footer.
    for i in range(2):
        q = doc.new_page(width=595, height=842)
        q.draw_rect((30, 28, 470, 55), fill=(0.80, 0.87, 1.0), color=None)
        q.insert_text((38, 48), "NCERT Basics : Class 6", fontsize=11)
        q.insert_text((485, 49), "ALLEN", fontsize=18, fontname="hebo")
        q.draw_line((30, 60), (565, 60), color=(0, 0, 0.45), width=1)
        q.insert_text((50, 105), f"{i+2}. Standard academic page body must stay fixed.", fontsize=10)
        q.draw_rect((30, 798, 565, 820), fill=(0.82, 0.88, 1.0), color=(0, 0, 0.45), width=0.6)
        q.insert_text((530, 814), f"[{i+2}]", fontsize=8, fontname="hebo")
    doc.save(path)
    doc.close()


def test_realistic_designed_opener_ignores_document_level_repeated_header(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion.pdf"
    make_realistic_designed_opener_with_later_repeated_headers(src)
    profile = load_profile(PROFILE)
    plan = analyze_pdf(src, tmp_path, profile)
    first = plan.pages[0]
    assert first.page_type == PageType.DESIGNED
    assert first.page_strategy == PageStrategy.DESIGNED_PAGE_PRESERVE
    assert first.header_rect is None
    assert first.footer_rect is None
    assert first.header_template == HeaderTemplate.NONE
    assert first.vertical_text_rects
    # Later pages still use the standard repeated header/footer replacement.
    assert plan.pages[1].page_strategy == PageStrategy.STANDARD_HEADER_FOOTER_REPLACEMENT
    assert plan.pages[1].header_rect is not None
    assert plan.pages[1].footer_rect is not None


def test_designed_page_is_exempt_from_generic_footer_qa(tmp_path: Path):
    src = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion.pdf"
    make_realistic_designed_opener_with_later_repeated_headers(src)
    out = tmp_path / "out" / "notes.pdf"
    profile = load_profile(PROFILE)
    result = process_pdf(src, out, tmp_path, profile)
    assert result.status == "completed", result.message
    with fitz.open(out) as doc:
        text = doc[0].get_text("text")
        assert "Measurement and Motion" in text
        assert "SHREE SAMARTH KRUPA" not in text
        assert not doc[0].search_for("PNCF")


def test_vertical_margin_fallback_detects_tall_legacy_block_without_direction():
    from pdf_branding.analysis import PageFeatures, find_vertical_text_rects

    features = PageFeatures(
        index=0,
        width=595.0,
        height=842.0,
        text_blocks=[
            {
                "type": 0,
                "bbox": (12.0, 505.0, 27.0, 790.0),
                "lines": [
                    {
                        # Deliberately omit dir: this mimics PDFs where rotated
                        # publisher text loses reliable direction metadata.
                        "spans": [{"text": "PNCF 2024-25 LIVE Module SET-1 Measurement and Motion", "size": 6.0}],
                    }
                ],
            }
        ],
        drawings=[],
        image_info=[],
        plain_text="PNCF 2024-25 LIVE Module SET-1 Measurement and Motion",
        blank=False,
        dark_top_ratio=0.0,
        top_lines=[],
        bottom_lines=[],
        bottom_visual_bands=[],
    )
    rects = find_vertical_text_rects(features, 68.0, ("PNCF", "LIVE Module", "SET-1"))
    assert len(rects) == 1
    assert rects[0].x1 <= 29.1
    assert rects[0].height > 280


def test_transparent_logo_assets_have_real_alpha():
    from PIL import Image

    for name in ("school_emblem_transparent.png", "school_emblem_white.png"):
        im = Image.open(ROOT / "assets" / name).convert("RGBA")
        alpha = im.getchannel("A")
        lo, hi = alpha.getextrema()
        assert lo == 0 and hi == 255
        # Corners must be fully transparent; no hidden square canvas.
        assert im.getpixel((0, 0))[3] == 0
        assert im.getpixel((im.width - 1, 0))[3] == 0
        assert im.getpixel((0, im.height - 1))[3] == 0


def test_designed_logo_background_uses_dominant_local_colour(tmp_path: Path):
    from pdf_branding.analysis import sample_local_background

    path = tmp_path / "flat_designed.pdf"
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    # Flat brown designed background, close to the real Key Points first page.
    bg = (70 / 255, 50 / 255, 26 / 255)
    p.draw_rect(p.rect, fill=bg, color=None)
    # Fake legacy logo artwork entirely inside the replacement box.  It must not
    # influence background estimation because the estimator samples outside it.
    r = fitz.Rect(3, 7, 107, 69)
    p.draw_rect((20, 20, 88, 48), fill=(1, 1, 1), color=None)
    p.draw_rect((30, 29, 80, 38), fill=(0.1, 0.2, 0.7), color=None)
    doc.save(path)
    doc.close()

    with fitz.open(path) as d:
        sampled = sample_local_background(d[0], r)
    got = tuple(round(c * 255) for c in sampled)
    assert max(abs(a - b) for a, b in zip(got, (70, 50, 26))) <= 3, got


def test_keypoints_fallback_logo_rect_is_tight(tmp_path: Path):
    path = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion_Key point.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.draw_rect(p.rect, fill=(70 / 255, 50 / 255, 26 / 255), color=None)
    # No searchable ALLEN: this forces the designed-page fallback box.
    p.draw_rect((18, 20, 95, 48), fill=(1, 1, 1), color=None)
    p.insert_text((300, 80), "Measurement & Motion", fontsize=22, fontname="hebo", color=(1, 0.9, 0.1))
    doc.save(path)
    doc.close()

    profile = load_profile(PROFILE)
    plan = analyze_pdf(path, tmp_path, profile)
    first = plan.pages[0]
    assert first.page_strategy == PageStrategy.DESIGNED_PAGE_LOGO_ONLY
    assert first.logo_replace_rect is not None
    # V3.4.3 tightens the old 20.5% x 10.7% fallback box.
    assert (first.logo_replace_rect.x1 - first.logo_replace_rect.x0) <= 595 * 0.18 + 0.01
    assert (first.logo_replace_rect.y1 - first.logo_replace_rect.y0) <= 842 * 0.082 + 0.01


def test_logo_only_render_does_not_create_flat_wrong_colour_box(tmp_path: Path):
    path = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion_Key point.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    bg8 = (70, 50, 26)
    bg = tuple(v / 255 for v in bg8)
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.draw_rect(p.rect, fill=bg, color=None)
    # Unsearchable pseudo-brand artwork in the fallback zone.
    p.draw_rect((18, 20, 95, 48), fill=(1, 1, 1), color=None)
    p.draw_rect((24, 27, 90, 41), fill=(0.1, 0.2, 0.7), color=None)
    p.insert_text((300, 80), "Measurement & Motion", fontsize=22, fontname="hebo", color=(1, 0.9, 0.1))
    doc.save(path)
    doc.close()

    profile = load_profile(PROFILE)
    out = tmp_path / "out" / "keypoints.pdf"
    result = process_pdf(path, out, tmp_path, profile)
    assert result.status == "completed", result.message

    with fitz.open(out) as rendered:
        page = rendered[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        # Sample a point inside the repaired rectangle but away from the centred
        # transparent emblem itself.  It should match the designed page background.
        x_pt, y_pt = 6.0, 10.0
        x, y = int(x_pt * 2), int(y_pt * 2)
        n = pix.n
        idx = (y * pix.width + x) * n
        got = tuple(pix.samples[idx + j] for j in range(3))
    assert max(abs(a - b) for a, b in zip(got, bg8)) <= 4, got


def make_textured_unsearchable_designed_logo_fixture(path: Path) -> None:
    """Raster legacy mark over a non-uniform designed background."""
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = 1190, 1684
    im = Image.new("RGB", (w, h))
    px = im.load()
    # Gentle dark gradient: a full-rectangle replacement is visibly wrong here.
    for y in range(h):
        for x in range(w):
            px[x, y] = (42 + x // 90, 50 + y // 120, 62 + x // 180)
    d = ImageDraw.Draw(im)
    # Raster-only white pseudo logo in the designed fallback region.
    d.rectangle((35, 32, 185, 70), fill=(245, 245, 245))
    d.rectangle((45, 80, 170, 105), fill=(235, 235, 235))
    raster = path.parent / "textured_logo_page.png"
    im.save(raster)

    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.insert_image(p.rect, filename=str(raster))
    p.insert_text((300, 115), "Measurement & Motion", fontsize=22, fontname="hebo", color=(1, 0.9, 0.1))
    doc.save(path)
    doc.close()


def test_designed_logo_cleanup_is_not_a_flat_rectangle(tmp_path: Path):
    path = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion_Key point.pdf"
    make_textured_unsearchable_designed_logo_fixture(path)
    profile = load_profile(PROFILE)
    out = tmp_path / "out" / "keypoints.pdf"
    result = process_pdf(path, out, tmp_path, profile)
    assert result.status == "completed", result.message

    with fitz.open(path) as src, fitz.open(out) as rendered:
        r = fitz.Rect(
            rendered[0].rect.width * 0.004,
            rendered[0].rect.height * 0.008,
            rendered[0].rect.width * 0.18,
            rendered[0].rect.height * 0.082,
        )
        before = src[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=r, alpha=False)
        after = rendered[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=r, alpha=False)
        # A point near the lower-right corner is background, not logo. It must stay
        # almost exactly as it was; v3.4.3 flattened the entire rectangle here.
        x, y = int(before.width * 0.90), int(before.height * 0.88)
        bi = (y * before.width + x) * before.n
        ai = (y * after.width + x) * after.n
        b = tuple(before.samples[bi + j] for j in range(3))
        a = tuple(after.samples[ai + j] for j in range(3))
        assert max(abs(u - v) for u, v in zip(a, b)) <= 8, (a, b)


def test_dark_faint_legacy_logo_pixels_are_removed_on_designed_page(tmp_path: Path):
    """Regression for the faint dark ALLEN ghost seen on real Key Points pages."""
    path = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion_Key point.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    bg8 = (70, 50, 26)
    bg = tuple(v / 255 for v in bg8)
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.draw_rect(p.rect, fill=bg, color=None)
    # Faint dark pseudo-logo components. Difference from background is deliberately
    # modest, matching the residual that V3.4.4 failed to clean completely.
    dark = (49 / 255, 34 / 255, 18 / 255)
    p.draw_rect((8, 22, 21, 50), fill=dark, color=None)
    p.draw_rect((87, 22, 99, 50), fill=dark, color=None)
    # Border close to the fallback rectangle's right edge must survive.
    p.draw_line((105.5, 8), (105.5, 68), color=(0.85, 0.72, 0.35), width=1.0)
    p.insert_text((300, 80), "Measurement & Motion", fontsize=22, fontname="hebo", color=(1, 0.9, 0.1))
    doc.save(path)
    doc.close()

    profile = load_profile(PROFILE)
    out = tmp_path / "out" / "keypoints.pdf"
    result = process_pdf(path, out, tmp_path, profile)
    assert result.status == "completed", result.message

    with fitz.open(out) as rendered:
        page = rendered[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
        def rgb_at(x_pt: float, y_pt: float):
            x, y = int(x_pt * 3), int(y_pt * 3)
            i = (y * pix.width + x) * pix.n
            return tuple(pix.samples[i + j] for j in range(3))

        # Old dark logo pixels outside the centred replacement emblem should be
        # restored to the designed background rather than remain faintly visible.
        for pt in ((12.0, 35.0), (94.0, 35.0)):
            got = rgb_at(*pt)
            assert max(abs(a - b) for a, b in zip(got, bg8)) <= 8, (pt, got)

        # The nearby page rule is outside the active core/protected by the frame;
        # it must not be painted over with the background colour.
        border = rgb_at(105.5, 35.0)
        assert max(abs(a - b) for a, b in zip(border, bg8)) >= 20, border


def test_flat_field_rebuild_removes_low_contrast_allen_shadow(tmp_path: Path):
    """Regression for the real Key Points ALLEN shadow that remained in V3.4.5."""
    path = tmp_path / "6th class" / "science" / "07 Measurement and Motion" / "Measurement and Motion_Key point.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    bg8 = (71, 51, 26)
    bg = tuple(v / 255 for v in bg8)
    shadow8 = (66, 53, 42)  # sampled from the user's visible residual ALLEN shadow
    shadow = tuple(v / 255 for v in shadow8)

    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.draw_rect(p.rect, fill=bg, color=None)
    # Low-contrast left / right pseudo-letters outside the centred new emblem.
    p.draw_rect((8, 22, 23, 50), fill=shadow, color=None)
    p.draw_rect((86, 22, 100, 50), fill=shadow, color=None)
    # Bright legacy core makes the fixture look like a normal old logo region.
    p.draw_rect((34, 24, 74, 46), fill=(0.97, 0.97, 0.97), color=None)
    # A nearby edge rule inside the fallback box's protected frame must survive.
    p.draw_line((105.5, 8), (105.5, 68), color=(0.85, 0.72, 0.35), width=1.0)
    p.insert_text((300, 80), "Measurement & Motion", fontsize=22, fontname="hebo", color=(1, 0.9, 0.1))
    doc.save(path)
    doc.close()

    profile = load_profile(PROFILE)
    out = tmp_path / "out" / "keypoints.pdf"
    result = process_pdf(path, out, tmp_path, profile)
    assert result.status == "completed", result.message

    with fitz.open(out) as rendered:
        page = rendered[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)

        def rgb_at(x_pt: float, y_pt: float):
            x, y = int(x_pt * 3), int(y_pt * 3)
            i = (y * pix.width + x) * pix.n
            return tuple(pix.samples[i + j] for j in range(3))

        for pt in ((14.0, 35.0), (94.0, 35.0)):
            got = rgb_at(*pt)
            assert max(abs(a - b) for a, b in zip(got, bg8)) <= 5, (pt, got)

        border = rgb_at(105.5, 35.0)
        assert max(abs(a - b) for a, b in zip(border, bg8)) >= 20, border
