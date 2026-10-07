from pathlib import Path

import pymupdf as fitz

from pdf_branding.benchmark import choose_workers, compare_runs, render_signature


def test_render_signature_ignores_pdf_identity_but_catches_missing_content(tmp_path: Path):
    first, clone, changed = [tmp_path / name for name in ("first.pdf", "clone.pdf", "changed.pdf")]
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 100), "Academic diagram and explanation")
        document.save(first)
        document.save(clone)
    with fitz.open() as document:
        document.new_page()
        document.save(changed)
    assert render_signature(first) == render_signature(clone)
    assert render_signature(first) != render_signature(changed)
    baseline = {"source": {"status": "completed", "signature": render_signature(first)}}
    assert compare_runs(baseline, {"source": {"status": "completed", "signature": render_signature(clone)}}) == []
    assert compare_runs(baseline, {"source": {"status": "completed", "signature": render_signature(changed)}})[0]["code"] == "RENDER_CHANGED"


def test_worker_recommendation_rejects_failed_or_memory_heavy_runs():
    def row(workers, seconds, memory=100, code=0, issues=None):
        return dict(workers=workers, elapsed_seconds=seconds, peak_process_tree_rss_mb=memory,
                    exit_code=code, integrity_issues=issues or [])
    assert choose_workers([row(1, 100), row(2, 80), row(4, 75), row(8, 50, code=2)], 1000) == 2
    assert choose_workers([row(1, 100), row(4, 40, memory=600)], 1000) == 1
    assert choose_workers([row(4, 30, issues=[{"code": "RENDER_CHANGED"}])], 1000) == 1
