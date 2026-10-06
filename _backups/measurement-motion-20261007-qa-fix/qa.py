from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pymupdf as fitz

from .config import BrandingProfile
from .metadata import norm
from .models import DocumentPlan, PageStrategy, QAIssue, QAReport, RectData, Severity


def _intersects(a: fitz.Rect, b: RectData) -> bool:
    return not (a & fitz.Rect(*b.as_tuple())).is_empty


def _excluded_rects(plan_page) -> list[RectData]:
    out = list(plan_page.cleanup_rects) + list(plan_page.vertical_text_rects)
    if plan_page.footer_rect:
        out.append(plan_page.footer_rect)
    if plan_page.logo_replace_rect:
        out.append(plan_page.logo_replace_rect)
    return out


def _body_words(page: fitz.Page, plan_page, legacy_terms: tuple[str, ...]) -> Counter[str]:
    excluded = _excluded_rects(plan_page)
    words: list[str] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return Counter()
    legacy_n = {norm(t) for t in legacy_terms}
    for block in blocks:
        if block.get("type") != 0:
            continue
        r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
        if any(_intersects(r, ex) for ex in excluded):
            continue
        text = " ".join(
            span.get("text", "")
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        )
        for word in re.findall(r"[A-Za-z0-9]+", text.lower()):
            if norm(word) not in legacy_n:
                words.append(word)
    return Counter(words)

def _retention(source: Counter[str], output: Counter[str]) -> float:
    total = sum(source.values())
    if total == 0:
        return 1.0
    return sum((source & output).values()) / total


def _body_anchors(page: fitz.Page, plan_page, max_items: int = 4) -> list[tuple[str, float]]:
    excluded = _excluded_rects(plan_page)
    candidates: list[tuple[str, float]] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return candidates
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = " ".join(s.get("text", "").strip() for s in spans if s.get("text", "").strip()).strip()
            if len(text) < 12 or len(text) > 100:
                continue
            r = fitz.Rect(line.get("bbox", block.get("bbox", (0, 0, 0, 0))))
            if any(_intersects(r, ex) for ex in excluded):
                continue
            # Avoid generic short labels that may repeat many times.
            if norm(text) in {"solution", "instructions", "physics", "chemistry", "biology", "mathematics"}:
                continue
            candidates.append((text, r.y0))
    # Spread anchors through the page rather than using only the first paragraph.
    if len(candidates) <= max_items:
        return candidates
    indexes = sorted({0, len(candidates) // 3, (2 * len(candidates)) // 3, len(candidates) - 1})
    return [candidates[i] for i in indexes[:max_items]]


def _legacy_side_hits(page: fitz.Page, terms: tuple[str, ...], side_width: float) -> list[str]:
    found: list[str] = []
    for term in terms:
        try:
            rects = page.search_for(term)
        except Exception:
            rects = []
        if any(r.x1 <= side_width or r.x0 >= page.rect.width - side_width for r in rects):
            found.append(term)
    return found


def run_qa(source_doc: fitz.Document, output_path: Path, plan: DocumentPlan, brand: BrandingProfile) -> QAReport:
    issues: list[QAIssue] = []
    try:
        out = fitz.open(output_path)
    except Exception as exc:
        return QAReport(
            source=str(plan.source), output=str(output_path), passed=False,
            page_count_source=len(source_doc), page_count_output=0,
            issues=[QAIssue(Severity.FATAL, "OUTPUT_OPEN_FAILED", f"Output PDF could not be opened: {exc}")],
        )

    with out:
        if len(out) != len(source_doc):
            issues.append(QAIssue(Severity.FATAL, "PAGE_COUNT_CHANGED", f"Source has {len(source_doc)} pages but output has {len(out)} pages"))

        min_retention = float(brand.qa.get("min_body_text_retention", 0.985))
        max_shift = float(brand.qa.get("max_body_position_shift_pt", 2.5))
        size_tol = float(brand.qa.get("page_size_tolerance_pt", 0.5))
        side_width = float(brand.layout("side_text_width", 68))
        retention_values: list[float] = []
        legacy_hits = 0
        shift_checks = 0
        max_observed_shift = 0.0

        for i in range(min(len(source_doc), len(out))):
            src_page, out_page = source_doc[i], out[i]
            page_plan = plan.pages[i]

            if page_plan.page_strategy in {PageStrategy.DESIGNED_PAGE_PRESERVE, PageStrategy.DESIGNED_PAGE_LOGO_ONLY}:
                if page_plan.header_rect is not None or page_plan.header_template.value != "none":
                    issues.append(QAIssue(Severity.FATAL, "GENERIC_HEADER_ON_DESIGNED_PAGE", "Designed page was given a generic dynamic header", i + 1))

            if (
                plan.repeated_bands.footer_top is not None
                and plan.repeated_bands.footer_confidence >= 0.65
                and page_plan.page_type.value != "blank"
                and page_plan.page_strategy not in {PageStrategy.DESIGNED_PAGE_PRESERVE, PageStrategy.DESIGNED_PAGE_LOGO_ONLY}
                and page_plan.footer_rect is None
            ):
                issues.append(QAIssue(Severity.ERROR, "REPEATED_FOOTER_NOT_REPLACED", "A verified repeated legacy footer exists but this page has no in-place footer replacement plan", i + 1))

            if abs(src_page.rect.width - out_page.rect.width) > size_tol or abs(src_page.rect.height - out_page.rect.height) > size_tol:
                issues.append(QAIssue(Severity.FATAL, "PAGE_SIZE_CHANGED", "Output page dimensions differ from the source", i + 1))

            src_words = _body_words(src_page, page_plan, brand.legacy_terms)
            out_words = Counter(re.findall(r"[A-Za-z0-9]+", out_page.get_text("text").lower()))
            retained = _retention(src_words, out_words)
            retention_values.append(retained)
            if retained < min_retention:
                issues.append(QAIssue(Severity.ERROR, "BODY_TEXT_LOSS", f"Body text retention is {retained:.1%}, below {min_retention:.1%}", i + 1))

            # Detect any searchable legacy brand that survived the operation.
            for term in brand.legacy_terms:
                try:
                    rects = out_page.search_for(term)
                except Exception:
                    rects = []
                if rects:
                    legacy_hits += len(rects)
                    sev = Severity.ERROR if brand.qa.get("legacy_text_is_error", True) else Severity.WARNING
                    issues.append(QAIssue(sev, "LEGACY_TEXT_REMAINS", f"Legacy text {term!r} remains in output", i + 1))
                    break

            # Overlay-mode pages are required to keep academic body coordinates unchanged.
            if page_plan.strategy.value in {"overlay", "preserve", "replace_logo_only"}:
                for text, source_y in _body_anchors(src_page, page_plan):
                    try:
                        hits = out_page.search_for(text)
                    except Exception:
                        hits = []
                    if not hits:
                        continue
                    nearest = min(hits, key=lambda r: abs(r.y0 - source_y))
                    shift = abs(nearest.y0 - source_y)
                    max_observed_shift = max(max_observed_shift, shift)
                    shift_checks += 1
                    if shift > max_shift:
                        issues.append(QAIssue(Severity.ERROR, "BODY_POSITION_SHIFT", f"Body text moved by {shift:.1f} pt (limit {max_shift:.1f} pt)", i + 1))
                        break

            # Plan-integrity: header must not extend into known body boundary.
            if page_plan.header_rect:
                gap = float(brand.layout("safety_gap", 7))
                reasons = " ".join(page_plan.reasons)
                m = re.search(r"first body content y=([0-9.]+)", reasons)
                if m and page_plan.header_rect.y1 > float(m.group(1)) - gap + 0.2:
                    issues.append(QAIssue(Severity.FATAL, "HEADER_BODY_COLLISION_PLAN", "Planned header crosses the safe body boundary", i + 1))

            side_hits = _legacy_side_hits(out_page, brand.side_text_terms, side_width)
            if side_hits:
                sev = Severity.ERROR if brand.qa.get("side_text_is_error", False) else Severity.WARNING
                issues.append(QAIssue(sev, "VERTICAL_LEGACY_TEXT_REMAINS", f"Legacy side text remains: {', '.join(side_hits[:3])}", i + 1))

        avg_retention = sum(retention_values) / max(len(retention_values), 1)
        strategy_counts = Counter(p.page_strategy.value for p in plan.pages)
        passed = not any(item.severity in {Severity.ERROR, Severity.FATAL} for item in issues)
        return QAReport(
            source=str(plan.source), output=str(output_path), passed=passed,
            page_count_source=len(source_doc), page_count_output=len(out), issues=issues,
            metrics={
                "average_body_text_retention": round(avg_retention, 5),
                "legacy_text_hits": legacy_hits,
                "pages_checked": min(len(source_doc), len(out)),
                "body_position_checks": shift_checks,
                "max_body_position_shift_pt": round(max_observed_shift, 3),
                "page_strategy_counts": dict(strategy_counts),
                "repeated_footer_confidence": round(plan.repeated_bands.footer_confidence, 3),
            },
        )
