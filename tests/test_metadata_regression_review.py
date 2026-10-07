from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_branding.config import load_profile
from pdf_branding.engine import analyze_pdf
from pdf_branding.metadata import build_meta, infer_class, infer_material_type, quick_meta
from pdf_branding.models import DocumentPlan, DocumentProfile, DocumentType, HeaderTemplate, PagePlan, PageStrategy, PageType, RectData, RenderStrategy, RepeatedBands
from pdf_branding.preview import render_review_bundle, select_review_pages
from pdf_branding.regression import build_inventory, page_layout_flags, run_matrix, select_representatives


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "sskem.json"


def make_pdf(path: Path, text: str = "Academic question about measurement and motion.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((80, 200), text)
        doc.save(path)


@pytest.mark.parametrize("class_number", [6, 7, 8, 9, 10])
def test_explicit_class_folder_beats_chapter_and_external_numbers(tmp_path: Path, class_number: int):
    root = tmp_path / "class 10" / "library"
    source = root / f"{class_number}th class" / "science" / "10 Sound" / "Test 9.pdf"
    assert infer_class(source, root) == str(class_number)


def test_bare_chapter_number_is_not_class(tmp_path: Path):
    source = tmp_path / "science" / "10 Sound" / "notes.pdf"
    assert infer_class(source, tmp_path) is None


def test_class_folder_itself_can_be_input_root(tmp_path: Path):
    root = tmp_path / "6th class"
    source = root / "science" / "10 Fun with Magnets" / "notes.pdf"
    meta = quick_meta(source, root)
    assert meta and meta.class_name == "6" and meta.subject == "physics"


@pytest.mark.parametrize("filename, material", [
    ("topic_Key point.pdf", "KEY POINTS"), ("Chapter Test_topic.pdf", "CHAPTER TEST"),
    ("Coordinate Geometry_Keypoint.pdf", "KEY POINTS"),
    ("Practice Test_topic.pdf", "PRACTICE TEST"), ("Practice Sheet_topic.pdf", "PRACTICE SHEET"),
    ("NCERT Practice_topic.pdf", "NCERT PRACTICE"), ("NCERT Question solutions_topic.pdf", "NCERT SOLUTIONS"),
    ("Practice Exercise_topic.pdf", "PRACTICE EXERCISE"), ("Exercise solutions_topic.pdf", "EXERCISE"),
    ("Race solutions_topic.pdf", "DPP"), ("Test solutions_topic.pdf", "TEST"), ("topic.pdf", "NOTES"),
])
def test_material_filename_families(filename: str, material: str):
    assert infer_material_type(Path(filename)) == material


def test_unsorted_requires_actual_recorded_content(tmp_path: Path):
    source = tmp_path / "9th class" / "science" / "Unsorted" / "original (1).pdf"
    make_pdf(source, "Biology 9th Tissues Practice Sheet")
    meta = quick_meta(source, tmp_path)
    assert meta and (meta.class_name, meta.subject, meta.chapter, meta.material_type) == ("9", "biology", "Tissues", "PRACTICE SHEET")
    # A source with the same reviewed filename but incompatible contents must
    # remain unresolved rather than inherit the trusted classification.
    with fitz.open() as unrelated:
        unrelated.new_page().insert_text((60, 180), "Physics Motion Test")
        assert build_meta(source, tmp_path, unrelated) is None


def test_review_selection_never_hides_failures_after_page_eight():
    meta = quick_meta(ROOT / "foundation_notes/6th class/science/07 Measurement and Motion/Measurement and Motion.pdf", ROOT / "foundation_notes")
    assert meta
    pages = [PagePlan(i + 1, PageType.STANDARD, RenderStrategy.OVERLAY,
                      PageStrategy.STANDARD_HEADER_FOOTER_REPLACEMENT, HeaderTemplate.CHAPTER, .95)
             for i in range(24)]
    pages[22].confidence = .70
    plan = DocumentPlan(Path("source.pdf"), meta, DocumentProfile(Path("source.pdf"), DocumentType.NOTES, .9), RepeatedBands(), pages)
    selected = select_review_pages(plan, {"issues": [{"severity": "error", "page_number": 19}]}, 8)
    assert {18, 22}.issubset(selected)
    assert selected != list(range(8))


def test_layout_flags_identify_dense_grid_and_scanned_pages():
    from PIL import Image
    import io
    with fitz.open() as doc:
        dense = doc.new_page(width=595, height=842)
        for row in range(40):
            dense.insert_text((40, 80 + row * 15), "word " * 14, fontsize=8)
        for offset in range(5):
            dense.draw_line((40, 80 + offset * 30), (450, 80 + offset * 30))
            dense.draw_line((40 + offset * 80, 80), (40 + offset * 80, 200))
        # Extra lines make a real grid geometry, rather than just a page border.
        dense.draw_line((40, 230), (450, 230))
        dense.draw_line((440, 80), (440, 230))
        flags = page_layout_flags(dense)["flags"]
        assert {"dense_text", "tables"}.issubset(flags)
        image = Image.new("RGB", (100, 100), "#abcdef")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        scanned = doc.new_page()
        scanned.insert_image(scanned.rect, stream=stream.getvalue())
        assert "image_only" in page_layout_flags(scanned)["flags"]


def test_inventory_relative_ids_hashes_and_coverage(tmp_path: Path):
    root = tmp_path / "source"
    make_pdf(root / "6th class/science/07 Measurement and Motion/Notes.pdf")
    make_pdf(root / "9th class/math/01 Number Systems/Test.pdf")
    manifest = build_inventory(root, tmp_path / "inventory.json")
    assert manifest["summary"]["documents"] == 2
    for row in manifest["documents"]:
        assert not Path(row["source_id"]).is_absolute()
        assert row["sha256"] == hashlib.sha256((root / row["source_id"]).read_bytes()).hexdigest()
    representatives = select_representatives(manifest)
    assert len(representatives) == 2
    assert any("class_subject:9/mathematics" in row["covers"] for row in representatives)
    assert (tmp_path / "inventory.md").is_file()


def test_matrix_refuses_output_inside_source(tmp_path: Path):
    with pytest.raises(ValueError, match="separate"):
        run_matrix(tmp_path, tmp_path / "outputs", load_profile(PROFILE), {"representatives": []})


def test_review_bundle_relative_links_and_flagged_page(tmp_path: Path):
    source = tmp_path / "6th class/science/07 Measurement and Motion/Measurement and Motion.pdf"
    make_pdf(source)
    plan = analyze_pdf(source, tmp_path, load_profile(PROFILE))
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    review = render_review_bundle(source, plan, tmp_path / "review", qa={"passed": False, "issues": [{"page_number": 1, "severity": "error", "code": "TEST_FLAG", "message": "Review this page"}]})
    content = Path(review["index_html"]).read_text(encoding="utf-8")
    assert 'src="page_001_source.png"' in content
    assert "QA FAILED" in content and "TEST_FLAG" in content
    assert Path(review["contact_sheets"][0]).is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash


def test_real_library_reviewed_unsorted_metadata():
    root = Path(os.environ.get("PDF_BRANDING_FIXTURE_ROOT", ROOT / "foundation_notes"))
    source = root / "9th class/science/Unsorted/original (1).pdf"
    if not source.exists():
        pytest.skip("Real library unavailable; set PDF_BRANDING_FIXTURE_ROOT to validate reviewed source metadata")
    meta = quick_meta(source, root)
    assert meta and meta.chapter == "Tissues" and meta.subject == "biology" and meta.material_type == "PRACTICE SHEET"


def test_real_representative_fixture_references():
    if os.environ.get("PDF_BRANDING_REAL_TESTS") != "1":
        pytest.skip("Set PDF_BRANDING_REAL_TESTS=1 to verify all real fixture hashes and family/layout coverage")
    root = Path(os.environ.get("PDF_BRANDING_FIXTURE_ROOT", ROOT / "foundation_notes"))
    fixture_file = Path(__file__).parent / "fixtures" / "real_matrix.json"
    if not fixture_file.exists() or not root.is_dir():
        pytest.skip("Real reference fixture inventory unavailable; build an inventory for this checkout")
    fixtures = json.loads(fixture_file.read_text(encoding="utf-8"))
    families = set()
    classes = set()
    subjects = set()
    for row in fixtures["sources"]:
        source = root / row["source_id"]
        assert source.is_file(), source
        assert hashlib.sha256(source.read_bytes()).hexdigest() == row["sha256"], source
        with fitz.open(source) as doc:
            meta = build_meta(source, root, doc)
            assert meta
            classes.add(meta.class_name)
            subjects.add(meta.subject)
        families.add(row["family"])
    assert classes == {"6", "7", "8", "9", "10"}
    assert subjects == {"mathematics", "physics", "chemistry", "biology"}
    assert {"notes", "key_points", "dpp", "ncert", "exercise", "test", "practice_sheet"}.issubset(families)
