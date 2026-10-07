"""Opt-in end-to-end tests using immutable references to the real library."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_branding.config import load_profile
from pdf_branding.engine import process_pdf
from pdf_branding.qa import run_qa

PROJECT = Path(__file__).parents[1]
FIXTURES = json.loads((Path(__file__).parent / "fixtures/real_matrix.json").read_text(encoding="utf-8"))["sources"]


@pytest.mark.skipif(os.environ.get("PDF_BRANDING_REAL_TESTS") != "1", reason="Set PDF_BRANDING_REAL_TESTS=1 for real-PDF end-to-end regression")
@pytest.mark.parametrize("fixture", FIXTURES, ids=[row["source_id"] for row in FIXTURES])
def test_real_family_preserves_source_content_and_passes_independent_qa(tmp_path, fixture):
    root = Path(os.environ.get("PDF_BRANDING_FIXTURE_ROOT", PROJECT / "foundation_notes"))
    source = root / fixture["source_id"]
    if not root.is_dir():
        pytest.skip("Real source library is unavailable")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert before == fixture["sha256"]
    profile = load_profile(PROJECT / "profiles/sskem.json")
    result = process_pdf(source, tmp_path / "output.pdf", root, profile, tmp_path / "reports", Path(source.name))
    assert result.status == "completed", [(issue.code, issue.page_number) for issue in result.qa.issues] if result.qa else result.message
    assert result.publication_committed
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with fitz.open(source) as original:
        independent = run_qa(original, result.output, result.plan, profile)
        assert independent.passed, [(issue.code, issue.page_number) for issue in independent.issues]
        assert independent.page_count_output == fixture["page_count"]
        assert independent.metrics["rendered_pages_checked"] == len(original)
