from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from .models import DocumentPlan, RectData


def _rect(r: RectData | None) -> fitz.Rect | None:
    return fitz.Rect(*r.as_tuple()) if r else None


def _draw_label(page: fitz.Page, x: float, y: float, text: str) -> None:
    page.insert_text((x, y), text, fontsize=7, fontname="hebo", color=(0.7, 0.0, 0.0), overlay=True)


def render_plan_previews(source: Path, plan: DocumentPlan, output_dir: Path, max_pages: int = 8) -> list[Path]:
    """Render annotated PNG previews without changing the source PDF.

    Legacy cleanup regions are red, the compact replacement header is green, footer
    regions blue, logo-only regions purple, and vertical text cleanup regions orange.
    These previews are for approval before render.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    chosen = []
    # Always include first two pages, low-confidence pages, designed pages and pages with unusual strategies.
    for i, p in enumerate(plan.pages):
        if i < 2 or p.confidence < 0.82 or p.logo_replace_rect or p.strategy.value != "overlay":
            chosen.append(i)
    # Fill remaining slots evenly from the document.
    if len(chosen) < max_pages and plan.pages:
        for i in range(0, len(plan.pages), max(1, len(plan.pages) // max_pages)):
            if i not in chosen:
                chosen.append(i)
            if len(chosen) >= max_pages:
                break
    chosen = sorted(set(chosen))[:max_pages]

    outputs: list[Path] = []
    with fitz.open(source) as src:
        for i in chosen:
            one = fitz.open()
            one.insert_pdf(src, from_page=i, to_page=i)
            page = one[0]
            pp = plan.pages[i]
            for rdata in pp.cleanup_rects:
                r = _rect(rdata)
                page.draw_rect(r, color=(0.85, 0.05, 0.05), width=1.4, overlay=True)
                _draw_label(page, r.x0 + 2, max(8, r.y0 + 9), "legacy cleanup")
            hr = _rect(pp.header_rect)
            if hr:
                page.draw_rect(hr, color=(0.0, 0.55, 0.25), width=1.4, overlay=True)
                page.insert_text((hr.x1 - 78, max(8, hr.y0 + 9)), "new header", fontsize=7, fontname="hebo", color=(0.0, 0.45, 0.20), overlay=True)
            for rdata in pp.vertical_text_rects:
                r = _rect(rdata)
                page.draw_rect(r, color=(0.95, 0.45, 0.0), width=1.2, overlay=True)
            r = _rect(pp.footer_rect)
            if r:
                page.draw_rect(r, color=(0.0, 0.35, 0.85), width=1.2, overlay=True)
                _draw_label(page, r.x0 + 2, r.y0 + 9, "footer")
            r = _rect(pp.logo_replace_rect)
            if r:
                page.draw_rect(r, color=(0.5, 0.1, 0.7), width=1.5, overlay=True)
                _draw_label(page, r.x0 + 2, max(8, r.y0 + 9), "logo replace")
            _draw_label(page, 12, page.rect.height - 8, f"V3.4.6: {pp.page_strategy.value} / {pp.strategy.value} | confidence {pp.confidence:.2f}")
            pix = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
            out = output_dir / f"page_{i+1:03d}_plan.png"
            pix.save(out)
            outputs.append(out)
            one.close()
    return outputs
