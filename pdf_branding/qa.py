from __future__ import annotations

import re
import math
from collections import Counter
from pathlib import Path

import pymupdf as fitz

from .image_info import image_info as page_image_info
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .config import BrandingProfile
from .metadata import norm
from .models import DocumentPlan, PageStrategy, QAIssue, QAReport, RectData, Severity
from .native_cleanup import image_background_pixmap
from .raster_brand import scan_header


def _intersects(a: fitz.Rect, b: RectData) -> bool:
    return not (a & fitz.Rect(*b.as_tuple())).is_empty


def _excluded_rects(plan_page, output_coords: bool = False) -> list[RectData]:
    if plan_page.strategy.value == "rebuild_crop" and plan_page.source_clip_rect is not None:
        clip = plan_page.source_clip_rect
        if output_coords:
            out = [plan_page.map_source_rect(r) for r in plan_page.vertical_text_rects + plan_page.additional_logo_rects]
            if plan_page.logo_replace_rect:
                out.append(plan_page.map_source_rect(plan_page.logo_replace_rect))
            out += [r for r in (plan_page.header_rect, plan_page.footer_rect) if r is not None]
        else:
            out = list(plan_page.vertical_text_rects) + list(plan_page.additional_logo_rects)
            out += [RectData(0, 0, clip.x1, clip.y0), RectData(0, clip.y1, clip.x1, 100000)]
        return out
    out = list(plan_page.cleanup_rects) + list(plan_page.vertical_text_rects) + list(plan_page.additional_logo_rects)
    if plan_page.header_rect:
        out.append(plan_page.header_rect)
    if plan_page.footer_rect:
        out.append(plan_page.footer_rect)
    if plan_page.logo_replace_rect:
        out.append(plan_page.logo_replace_rect)
    return out


def _body_words(page: fitz.Page, plan_page, legacy_terms: tuple[str, ...], *, output_coords: bool = False) -> Counter[str]:
    excluded = _excluded_rects(plan_page, output_coords)
    words: list[str] = []
    try:
        page_words = page.get_text("words")
    except Exception as exc:
        raise ValueError(f"Body text extraction failed: {exc}") from exc
    legacy_n = {norm(t) for t in legacy_terms}
    # Word extraction preserves words spanning font changes or superscripts.
    # Filter individual word boxes so a block containing both header and body
    # text cannot cause genuine academic text to be omitted from QA.
    for page_word in page_words:
        r = fitz.Rect(page_word[:4])
        if any(_intersects(r, ex) for ex in excluded):
            continue
        for word in re.findall(r"[A-Za-z0-9]+", page_word[4].lower()):
            if norm(word) not in legacy_n:
                words.append(word)
    return Counter(words)


def _geometry_issues(page_plan, page_number: int) -> list[QAIssue]:
    issues: list[QAIssue] = []
    protected = page_plan.protected_text_rects + page_plan.protected_visual_rects
    for label, region in (("HEADER", page_plan.header_rect), ("FOOTER", page_plan.footer_rect)):
        if region is None:
            continue
        if any(_intersects(fitz.Rect(*page_plan.map_source_rect(body).as_tuple()), region) for body in protected):
            issues.append(QAIssue(Severity.FATAL, f"{label}_BODY_COLLISION_PLAN", f"Planned {label.lower()} intersects protected academic content", page_number))
    for region in page_plan.cleanup_rects + page_plan.vertical_text_rects + page_plan.additional_logo_rects + ([page_plan.logo_replace_rect] if page_plan.logo_replace_rect else []):
        if any(_intersects(fitz.Rect(*body.as_tuple()), region) for body in protected):
            issues.append(QAIssue(Severity.FATAL, "CLEANUP_BODY_COLLISION_PLAN", "Cleanup intersects protected academic text, image or drawing", page_number))
            break
    if page_plan.strategy.value == "rebuild_crop":
        if page_plan.source_clip_rect is None or page_plan.source_to_output_matrix is None:
            issues.append(QAIssue(Severity.FATAL, "CROP_MAPPING_MISSING", "Crop has no explicit source-to-output geometry", page_number))
        elif not all(math.isfinite(v) for v in page_plan.source_to_output_matrix):
            issues.append(QAIssue(Severity.FATAL, "CROP_MAPPING_INVALID", "Crop transform contains non-finite coordinates", page_number))
        elif any(not fitz.Rect(*page_plan.source_clip_rect.as_tuple()).contains(fitz.Rect(*body.as_tuple())) for body in protected):
            issues.append(QAIssue(Severity.FATAL, "CROP_CONTENT_CLIPPED", "Crop removes protected academic content", page_number))
    return issues


def _rgb_image(page: fitz.Page, zoom: float = 1.0) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _significant_difference(a: Image.Image, b: Image.Image, tolerance: int) -> Image.Image:
    channels = ImageChops.difference(a, b).split()
    delta = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    return delta.point(lambda value: 255 if value > tolerance else 0)


def _rendered_body_check(source_doc: fitz.Document, index: int, output: fitz.Page, page_plan, brand: BrandingProfile, *, retry=True) -> tuple[float, list[QAIssue]]:
    zoom = float(brand.qa.get("render_compare_zoom", 1.0))
    reference = None
    try:
        if page_plan.strategy.value == "rebuild_crop" and page_plan.source_clip_rect is not None and page_plan.source_to_output_matrix is not None:
            reference = fitz.open(stream=source_doc.tobytes(), filetype="pdf")
            expected_page = reference[index]
            empty = reference.get_new_xref()
            reference.update_object(empty, "<< >>")
            reference.update_stream(empty, b"")
            expected_page.set_contents(empty)
            clip = fitz.Rect(*page_plan.source_clip_rect.as_tuple())
            destination = fitz.Rect(*page_plan.map_source_rect(page_plan.source_clip_rect).as_tuple())
            expected_page.show_pdf_page(destination, source_doc, index, clip=clip, keep_proportion=False)
        else:
            expected_page = source_doc[index]
        expected, actual = _rgb_image(expected_page, zoom), _rgb_image(output, zoom)
        if expected.size != actual.size:
            return 1.0, [QAIssue(Severity.ERROR, "RENDER_SIZE_MISMATCH", "Rendered page dimensions differ", index + 1)]
        mask = Image.new("L", expected.size, 255)
        draw = ImageDraw.Draw(mask)
        for rect in _excluded_rects(page_plan, output_coords=True):
            # One device pixel accounts for rasterization at an authorized edge.
            draw.rectangle((math.floor(rect.x0 * zoom) - 1, math.floor(rect.y0 * zoom) - 1,
                            math.ceil(rect.x1 * zoom) + 1, math.ceil(rect.y1 * zoom) + 1), fill=0)
        compared = mask.histogram()[255]
        if not compared:
            return 1.0, [QAIssue(Severity.ERROR, "NO_BODY_REGION_VERIFIED", "Cleanup excludes the entire page from visual verification", index + 1)]
        difference = ImageChops.multiply(_significant_difference(expected, actual, int(brand.qa.get("render_pixel_tolerance", 12))), mask)
        ratio = difference.histogram()[255] / compared
        issues: list[QAIssue] = []
        if ratio > float(brand.qa.get("max_body_render_change_ratio", .0005)):
            issues.append(QAIssue(Severity.ERROR, "BODY_RENDER_CHANGED", f"Academic region changes in {ratio:.3%} of compared pixels", index + 1))
        # Check each protected visual separately: a small diagram must not be
        # diluted by a mostly blank page in the page-wide ratio above.
        for region in page_plan.protected_visual_rects:
            mapped = page_plan.map_source_rect(region)
            box = (max(0, math.floor(mapped.x0 * zoom)), max(0, math.floor(mapped.y0 * zoom)),
                   min(expected.width, math.ceil(mapped.x1 * zoom)), min(expected.height, math.ceil(mapped.y1 * zoom)))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            roi_mask, roi_diff = mask.crop(box), difference.crop(box)
            checked = roi_mask.histogram()[255]
            if checked and roi_diff.histogram()[255] / checked > float(brand.qa.get("max_visual_region_change_ratio", .01)):
                issues.append(QAIssue(Severity.ERROR, "DIAGRAM_RENDER_CHANGED", "A protected image or drawing changed outside authorized cleanup", index + 1))
                break
        if issues and retry and output.parent.name:
            # Recheck suspect pages from independent documents with an empty
            # MuPDF store. Mixed-scale image renders can retain decoded masks.
            fitz.TOOLS.store_shrink(100)
            with fitz.open(stream=source_doc.tobytes(), filetype="pdf") as fresh_source, fitz.open(output.parent.name) as fresh_output:
                return _rendered_body_check(fresh_source, index, fresh_output[index], page_plan, brand, retry=False)
        return ratio, issues
    except Exception as exc:
        return 1.0, [QAIssue(Severity.ERROR, "RENDER_VERIFICATION_FAILED", f"Rendered-content verification failed: {exc}", index + 1)]
    finally:
        if reference is not None:
            reference.close()


def _raster_logo_residual(source: fitz.Page, output: fitz.Page, page_plan, region: RectData | None = None) -> tuple[int, int]:
    """Independently compare source logo edge templates outside the new emblem.

    This detects unchanged raster/vector legacy strokes after a nominal repair;
    it does not claim OCR recognition of arbitrary logos elsewhere on the page.
    """
    region = region or page_plan.logo_replace_rect
    if region is None:
        return 0, 0
    r = fitz.Rect(*region.as_tuple())
    if r.width < 8 or r.height < 8 or page_plan.source_to_output_matrix is not None:
        return 0, 0
    zoom = 2.0
    def crop_image(page):
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=r, colorspace=fitz.csRGB, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    before, after = crop_image(source), crop_image(output)
    if before.size != after.size:
        raise ValueError("Logo reference and output crop differ in size")
    edge = _significant_difference(before, before.filter(ImageFilter.GaussianBlur(1.2)), 6)
    bg = page_plan.logo_background or (1.0, 1.0, 1.0)
    background = Image.new("RGB", before.size, tuple(round(channel * 255) for channel in bg))
    # Blur contrast also marks unchanged background pixels adjacent to a glyph.
    # Require ink on the source pixel itself, or that halo is a false residual.
    edge = ImageChops.multiply(edge, _significant_difference(before, background, 8))
    for info in page_image_info(source):
        box = fitz.Rect(info["bbox"])
        if not info.get("xref") or not r.contains(box):
            continue
        mask_xref = source.parent.extract_image(info["xref"]).get("smask", 0)
        if not mask_xref:
            continue
        alpha_pix = fitz.Pixmap(source.parent, mask_xref)
        alpha = Image.frombytes("L", (alpha_pix.width, alpha_pix.height), alpha_pix.samples)
        if alpha.histogram()[0] < alpha.width * alpha.height * .02:
            continue  # An opaque mask does not identify foreground ink.
        alpha = alpha.resize((max(1, round(box.width * zoom)), max(1, round(box.height * zoom))))
        ink = Image.new("L", before.size, 0)
        ink.paste(alpha.point(lambda value: 255 if value >= 128 else 0),
                  (round((box.x0-r.x0)*zoom), round((box.y0-r.y0)*zoom)))
        edge = ImageChops.multiply(edge, ink)
        break
    else:
        # Opaque JPEG logos need a reference to the actual artwork beneath the
        # image. A sampled flat colour mislabels gradient/background pixels as
        # logo ink. Remove just the contained source image in a disposable clone.
        for info in page_image_info(source):
            box = fitz.Rect(info["bbox"])
            if info.get("xref") and r.contains(box) and box.get_area() > r.get_area() * .15:
                empty_pixmap = image_background_pixmap(source, info["xref"], r, zoom)
                empty = Image.frombytes("RGB", (empty_pixmap.width, empty_pixmap.height), empty_pixmap.samples)
                edge = ImageChops.multiply(edge, _significant_difference(before, empty, 8))
                break
    preserved = ImageChops.invert(_significant_difference(before, after, 12))
    mask = Image.new("L", before.size, 0)
    draw = ImageDraw.Draw(mask)
    inset = 3.0 * zoom
    draw.rectangle((inset, inset, before.width - inset - 1, before.height - inset - 1), fill=255)
    # Emblem placement is specified by the renderer; exclude its entire opaque
    # footprint, rather than interpreting new school strokes as an old logo.
    logo_width = min(r.width * .90, 58.0)
    # A square envelope conservatively contains both portrait and wide emblems.
    half = min(r.height * .45, max(logo_width / 2, 18)) * zoom
    cx, cy = before.width / 2, before.height / 2
    if page_plan.logo_replace_rect is not None or region in page_plan.additional_logo_rects:
        draw.rectangle((cx - logo_width * zoom / 2 - 2, cy - half - 2,
                        cx + logo_width * zoom / 2 + 2, cy + half + 2), fill=0)
    template = ImageChops.multiply(edge, mask)
    return ImageChops.multiply(template, preserved).histogram()[255], template.histogram()[255]


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


def _body_positions(page: fitz.Page, page_plan, legacy_terms: tuple[str, ...], *, output_coords: bool = False) -> dict[str, list[fitz.Rect]]:
    excluded = _excluded_rects(page_plan, output_coords)
    legacy_n = {norm(t) for t in legacy_terms}
    positions: dict[str, list[fitz.Rect]] = {}
    for word in page.get_text("words"):
        rect = fitz.Rect(word[:4])
        if any(_intersects(rect, ex) for ex in excluded):
            continue
        for token in re.findall(r"[A-Za-z0-9]+", word[4].lower()):
            if norm(token) not in legacy_n:
                positions.setdefault(token, []).append(rect)
    return positions


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
        render_changes: list[float] = []
        visual_only_pages = 0
        raster_logo_checks = 0
        legacy_hits = 0
        shift_checks = 0
        max_observed_shift = 0.0

        for i in range(min(len(source_doc), len(out))):
            src_page, out_page = source_doc[i], out[i]
            page_plan = plan.pages[i]
            issues.extend(_geometry_issues(page_plan, i + 1))
            if getattr(page_plan, "unsafe_image_cleanup", False):
                issues.append(QAIssue(Severity.ERROR, "UNSAFE_SHARED_IMAGE_CLEANUP", "Legacy image is shared with protected content; destructive cleanup was declined", i+1))
            if page_plan.raster_ocr_checked:
                if page_plan.raster_ocr_error:
                    issues.append(QAIssue(Severity.ERROR, "RASTER_OCR_UNAVAILABLE", page_plan.raster_ocr_error, i+1))
                if page_plan.raster_ocr_unsafe:
                    issues.append(QAIssue(Severity.ERROR, "RASTER_BRAND_UNSAFE_GEOMETRY", "Raster brand was recognized but a safe standalone logo boundary was not established", i+1))
                scan = scan_header(out_page, brand)
                if scan.error:
                    issues.append(QAIssue(Severity.ERROR, "RASTER_OCR_UNAVAILABLE", scan.error, i+1))
                elif scan.hits or scan.unsafe_hits:
                    issues.append(QAIssue(Severity.ERROR, "RASTER_LEGACY_TEXT_REMAINS", "OCR recognizes a configured legacy brand in the rendered output header", i+1))
            if not page_plan.text_extraction_ok:
                issues.append(QAIssue(Severity.ERROR, "TEXT_EXTRACTION_FAILED", "Source text extraction failed during analysis; content cannot be certified", i + 1))

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
                footer_overlaps_body = any(r.y1 > plan.repeated_bands.footer_top for r in page_plan.protected_text_rects + page_plan.protected_visual_rects)
                issues.append(QAIssue(Severity.WARNING if footer_overlaps_body else Severity.ERROR, "REPEATED_FOOTER_NOT_REPLACED", "Repeated footer preserved because protected content makes replacement unsafe" if footer_overlaps_body else "A verified repeated legacy footer exists but this page has no in-place footer replacement plan", i + 1))

            if abs(src_page.rect.width - out_page.rect.width) > size_tol or abs(src_page.rect.height - out_page.rect.height) > size_tol:
                issues.append(QAIssue(Severity.FATAL, "PAGE_SIZE_CHANGED", "Output page dimensions differ from the source", i + 1))

            try:
                src_words = _body_words(src_page, page_plan, brand.legacy_terms)
                out_words = _body_words(out_page, page_plan, brand.legacy_terms, output_coords=True)
                if src_words:
                    retained = _retention(src_words, out_words)
                    retention_values.append(retained)
                    if retained < min_retention:
                        issues.append(QAIssue(Severity.ERROR, "BODY_TEXT_LOSS", f"Body text retention is {retained:.1%}, below {min_retention:.1%}", i + 1))
                elif page_plan.page_type.value != "blank":
                    visual_only_pages += 1
                    issues.append(QAIssue(Severity.INFO, "BODY_TEXT_UNAVAILABLE", "No searchable body words; preservation is verified by rendered pixels rather than text retention", i + 1))
            except Exception as exc:
                issues.append(QAIssue(Severity.ERROR, "TEXT_EXTRACTION_FAILED", str(exc), i + 1))

            changed, render_issues = _rendered_body_check(source_doc, i, out_page, page_plan, brand)
            render_changes.append(changed)
            issues.extend(render_issues)
            try:
                regions = list(page_plan.legacy_logo_rects)
                if page_plan.logo_replace_rect is not None and page_plan.logo_replace_rect not in regions:
                    regions.append(page_plan.logo_replace_rect)
                for region in regions:
                    remaining, template = _raster_logo_residual(src_page, out_page, page_plan, region)
                    if template >= 24:
                        raster_logo_checks += 1
                        if remaining >= 24 and remaining / template > .08:
                            issues.append(QAIssue(Severity.ERROR, "RASTER_LEGACY_LOGO_REMAINS", f"{remaining}/{template} source-logo edge pixels survive outside the replacement emblem", i + 1))
            except Exception as exc:
                issues.append(QAIssue(Severity.ERROR, "RASTER_LOGO_CHECK_FAILED", f"Logo verification failed: {exc}", i + 1))

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

            # Match every searchable body word against its expected mapped box.
            # This verifies X, Y and extent, including intentional crop transforms.
            try:
                positions = _body_positions(out_page, page_plan, brand.legacy_terms, output_coords=True)
                moved = False
                for token, boxes in _body_positions(src_page, page_plan, brand.legacy_terms).items():
                    available = list(positions.get(token, []))
                    for box in boxes:
                        if not available:
                            continue
                        expected = fitz.Rect(*page_plan.map_source_rect(RectData.from_rect(box)).as_tuple())
                        nearest = min(available, key=lambda r: abs(r.x0 - expected.x0) + abs(r.y0 - expected.y0))
                        available.remove(nearest)
                        shift = max(abs(nearest.x0 - expected.x0), abs(nearest.y0 - expected.y0),
                                    abs(nearest.x1 - expected.x1), abs(nearest.y1 - expected.y1))
                        max_observed_shift = max(max_observed_shift, shift)
                        shift_checks += 1
                        if shift > max_shift:
                            issues.append(QAIssue(Severity.ERROR, "BODY_POSITION_SHIFT", f"Body word {token!r} differs from its expected position/extent by {shift:.1f} pt (limit {max_shift:.1f} pt)", i + 1))
                            moved = True
                            break
                    if moved:
                        break
            except Exception as exc:
                issues.append(QAIssue(Severity.ERROR, "BODY_POSITION_CHECK_FAILED", str(exc), i + 1))

            side_hits = _legacy_side_hits(out_page, brand.side_text_terms, side_width)
            if side_hits:
                sev = Severity.ERROR if brand.qa.get("side_text_is_error", False) else Severity.WARNING
                issues.append(QAIssue(sev, "VERTICAL_LEGACY_TEXT_REMAINS", f"Legacy side text remains: {', '.join(side_hits[:3])}", i + 1))

        avg_retention = sum(retention_values) / len(retention_values) if retention_values else None
        strategy_counts = Counter(p.page_strategy.value for p in plan.pages)
        passed = not any(item.severity in {Severity.ERROR, Severity.FATAL} for item in issues)
        return QAReport(
            source=str(plan.source), output=str(output_path), passed=passed,
            page_count_source=len(source_doc), page_count_output=len(out), issues=issues,
            metrics={
                "average_body_text_retention": round(avg_retention, 5) if avg_retention is not None else None,
                "text_retention_pages_verified": len(retention_values),
                "visual_only_pages_verified": visual_only_pages,
                "rendered_pages_checked": len(render_changes),
                "max_body_render_change_ratio": round(max(render_changes, default=0), 6),
                "raster_logo_template_checks": raster_logo_checks,
                "legacy_text_hits": legacy_hits,
                "pages_checked": min(len(source_doc), len(out)),
                "body_position_checks": shift_checks,
                "max_body_position_shift_pt": round(max_observed_shift, 3),
                "page_strategy_counts": dict(strategy_counts),
                "repeated_footer_confidence": round(plan.repeated_bands.footer_confidence, 3),
            },
        )
