from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

import pymupdf as fitz

from .config import BrandingProfile
from .metadata import norm
from .models import DocumentMeta, PageAnalysis, PageType, RectData, RepeatedBands


@dataclass(slots=True)
class PageFeatures:
    index: int
    width: float
    height: float
    text_blocks: list[dict]
    drawings: list[dict]
    image_info: list[dict]
    plain_text: str
    blank: bool
    dark_top_ratio: float
    top_lines: list[float]
    bottom_lines: list[float]
    bottom_visual_bands: list[tuple[float, float, float]]


def _block_text(block: dict) -> str:
    return " ".join(
        span.get("text", "")
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    ).strip()


def _repeat_signature(text: str) -> str:
    n = norm(text)
    n = re.sub(r"\b\d+\b", "#", n)
    n = re.sub(r"\bpage\s*#\b", "page #", n)
    return n.strip()


def _is_vertical_line(line: dict) -> bool:
    direction = line.get("dir")
    if not direction:
        return False
    dx, dy = direction
    return abs(dx) < 0.25 and abs(abs(dy) - 1) < 0.25



def _max_span_size(block: dict) -> float:
    sizes: list[float] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            try:
                sizes.append(float(span.get("size", 0.0)))
            except Exception:
                pass
    return max(sizes, default=0.0)


def _chapter_title_metrics(features: PageFeatures, chapter: str) -> tuple[bool, float, float, float]:
    """Return (found, max_font_size, y0, width_ratio) for the chapter title.

    Matching is block-based rather than exact-line based so titles such as
    "1 Measurement and Motion" are recognized reliably.
    """
    chapter_n = norm(chapter)
    if not chapter_n:
        return False, 0.0, features.height, 0.0
    best: tuple[float, float, float] | None = None
    for block in features.text_blocks:
        if block.get("type") != 0:
            continue
        text_n = norm(_block_text(block))
        if chapter_n not in text_n:
            continue
        try:
            r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
        except Exception:
            continue
        size = _max_span_size(block)
        width_ratio = max(0.0, r.width) / max(features.width, 1.0)
        candidate = (size, r.y0, width_ratio)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return False, 0.0, features.height, 0.0
    return True, best[0], best[1], best[2]


def _page_has_structural_repeated_header(
    features: PageFeatures, repeated: RepeatedBands, meta: DocumentMeta, legacy_terms: tuple[str, ...]
) -> bool:
    """Whether this page actually carries the repeated *structural* source header.

    Brand-only tokens such as ALLEN or a lone subject label do not count.  This is
    important for designed chapter openers that may contain a source logo but do not
    use the recurring NCERT/module header template seen on later pages.
    """
    if not repeated.header_signatures:
        return False
    subject_n = norm(meta.subject)
    legacy_n = tuple(norm(t) for t in legacy_terms)
    for block in features.text_blocks:
        if block.get("type") != 0:
            continue
        try:
            r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
        except Exception:
            continue
        if r.y1 > features.height * 0.20:
            continue
        text = _block_text(block).strip()
        if not text:
            continue
        text_n = norm(text)
        sig = _repeat_signature(text)
        if sig not in repeated.header_signatures:
            continue
        if any(t and t in text_n for t in legacy_n):
            continue
        if text_n in {subject_n, f"class {meta.class_name}", meta.class_name}:
            continue
        # Require a meaningful structural label, e.g. "NCERT Basics : Class 6".
        if len(text_n) >= 8:
            return True
    return False


def _dark_ratio_from_page(page: fitz.Page, top_ratio: float = 0.16, zoom: float = 0.8) -> float:
    h = max(10.0, page.rect.height * top_ratio)
    clip = fitz.Rect(0, 0, page.rect.width, min(page.rect.height, h))
    if clip.is_empty:
        return 0.0
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    except Exception:
        return 0.0
    data, n = pix.samples, pix.n
    if not data or n < 3:
        return 0.0
    dark = total = 0
    step = max(n * 11, n)
    for i in range(0, len(data) - n + 1, step):
        r, g, b = data[i], data[i + 1], data[i + 2]
        if 0.2126 * r + 0.7152 * g + 0.0722 * b < 120:
            dark += 1
        total += 1
    return dark / max(total, 1)


def _wide_horizontal_line_ys(drawings: list[dict], width: float, height: float, top: bool) -> list[float]:
    out: list[float] = []
    zone_limit = height * (0.28 if top else 0.78)
    for d in drawings:
        try:
            r = fitz.Rect(d.get("rect", (0, 0, 0, 0)))
        except Exception:
            continue
        if r.width < width * 0.45:
            continue
        # Ignore giant page/background rectangles.  Near the footer, however,
        # many source PDFs use a repeated filled page-number band 18-40 pt tall
        # rather than a hairline rule.  Treat the *top edge* of that band as the
        # footer boundary so the replacement footer can erase it completely.
        if top:
            if r.height > 16:
                continue
            y = (r.y0 + r.y1) / 2
            if 4 <= y <= zone_limit:
                out.append(y)
        else:
            if r.height <= 16:
                y = (r.y0 + r.y1) / 2
                if zone_limit <= y <= height - 2:
                    out.append(y)
            elif r.height <= 48 and r.y0 >= zone_limit and r.width >= width * 0.60:
                # Repeated footer strip / page-number band.
                out.append(r.y0)
    return out




def _visual_horizontal_bands(
    page: fitz.Page,
    *,
    bottom: bool,
    zoom: float = 0.72,
    min_row_fraction: float = 0.50,
) -> list[tuple[float, float, float]]:
    """Detect broad raster bands / separator rules without relying on PDF objects.

    This is intentionally conservative and is used primarily for repeated footer
    detection.  It catches source footers that are flattened into images or complex
    vector paths and therefore never appear as a simple drawing rectangle.
    """
    start_ratio = 0.70 if bottom else 0.0
    end_ratio = 1.0 if bottom else 0.28
    y0 = page.rect.height * start_ratio
    y1 = page.rect.height * end_ratio
    clip = fitz.Rect(0, y0, page.rect.width, y1)
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    except Exception:
        return []
    if pix.n < 3 or pix.width < 20 or pix.height < 2:
        return []

    data = pix.samples
    n = pix.n
    left = max(0, int(pix.width * 0.04))
    right = min(pix.width, int(pix.width * 0.96))
    span = max(1, right - left)
    marked: list[tuple[int, float]] = []
    for py in range(pix.height):
        nonwhite = 0
        base = py * pix.width * n
        # sample every second pixel for speed
        samples = 0
        for px in range(left, right, 2):
            i = base + px * n
            r, g, b = data[i], data[i + 1], data[i + 2]
            # Light-blue bands are close to white, so use a generous threshold.
            if min(r, g, b) < 246 or (max(r, g, b) - min(r, g, b)) > 10:
                nonwhite += 1
            samples += 1
        frac = nonwhite / max(samples, 1)
        if frac >= min_row_fraction:
            marked.append((py, frac))

    if not marked:
        return []

    groups: list[list[tuple[int, float]]] = []
    for item in marked:
        if not groups or item[0] - groups[-1][-1][0] > 2:
            groups.append([item])
        else:
            groups[-1].append(item)

    bands: list[tuple[float, float, float]] = []
    for group in groups:
        py0, py1 = group[0][0], group[-1][0] + 1
        gy0 = y0 + py0 / zoom
        gy1 = y0 + py1 / zoom
        strength = sum(v for _, v in group) / len(group)
        # Thin horizontal rules are valid; reject isolated noise only.
        if gy1 - gy0 >= 0.7:
            bands.append((gy0, gy1, strength))
    return bands


def extract_page_features(doc: fitz.Document) -> list[PageFeatures]:
    """Extract every expensive page primitive exactly once per analysis pass."""
    out: list[PageFeatures] = []
    for i in range(len(doc)):
        page = doc[i]
        try:
            td = page.get_text("dict")
            blocks = td.get("blocks", [])
        except Exception:
            blocks = []
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        try:
            image_info = page.get_image_info(xrefs=True)
        except Exception:
            image_info = []
        try:
            plain = page.get_text("text")
        except Exception:
            plain = ""
        try:
            blank = not plain.strip() and not page.get_images(full=True) and not drawings
        except Exception:
            blank = False
        out.append(PageFeatures(
            index=i,
            width=page.rect.width,
            height=page.rect.height,
            text_blocks=blocks,
            drawings=drawings,
            image_info=image_info,
            plain_text=plain,
            blank=blank,
            dark_top_ratio=_dark_ratio_from_page(page),
            top_lines=_wide_horizontal_line_ys(drawings, page.rect.width, page.rect.height, True),
            bottom_lines=_wide_horizontal_line_ys(drawings, page.rect.width, page.rect.height, False),
            bottom_visual_bands=_visual_horizontal_bands(page, bottom=True),
        ))
    return out


def _cluster_positions(values: list[float], tolerance: float = 4.0) -> list[list[float]]:
    if not values:
        return []
    clusters: list[list[float]] = []
    for value in sorted(values):
        if not clusters or abs(value - statistics.mean(clusters[-1])) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return clusters


def detect_repeated_bands_from_features(features: list[PageFeatures]) -> RepeatedBands:
    n_pages = len(features)
    if not n_pages:
        return RepeatedBands()
    # Repetition cannot be inferred from a one-page document.  Treating every
    # top block as "repeated" on a singleton PDF can incorrectly classify the
    # chapter title as boilerplate and create an unsafe 150pt cleanup band.
    if n_pages == 1:
        return RepeatedBands()
    selected = features[1:] if n_pages >= 4 else features
    sample_count = max(1, len(selected))
    threshold = max(2 if sample_count >= 2 else 1, math.ceil(sample_count * 0.45))

    header_occ: dict[str, set[int]] = defaultdict(set)
    footer_occ: dict[str, set[int]] = defaultdict(set)
    header_rects: dict[str, list[fitz.Rect]] = defaultdict(list)
    footer_rects: dict[str, list[fitz.Rect]] = defaultdict(list)
    all_top_lines: list[tuple[int, float]] = []
    all_bottom_lines: list[tuple[int, float]] = []
    all_bottom_visual: list[tuple[int, float, float, float]] = []

    for f in selected:
        for block in f.text_blocks:
            if block.get("type") != 0:
                continue
            text = _block_text(block)
            sig = _repeat_signature(text)
            if len(sig) < 3:
                continue
            r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
            if r.y1 <= f.height * 0.22:
                header_occ[sig].add(f.index)
                header_rects[sig].append(r)
            if r.y0 >= f.height * 0.82:
                footer_occ[sig].add(f.index)
                footer_rects[sig].append(r)
        all_top_lines.extend((f.index, y) for y in f.top_lines)
        all_bottom_lines.extend((f.index, y) for y in f.bottom_lines)
        all_bottom_visual.extend((f.index, y0, y1, strength) for y0, y1, strength in f.bottom_visual_bands)

    header_sigs = [s for s, pages in header_occ.items() if len(pages) >= threshold]
    footer_sigs = [s for s, pages in footer_occ.items() if len(pages) >= threshold]

    # Cluster line positions, counting support by distinct pages rather than raw drawing count.
    top_values = [y for _, y in all_top_lines]
    bottom_values = [y for _, y in all_bottom_lines]
    repeated_top: list[tuple[float, float]] = []
    for cluster in _cluster_positions(top_values):
        centre = statistics.median(cluster)
        pages = {idx for idx, y in all_top_lines if abs(y - centre) <= 4}
        if len(pages) >= threshold:
            repeated_top.append((centre, len(pages) / sample_count))
    repeated_bottom: list[tuple[float, float]] = []
    for cluster in _cluster_positions(bottom_values):
        centre = statistics.median(cluster)
        pages = {idx for idx, y in all_bottom_lines if abs(y - centre) <= 4}
        if len(pages) >= threshold:
            repeated_bottom.append((centre, len(pages) / sample_count))


    repeated_visual_footer: list[tuple[float, float]] = []
    visual_y0s = [y0 for _, y0, _, _ in all_bottom_visual]
    for cluster in _cluster_positions(visual_y0s, tolerance=6.0):
        centre = statistics.median(cluster)
        pages = {idx for idx, y0, _, _ in all_bottom_visual if abs(y0 - centre) <= 6}
        if len(pages) >= threshold:
            supports = [strength for idx, y0, _, strength in all_bottom_visual if idx in pages and abs(y0 - centre) <= 6]
            avg_strength = statistics.mean(supports) if supports else 0.0
            # Require a genuinely broad band / rule, not a coincidentally aligned text row.
            if avg_strength >= 0.50:
                repeated_visual_footer.append((centre, len(pages) / sample_count))

    # A repeated wide top rule is a stronger boundary signal than repeated body text.
    # Discard repeated text signatures that sit below that separator; otherwise identical
    # question/solution lines can be mistaken for header boilerplate.
    if repeated_top:
        strongest_top = [x for x in repeated_top if x[1] >= 0.60] or repeated_top
        top_boundary_hint = max(y for y, _ in strongest_top) + 10.0
        header_sigs = [
            sig for sig in header_sigs
            if header_rects[sig] and statistics.median(r.y1 for r in header_rects[sig]) <= top_boundary_hint
        ]

    header_bottom = 0.0
    if header_sigs:
        vals = [r.y1 for sig in header_sigs for r in header_rects[sig]]
        if vals:
            header_bottom = statistics.median(vals) + 4.0
    if repeated_top:
        # Use the lowest reliable top separator; this is usually the true legacy header boundary.
        strongest = [x for x in repeated_top if x[1] >= 0.60] or repeated_top
        header_bottom = max(header_bottom, max(y for y, _ in strongest) + 2.0)

    footer_top: float | None = None
    if footer_sigs:
        vals = [r.y0 for sig in footer_sigs for r in footer_rects[sig]]
        if vals:
            footer_top = statistics.median(vals) - 3.0
    if repeated_bottom:
        strongest = [x for x in repeated_bottom if x[1] >= 0.60] or repeated_bottom
        line_top = min(y for y, _ in strongest) - 2.0
        footer_top = line_top if footer_top is None else min(footer_top, line_top)
    if repeated_visual_footer:
        strongest_visual = [x for x in repeated_visual_footer if x[1] >= 0.60] or repeated_visual_footer
        visual_top = min(y for y, _ in strongest_visual)
        footer_top = visual_top if footer_top is None else min(footer_top, visual_top)

    header_support = 0.0
    if header_sigs:
        header_support = max(len(header_occ[s]) / sample_count for s in header_sigs)
    if repeated_top:
        header_support = max(header_support, max(s for _, s in repeated_top))
    footer_support = 0.0
    if footer_sigs:
        footer_support = max(len(footer_occ[s]) / sample_count for s in footer_sigs)
    if repeated_bottom:
        footer_support = max(footer_support, max(s for _, s in repeated_bottom))
    if repeated_visual_footer:
        footer_support = max(footer_support, max(s for _, s in repeated_visual_footer))

    return RepeatedBands(
        header_bottom=max(0.0, header_bottom),
        footer_top=footer_top,
        header_confidence=min(0.99, header_support),
        footer_confidence=min(0.99, footer_support),
        header_signatures=header_sigs[:16],
        footer_signatures=footer_sigs[:16],
    )


def _near(a: fitz.Rect, b: fitz.Rect, pad: float) -> bool:
    expanded = fitz.Rect(a.x0 - pad, a.y0 - pad, a.x1 + pad, a.y1 + pad)
    return not (expanded & b).is_empty


def _expand_legacy_hit(page: fitz.Page, hit: fitz.Rect, features: PageFeatures, profile: BrandingProfile) -> fitz.Rect:
    """Expand a searchable legacy word to cover its complete logo artwork.

    We use nearby text, vector drawing and image bounds. Expansion is capped to the
    upper part of the page and never becomes a page-wide destructive rectangle.
    """
    pad_x = float(profile.raw.get("legacy", {}).get("logo_padding_x", 30))
    pad_y = float(profile.raw.get("legacy", {}).get("logo_padding_y", 12))
    min_w = float(profile.raw.get("legacy", {}).get("logo_min_width", 92))
    min_h = float(profile.raw.get("legacy", {}).get("logo_min_height", 28))
    max_w = min(page.rect.width * 0.48, float(profile.raw.get("legacy", {}).get("logo_max_width", 280)))
    max_h = min(page.rect.height * 0.16, float(profile.raw.get("legacy", {}).get("logo_max_height", 110)))

    union = fitz.Rect(hit)
    neighbourhood = fitz.Rect(
        max(0, hit.x0 - pad_x), max(0, hit.y0 - pad_y),
        min(page.rect.width, hit.x1 + pad_x), min(page.rect.height * 0.34, hit.y1 + pad_y),
    )

    # Merge only genuine brand-text fragments (e.g. ALLEN / registered mark).
    # Do NOT union arbitrary nearby chapter titles.  V3.1 could absorb
    # "Measurement and Motion" into the logo rectangle on first pages.
    legacy_terms_n = tuple(norm(t) for t in profile.legacy_terms)
    for block in features.text_blocks:
        if block.get("type") != 0:
            continue
        text_n = norm(_block_text(block))
        is_brand_fragment = any(t and t in text_n for t in legacy_terms_n) or len(text_n) <= 2
        if not is_brand_fragment:
            continue
        r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
        if r.y0 > page.rect.height * 0.34:
            continue
        if _near(neighbourhood, r, 4) and r.width <= max_w:
            union |= r

    # Merge vector/image components that physically touch or sit very close to the logo hit.
    for d in features.drawings:
        try:
            r = fitz.Rect(d.get("rect", (0, 0, 0, 0)))
        except Exception:
            continue
        if r.y0 > page.rect.height * 0.34 or r.width > max_w or r.height > max_h:
            continue
        if _near(neighbourhood, r, 5):
            union |= r
    for info in features.image_info:
        try:
            r = fitz.Rect(info.get("bbox", (0, 0, 0, 0)))
        except Exception:
            continue
        if r.y0 > page.rect.height * 0.34 or r.width > max_w or r.height > max_h:
            continue
        if _near(neighbourhood, r, 6):
            union |= r

    cx, cy = (union.x0 + union.x1) / 2, (union.y0 + union.y1) / 2
    w, h = max(union.width + 8, min_w), max(union.height + 6, min_h)
    w, h = min(w, max_w), min(h, max_h)
    out = fitz.Rect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    return fitz.Rect(
        max(0, out.x0), max(0, out.y0),
        min(page.rect.width, out.x1), min(page.rect.height * 0.34, out.y1),
    )


def find_legacy_rects(page: fitz.Page, features: PageFeatures, profile: BrandingProfile) -> list[fitz.Rect]:
    hits: list[fitz.Rect] = []
    max_y = page.rect.height * 0.34
    for term in profile.legacy_terms:
        try:
            rects = page.search_for(term)
        except Exception:
            rects = []
        for r in rects:
            if r.y0 <= max_y:
                hits.append(_expand_legacy_hit(page, r, features, profile))

    # Merge overlapping / near-duplicate legacy regions.
    merged: list[fitz.Rect] = []
    for r in sorted(hits, key=lambda x: (x.y0, x.x0)):
        combined = False
        for i, existing in enumerate(merged):
            if _near(existing, r, 5):
                merged[i] = existing | r
                combined = True
                break
        if not combined:
            merged.append(r)
    return merged


def find_vertical_text_rects(features: PageFeatures, side_width: float, terms: tuple[str, ...]) -> list[fitz.Rect]:
    """Find verified legacy publisher/module text in the side margins.

    V3.4.3 deliberately supports two extraction shapes:
    1) proper rotated PDF text whose line direction is vertical;
    2) tall, narrow side-margin text blocks produced by some generators where
       PyMuPDF does not retain a reliable line direction.

    The fallback is conservative: it only activates for a side-margin block that
    matches a configured legacy term and has strongly vertical geometry.  This
    keeps designed chapter artwork intact while still removing the PNCF / LIVE
    Module strip visible on illustrated pages.
    """
    terms_n = tuple(norm(t) for t in terms if norm(t))
    out: list[fitz.Rect] = []
    for block in features.text_blocks:
        if block.get("type") != 0:
            continue
        r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
        if r.is_empty:
            continue
        if not (r.x1 <= side_width or r.x0 >= features.width - side_width):
            continue

        text_n = norm(_block_text(block))
        matches_term = bool(terms_n and any(t in text_n for t in terms_n))
        lines = block.get("lines", [])
        has_vertical_direction = any(_is_vertical_line(line) for line in lines)

        # Some PDFs expose rotated publisher text as a tall narrow block but lose
        # the line direction.  Require a configured legacy term plus strong shape.
        tall_narrow = r.height >= max(36.0, r.width * 2.2)
        geometry_fallback = matches_term and tall_narrow
        if not (has_vertical_direction or geometry_fallback):
            continue

        if terms_n and not matches_term and len(text_n) < 12:
            continue

        out.append(fitz.Rect(
            max(0, r.x0 - 2), max(0, r.y0 - 2),
            min(features.width, r.x1 + 2), min(features.height, r.y1 + 2),
        ))
    return out


def _known_headerish_text(text_n: str, meta: DocumentMeta, legacy_terms: tuple[str, ...]) -> bool:
    if not text_n:
        return True
    if any(norm(t) in text_n for t in legacy_terms):
        return True
    subject = norm(meta.subject)
    if text_n in {subject, f"class {meta.class_name}", meta.class_name}:
        return True
    if text_n.startswith("page "):
        return True
    # Common legacy publication boilerplate.
    if any(t in text_n for t in ("module", "foundation", "allen career institute")) and len(text_n) < 80:
        return True
    return False


def content_bounds(features: PageFeatures, meta: DocumentMeta, repeated: RepeatedBands, legacy_terms: tuple[str, ...]) -> tuple[float | None, float | None]:
    first: float | None = None
    last: float | None = None
    for block in features.text_blocks:
        if block.get("type") != 0:
            continue
        text = _block_text(block).strip()
        if not text:
            continue
        if any(_is_vertical_line(line) for line in block.get("lines", [])):
            continue
        r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
        text_n = norm(text)
        sig = _repeat_signature(text)
        # Repeated top-of-page text is boilerplate even when it does not contain the
        # brand name. Real source PDFs use labels such as "NCERT Basics : Class 6"
        # or "NCERT Course : Class 6" above the academic body. Treat those repeated
        # signatures as header content so they never become the body safety boundary.
        if (
            sig in repeated.header_signatures
            and r.y1 <= max(repeated.header_bottom + 12, features.height * 0.20)
        ):
            continue
        if (
            _known_headerish_text(text_n, meta, legacy_terms)
            and r.y0 <= max(repeated.header_bottom + 20, features.height * 0.16)
        ):
            continue
        # A few recurring legacy header labels are not always repeated in short PDFs.
        if r.y1 <= features.height * 0.16 and re.search(r"\bncert\s+(?:basics|course)\s*:?\s*class\s*\d+\b", text_n):
            continue
        if repeated.footer_top is not None and r.y0 >= repeated.footer_top and (text_n.startswith("page") or len(text_n) < 60):
            continue
        first = r.y0 if first is None else min(first, r.y0)
        last = r.y1 if last is None else max(last, r.y1)
    return first, last


def sample_local_background(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float, float]:
    """Estimate the *actual* local page background around a legacy logo.

    V3.4.3 used a global median of surrounding pixels.  That is robust for plain
    white worksheets, but on designed pages a nearby illustration (cloud, rocket,
    dinosaur, etc.) can pull the median away from the real flat background and
    create a visible rectangular patch behind an otherwise transparent PNG.

    V3.4.3 instead uses the dominant quantized colour from tighter edge probes,
    then refines it with the median of the pixels in that dominant cluster.  On
    flat/near-flat artwork this matches the source background closely enough that
    the transparent school emblem reads as a true overlay rather than a boxed
    sticker.  If no stable dominant cluster exists we fall back to the old median.
    """
    probes = [
        # Tight strips immediately outside the replacement rectangle.  Keep these
        # narrow so unrelated artwork farther away cannot dominate the estimate.
        fitz.Rect(max(0, rect.x0 - 18), rect.y0, max(0, rect.x0 - 3), rect.y1),
        fitz.Rect(min(page.rect.width, rect.x1 + 3), rect.y0, min(page.rect.width, rect.x1 + 20), rect.y1),
        fitz.Rect(rect.x0, max(0, rect.y0 - 14), rect.x1, max(0, rect.y0 - 3)),
        fitz.Rect(rect.x0, min(page.rect.height, rect.y1 + 3), rect.x1, min(page.rect.height, rect.y1 + 18)),
    ]
    samples: list[tuple[int, int, int]] = []
    for pr in probes:
        if pr.width <= 2 or pr.height <= 2:
            continue
        try:
            # 1.5x captures enough pixels for a stable mode without making analysis
            # expensive across a large batch.
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), clip=pr, alpha=False)
        except Exception:
            continue
        data, n = pix.samples, pix.n
        if n < 3:
            continue
        # Dense-enough sampling for small logo neighbourhoods.
        step = max(n * 7, n)
        for i in range(0, len(data) - n + 1, step):
            samples.append((data[i], data[i + 1], data[i + 2]))
    if not samples:
        return 1.0, 1.0, 1.0

    # Preserve genuinely white worksheet backgrounds exactly.
    whiteish = sum(1 for r, g, b in samples if r > 240 and g > 240 and b > 240)
    if whiteish / len(samples) >= 0.55:
        return 1.0, 1.0, 1.0

    # Find the dominant local colour after light quantisation.  The cluster is
    # deliberately coarse enough to absorb antialiasing/JPEG noise but fine enough
    # to distinguish brown, navy, green, charcoal, etc.
    from collections import Counter

    bucket = 8
    keys = [((r // bucket) * bucket, (g // bucket) * bucket, (b // bucket) * bucket) for r, g, b in samples]
    counts = Counter(keys)
    dominant_key, dominant_count = counts.most_common(1)[0]
    dominant_fraction = dominant_count / max(len(samples), 1)

    # Refine the quantised mode back toward the source colour by taking the median
    # of the raw samples that landed in the dominant bucket.
    cluster = [rgb for rgb, key in zip(samples, keys) if key == dominant_key]
    if dominant_fraction >= 0.08 and len(cluster) >= 3:
        return (
            statistics.median(v[0] for v in cluster) / 255,
            statistics.median(v[1] for v in cluster) / 255,
            statistics.median(v[2] for v in cluster) / 255,
        )

    # Highly textured background: use the robust global median rather than allowing
    # a tiny accidental colour cluster to decide the repair colour.
    return (
        statistics.median(v[0] for v in samples) / 255,
        statistics.median(v[1] for v in samples) / 255,
        statistics.median(v[2] for v in samples) / 255,
    )


def analyze_document(doc: fitz.Document, meta: DocumentMeta, profile: BrandingProfile) -> tuple[RepeatedBands, list[PageAnalysis]]:
    features = extract_page_features(doc)
    repeated = detect_repeated_bands_from_features(features)
    side_width = float(profile.layout("side_text_width", 68))
    analyses: list[PageAnalysis] = []

    for f in features:
        page = doc[f.index]
        legacy = find_legacy_rects(page, f, profile)
        vertical = find_vertical_text_rects(f, side_width, profile.side_text_terms)
        first_y, last_y = content_bounds(f, meta, repeated, profile.legacy_terms)
        reasons: list[str] = []
        page_area = max(f.width * f.height, 1.0)
        large_image_ratio = 0.0
        for info in f.image_info:
            try:
                ir = fitz.Rect(info.get("bbox", (0, 0, 0, 0)))
            except Exception:
                continue
            large_image_ratio = max(large_image_ratio, max(0.0, ir.get_area()) / page_area)
        chapter_n = norm(meta.chapter)
        title_present = bool(chapter_n and chapter_n in norm(f.plain_text))
        title_found, title_font, title_y0, title_width_ratio = _chapter_title_metrics(f, meta.chapter)
        designed_ratio = float(profile.layout("designed_page_large_image_ratio", 0.075))
        title_min_font = float(profile.layout("designed_page_title_min_font", 17.0))
        title_top_ratio = float(profile.layout("designed_page_title_top_ratio", 0.30))
        repeated_header_on_page = _page_has_structural_repeated_header(
            f, repeated, meta, profile.legacy_terms
        )
        prominent_title = (
            title_found
            and title_font >= title_min_font
            and title_y0 <= f.height * title_top_ratio
        )
        visual_design_signal = (
            large_image_ratio >= designed_ratio
            or len(f.image_info) >= 1
            or len(f.drawings) >= 4
            or title_font >= title_min_font * 1.20
            or title_width_ratio >= 0.42
        )
        designed_chapter_opener = (
            f.index == 0
            and meta.material_type == "NOTES"
            and title_present
            and prominent_title
            and visual_design_signal
            and not repeated_header_on_page
        )

        if f.blank:
            ptype = PageType.BLANK
            reasons.append("blank page")
        elif designed_chapter_opener:
            ptype = PageType.DESIGNED
            reasons.append(
                f"designed chapter opener: prominent chapter title font={title_font:.1f}pt at y={title_y0:.1f}, "
                f"large-image ratio={large_image_ratio:.2f}, structural repeated header absent"
            )
            if legacy:
                reasons.append("legacy logo may be replaced surgically; generic header remains forbidden")
        elif meta.material_type == "KEY POINTS":
            ptype = PageType.DESIGNED
            reasons.append("Key Points material preserves designed artwork")
            if not legacy and bool(profile.raw.get("legacy", {}).get("allow_designed_fallback", True)):
                fallback = profile.raw.get("legacy", {}).get("designed_logo_fallback_ratio")
                if isinstance(fallback, list) and len(fallback) == 4:
                    x0, y0, x1, y1 = [float(v) for v in fallback]
                    legacy = [fitz.Rect(f.width * x0, f.height * y0, f.width * x1, f.height * y1)]
                    reasons.append("profile fallback logo region used on designed page")
        elif f.dark_top_ratio >= float(profile.layout("dark_page_threshold", 0.45)):
            ptype = PageType.DARK
            reasons.append(f"dark top-region ratio={f.dark_top_ratio:.2f}")
        else:
            ptype = PageType.STANDARD

        bg = sample_local_background(page, legacy[0]) if legacy else None
        analyses.append(PageAnalysis(
            page_number=f.index + 1,
            width=f.width,
            height=f.height,
            page_type=ptype,
            blank=f.blank,
            dark_ratio=f.dark_top_ratio,
            first_content_y=first_y,
            last_content_y=last_y,
            legacy_rects=[RectData.from_rect(r) for r in legacy],
            vertical_text_rects=[RectData.from_rect(r) for r in vertical],
            local_logo_background=bg,
            large_image_ratio=large_image_ratio,
            reasons=reasons,
        ))
    return repeated, analyses
