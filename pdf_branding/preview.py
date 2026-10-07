from __future__ import annotations

from pathlib import Path
import html
import json
from typing import Any

from PIL import Image, ImageDraw

import pymupdf as fitz

from .models import DocumentPlan, QAReport, RectData
from . import __version__


def _rect(r: RectData | None) -> fitz.Rect | None:
    return fitz.Rect(*r.as_tuple()) if r else None


def _draw_label(page: fitz.Page, x: float, y: float, text: str) -> None:
    page.insert_text((x, y), text, fontsize=7, fontname="hebo", color=(0.7, 0.0, 0.0), overlay=True)


def render_plan_previews(source: Path, plan: DocumentPlan, output_dir: Path, max_pages: int = 8, *, page_indexes: list[int] | None = None) -> list[Path]:
    """Render annotated PNG previews without changing the source PDF.

    Legacy cleanup regions are red, the compact replacement header is green, footer
    regions blue, logo-only regions purple, and vertical text cleanup regions orange.
    These previews are for approval before render.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    chosen = page_indexes if page_indexes is not None else select_review_pages(plan, max_pages=max_pages)

    outputs: list[Path] = []
    with fitz.open(source) as src:
        for i in chosen:
            # Annotate the source document only in memory, never save it. A
            # page-only insert_pdf clone can omit root ICC / OutputIntent
            # dictionaries and visibly change a designed page's colors.
            page = src[i]
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
            for rdata in ([pp.logo_replace_rect] if pp.logo_replace_rect else []) + pp.additional_logo_rects:
                r = _rect(rdata)
                page.draw_rect(r, color=(0.5, 0.1, 0.7), width=1.5, overlay=True)
                _draw_label(page, r.x0 + 2, max(8, r.y0 + 9), "logo replace")
            crop = _rect(pp.source_clip_rect)
            if crop:
                page.draw_rect(crop, color=(.8, .5, 0), width=1.4, dashes="[5 3]", overlay=True)
                _draw_label(page, crop.x0 + 4, crop.y0 + 12, "retained source crop")
            _draw_label(page, 12, page.rect.height - 8, f"V{__version__}: {pp.page_strategy.value} / {pp.strategy.value} | confidence {pp.confidence:.2f}")
            pix = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
            out = output_dir / f"page_{i+1:03d}_plan.png"
            pix.save(out)
            outputs.append(out)
    return outputs


def _qa_data(qa: QAReport | dict | None) -> dict:
    return qa.to_dict() if hasattr(qa, "to_dict") else (qa or {})


def select_review_pages(plan: DocumentPlan, qa: QAReport | dict | None = None, max_pages: int = 8) -> list[int]:
    """Return zero-based indexes; mandatory risky pages are never truncated.

    max_pages budgets representative pages, not QA failures or low-confidence
    pages. A page-19 failure cannot be hidden behind the first eight previews.
    """
    if not plan.pages:
        return []
    mandatory = {i for i, page in enumerate(plan.pages) if page.confidence < 0.82}
    for issue in _qa_data(qa).get("issues", []):
        number = issue.get("page_number")
        if isinstance(number, int) and 1 <= number <= len(plan.pages):
            mandatory.add(number - 1)
    selected = mandatory | {0}
    if len(plan.pages) > 1:
        selected.add(1)
    target = max(1, max_pages)
    seen = {(plan.pages[i].strategy.value, plan.pages[i].page_type.value) for i in selected}
    for i, page in enumerate(plan.pages):
        signature = (page.strategy.value, page.page_type.value)
        if signature not in seen and len(selected) < target:
            selected.add(i)
            seen.add(signature)
    if len(selected) < target:
        for slot in range(target):
            selected.add(round(slot * (len(plan.pages) - 1) / max(1, target - 1)))
    return sorted(selected)


def _page_notes(plan: DocumentPlan, qa: dict, index: int) -> list[str]:
    notes = []
    page = plan.pages[index]
    if page.confidence < 0.82:
        notes.append(f"Low confidence: {page.confidence:.2f}")
    for issue in qa.get("issues", []):
        if issue.get("page_number") == index + 1:
            notes.append(f"{str(issue.get('severity', 'warning')).upper()}: {issue.get('code', '')} - {issue.get('message', '')}")
    return notes


def render_review_bundle(
    source: Path, plan: DocumentPlan, output_dir: Path,
    final: Path | None = None, qa: QAReport | dict | None = None,
    max_pages: int = 8,
) -> dict[str, Any]:
    """Create source/annotated-plan/final PNGs, contact sheets and an HTML index.

    PDFs are read only; annotation uses disposable in-memory pages. Image links
    in the review index are relative, so a review folder can be moved or shared.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    indexes = select_review_pages(plan, qa, max_pages)
    report = _qa_data(qa)
    render_plan_previews(source, plan, output_dir, max_pages, page_indexes=indexes)
    rows = []
    with fitz.open(source) as original:
        final_doc = fitz.open(final) if final and Path(final).is_file() else None
        try:
            for i in indexes:
                src_name = f"page_{i+1:03d}_source.png"
                original[i].get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False).save(output_dir / src_name)
                final_name = None
                if final_doc is not None and i < len(final_doc):
                    final_name = f"page_{i+1:03d}_final.png"
                    final_doc[i].get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False).save(output_dir / final_name)
                rows.append({"page_number": i + 1, "source": src_name,
                             "plan": f"page_{i+1:03d}_plan.png", "final": final_name,
                             "confidence": plan.pages[i].confidence,
                             "strategy": plan.pages[i].page_strategy.value,
                             "notes": _page_notes(plan, report, i)})
        finally:
            if final_doc is not None:
                final_doc.close()
    sheets = []
    for first in range(0, len(rows), 4):
        group = rows[first:first + 4]
        sheet = Image.new("RGB", (1320, 1840), "#eef2f6")
        draw = ImageDraw.Draw(sheet)
        for r, row in enumerate(group):
            top = r * 460
            issue_color = "#b42318" if row["notes"] else "#16324f"
            draw.text((14, top + 7), f"Page {row['page_number']} | {row['strategy']} | confidence {row['confidence']:.2f}", fill=issue_color)
            if row["notes"]:
                draw.text((14, top + 23), row["notes"][0][:175], fill=issue_color)
            for col, key in enumerate(("source", "plan", "final")):
                draw.text((col * 440 + 12, top + 41), key.upper(), fill="#16324f")
                filename = row[key]
                if filename:
                    with Image.open(output_dir / filename) as raw:
                        thumb = raw.convert("RGB")
                        thumb.thumbnail((420, 395))
                        sheet.paste(thumb, (col * 440 + 12, top + 60))
                else:
                    draw.text((col * 440 + 12, top + 75), "Final PDF not available (analysis only)", fill="#555555")
        filename = f"contact_{first//4+1:03d}.png"
        sheet.save(output_dir / filename)
        sheets.append(filename)
    e = html.escape
    cards = []
    for row in rows:
        notes = ''.join(f"<li>{e(note)}</li>" for note in row["notes"])
        panels = ''.join(f'<figure><figcaption>{key.title()}</figcaption><a href="{e(row[key])}"><img loading="lazy" src="{e(row[key])}" alt="Page {row["page_number"]} {key}"></a></figure>'
                         if row[key] else '<figure><figcaption>Final</figcaption><p>Analysis only</p></figure>'
                         for key in ("source", "plan", "final"))
        cards.append(f'<section class="{"flagged" if notes else ""}" id="page-{row["page_number"]}"><h2>Page {row["page_number"]} - {e(row["strategy"])} (confidence {row["confidence"]:.2f})</h2><ul>{notes}</ul><div class="panels">{panels}</div></section>')
    status = "QA FAILED" if report.get("passed") is False else ("QA passed" if report.get("passed") else "Analysis preview")
    sheet_links = ' '.join(f'<a href="{e(name)}">Contact sheet {i+1}</a>' for i, name in enumerate(sheets))
    index = output_dir / "index.html"
    index.write_text(f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PDF review - {e(source.name)}</title>
<style>body{{font:16px system-ui;margin:24px;color:#16324f;background:#f5f7fa}}h1{{font-size:24px}}section{{background:white;padding:16px;margin:20px 0;border:2px solid #dbe3ed}}section.flagged{{border-color:#c12d20}}.panels{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}}figure{{margin:0}}figcaption{{font-weight:700;padding:8px}}img{{width:100%;height:auto}}a{{color:#1254a4}}li{{color:#a22519}}@media(max-width:720px){{.panels{{grid-template-columns:1fr}}}}</style>
<h1>{e(source.name)}</h1><p><strong>{status}</strong> - {len(rows)} of {len(plan.pages)} pages selected. All QA issue pages and low-confidence pages are included.</p><p>Plan key: red cleanup, green replacement header, blue footer, purple logo, orange vertical text.</p><nav>{sheet_links}</nav>{''.join(cards)}</html>''', encoding="utf-8")
    data = {"schema_version": 1, "source": str(source), "final": str(final) if final else None,
            "qa_passed": report.get("passed"), "index_html": str(index),
            "contact_sheets": [str(output_dir / name) for name in sheets], "pages": rows}
    (output_dir / "review.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
