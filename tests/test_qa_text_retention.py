from __future__ import annotations

from collections import Counter
from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_branding.config import load_profile
from pdf_branding.models import (
    DocumentMeta,
    DocumentPlan,
    DocumentProfile,
    DocumentType,
    HeaderTemplate,
    PagePlan,
    PageStrategy,
    PageType,
    RectData,
    RenderStrategy,
    RepeatedBands,
)
from pdf_branding.qa import _body_words, run_qa


PROFILE = Path(__file__).parents[1] / "profiles" / "sskem.json"


def _page_plan(**kwargs) -> PagePlan:
    return PagePlan(
        page_number=1,
        page_type=PageType.STANDARD,
        strategy=RenderStrategy.OVERLAY,
        page_strategy=PageStrategy.STANDARD_HEADER_FOOTER_REPLACEMENT,
        header_template=HeaderTemplate.TEST,
        confidence=1.0,
        **kwargs,
    )


def _plan(source: Path, page: PagePlan) -> DocumentPlan:
    return DocumentPlan(
        source=source,
        meta=DocumentMeta("6", "physics", "Measurement and Motion", "07", "DPP", 1),
        profile=DocumentProfile(source, DocumentType.DPP, 1.0),
        repeated_bands=RepeatedBands(),
        pages=[page],
    )


def _copy(source: fitz.Document) -> fitz.Document:
    output = fitz.open()
    output.insert_pdf(source)
    return output


@pytest.mark.parametrize("ordinal", [10, 19])
def test_superscript_ordinal_is_retained_as_one_word(tmp_path: Path, ordinal: int):
    brand = load_profile(PROFILE)
    page_plan = _page_plan()
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        prefix = f"Developed in the {ordinal}"
        page.insert_text((40, 100), prefix, fontsize=12)
        x = 40 + fitz.get_text_length(prefix, fontsize=12)
        page.insert_text((x, 97), "th", fontsize=8)
        x += fitz.get_text_length("th", fontsize=8)
        page.insert_text((x, 100), " century.", fontsize=12)
        assert f"{ordinal}th" in {word[4] for word in page.get_text("words")}
        assert _body_words(page, page_plan, brand.legacy_terms)[f"{ordinal}th"] == 1

        output_path = tmp_path / "ordinal.pdf"
        with _copy(source) as output:
            output.save(output_path, garbage=4, deflate=True, clean=True)
        report = run_qa(source, output_path, _plan(tmp_path / "source.pdf", page_plan), brand)

    assert report.passed, report.issues
    assert report.metrics["average_body_text_retention"] == 1.0


def test_word_spanning_font_change_is_retained(tmp_path: Path):
    brand = load_profile(PROFILE)
    page_plan = _page_plan()
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.insert_text((40, 100), "centi", fontsize=12)
        x = 40 + fitz.get_text_length("centi", fontsize=12)
        page.insert_text((x, 100), "metres", fontsize=12, fontname="hebo")
        assert "centimetres" in {word[4] for word in page.get_text("words")}
        assert _body_words(page, page_plan, brand.legacy_terms) == Counter({"centimetres": 1})

        output_path = tmp_path / "font-change.pdf"
        with _copy(source) as output:
            output.save(output_path, garbage=4, deflate=True, clean=True)
        report = run_qa(source, output_path, _plan(tmp_path / "source.pdf", page_plan), brand)

    assert report.passed, report.issues
    assert report.metrics["average_body_text_retention"] == 1.0


@pytest.mark.parametrize("branding_y", [28, 818])
def test_generated_branding_cannot_mask_deleted_body_word(tmp_path: Path, branding_y: int):
    brand = load_profile(PROFILE)
    page_plan = _page_plan(
        header_rect=RectData(0, 0, 595, 50),
        footer_rect=RectData(0, 780, 595, 842),
    )
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.insert_text((40, 100), "Measurement", fontsize=12)
        page.insert_text((40, 130), "An academic question stays here.", fontsize=12)
        output_path = tmp_path / "deleted-word.pdf"
        with _copy(source) as output:
            target = output[0]
            target.add_redact_annot(target.search_for("Measurement")[0])
            target.apply_redactions()
            target.insert_text((40, branding_y), "Measurement", fontsize=12)
            output.save(output_path)
        report = run_qa(source, output_path, _plan(tmp_path / "source.pdf", page_plan), brand)

    assert not report.passed
    assert any(issue.code == "BODY_TEXT_LOSS" and issue.page_number == 1 for issue in report.issues)
    assert report.metrics["average_body_text_retention"] < brand.qa["min_body_text_retention"]


@pytest.mark.parametrize("delete_body", [False, True])
def test_body_in_block_touching_cleanup_is_still_checked(tmp_path: Path, delete_body: bool):
    brand = load_profile(PROFILE)
    body = "Academic words below the header must survive."
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.insert_text((40, 50), ["Old header", body], fontsize=12, lineheight=1.4)
        blocks = page.get_text("dict")["blocks"]
        assert len(blocks) == 1 and len(blocks[0]["lines"]) == 2
        header_line, body_line = blocks[0]["lines"]
        cutoff = (header_line["bbox"][3] + body_line["bbox"][1]) / 2
        cleanup = RectData(0, 0, 595, cutoff)
        page_plan = _page_plan(cleanup_rects=[cleanup])
        assert sum(_body_words(page, page_plan, brand.legacy_terms).values()) == 7

        output_path = tmp_path / "mixed-block.pdf"
        with _copy(source) as output:
            target = output[0]
            target.add_redact_annot(fitz.Rect(*cleanup.as_tuple()))
            if delete_body:
                target.add_redact_annot(fitz.Rect(body_line["bbox"]))
            target.apply_redactions()
            output.save(output_path)
        report = run_qa(source, output_path, _plan(tmp_path / "source.pdf", page_plan), brand)

    if delete_body:
        assert not report.passed
        assert any(issue.code == "BODY_TEXT_LOSS" for issue in report.issues)
    else:
        assert report.passed, report.issues
        assert report.metrics["average_body_text_retention"] == 1.0
