from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from .config import BrandingProfile
from .models import (
    DocumentPlan,
    DocumentProfile,
    HeaderTemplate,
    PageAnalysis,
    PagePlan,
    PageStrategy,
    PageType,
    RectData,
    RenderStrategy,
    RepeatedBands,
)


def choose_header_template(material_type: str) -> HeaderTemplate:
    if material_type in {"TEST", "PRACTICE TEST", "CHAPTER TEST", "DPP"}:
        return HeaderTemplate.TEST
    if material_type in {"EXERCISE", "PRACTICE EXERCISE", "PRACTICE SHEET", "NCERT PRACTICE", "NCERT SOLUTIONS"}:
        return HeaderTemplate.PRACTICE
    if material_type == "KEY POINTS":
        return HeaderTemplate.NONE
    return HeaderTemplate.NOTES


def _max_legacy_bottom(a: PageAnalysis) -> float:
    return max((r.y1 for r in a.legacy_rects), default=0.0)


def _safe_header_geometry(
    a: PageAnalysis, repeated: RepeatedBands, profile: BrandingProfile
) -> tuple[float, float, bool, list[str]]:
    """Return cleanup depth, adaptive visible header height, safety, reasons.

    Cleanup and rendering are deliberately separate.  The old brand may occupy a
    deeper band than the new header, but V3.4.3 lets the visible SSKEMS header expand
    modestly into the verified legacy space rather than always using a fixed 54 pt.
    """
    visual_target = float(profile.layout("header_height", 54))
    visual_max = float(profile.layout("header_adaptive_max_height", 76))
    minimum = float(profile.layout("header_min_height", 42))
    cleanup_maximum = float(profile.layout("cleanup_max_height", 150))
    gap = float(profile.layout("safety_gap", 7))
    legacy_bottom = _max_legacy_bottom(a)

    cleanup_bottom = max(visual_target, repeated.header_bottom, legacy_bottom)
    reasons: list[str] = []
    if repeated.header_bottom:
        reasons.append(f"repeated header band ends at y={repeated.header_bottom:.1f}")
    if a.legacy_rects:
        reasons.append(f"legacy artwork extends to y={legacy_bottom:.1f}")

    cleanup_bottom = min(cleanup_bottom, cleanup_maximum)
    if a.first_content_y is not None:
        hard_limit = max(0.0, a.first_content_y - gap)
        if cleanup_bottom > hard_limit:
            cleanup_bottom = hard_limit
            reasons.append(f"cleanup bounded before first academic content y={a.first_content_y:.1f}")

    safe = cleanup_bottom >= minimum
    visible_height = min(cleanup_bottom, visual_max) if safe else 0.0
    if safe:
        visible_height = max(min(visual_target, cleanup_bottom), visible_height)
        reasons.append(
            f"legacy cleanup y=0..{cleanup_bottom:.1f}; adaptive SSKEMS header y=0..{visible_height:.1f}"
        )
    else:
        reasons.append(
            f"only {cleanup_bottom:.1f} pt is safely available; minimum dynamic header is {minimum:.1f} pt"
        )
    return max(0.0, cleanup_bottom), max(0.0, visible_height), safe, reasons


def _safe_footer_rect(
    a: PageAnalysis, repeated: RepeatedBands, profile: BrandingProfile
) -> tuple[RectData | None, list[str]]:
    height = float(profile.layout("footer_height", 18))
    gap = float(profile.layout("safety_gap", 7))
    if height <= 0:
        return None, []
    desired_top = a.height - height
    reasons: list[str] = []
    top = desired_top

    # A verified repeated legacy band wins.  The complete source band is erased,
    # while the replacement footer is drawn inside the same rectangle.
    if repeated.footer_top is not None and repeated.footer_confidence >= 0.55:
        candidate = float(repeated.footer_top)
        max_replace = float(profile.layout("footer_max_replacement_height", 60))
        band_height = a.height - candidate
        body_clear = a.last_content_y is None or a.last_content_y <= candidate - gap
        if 0 < band_height <= max_replace and body_clear:
            top = candidate
            reasons.append(
                f"verified repeated graphic/text footer begins at y={candidate:.1f}; replace old footer in-place"
            )
        elif candidate >= desired_top - 8:
            top = min(candidate, desired_top)
            reasons.append(f"repeated footer boundary near page edge y={candidate:.1f}")

    if a.last_content_y is not None and a.last_content_y > top - gap:
        return None, [f"footer disabled: body content reaches y={a.last_content_y:.1f}"]
    return RectData(0, top, a.width, a.height), reasons


def _first_page_fallback(
    a: PageAnalysis,
    meta,
    brand: BrandingProfile,
    cleanup_bottom: float,
    visible_header_height: float,
    header_safe: bool,
    reasons: list[str],
) -> tuple[float, float, bool, bool]:
    """Apply a controlled visual-logo fallback and report whether it was used."""
    gap = float(brand.layout("safety_gap", 7))
    fallback_used = False
    fallback_targets = brand.raw.get("legacy", {}).get("first_page_cleanup_targets", {})
    if a.page_number != 1 or a.legacy_rects or not isinstance(fallback_targets, dict):
        return cleanup_bottom, visible_header_height, header_safe, fallback_used
    raw_target = fallback_targets.get(meta.material_type)
    if raw_target is None:
        return cleanup_bottom, visible_header_height, header_safe, fallback_used

    target = float(raw_target)
    hard_limit = (a.first_content_y - gap) if a.first_content_y is not None else target
    fallback_bottom = min(target, hard_limit)
    minimum = float(brand.layout("header_min_height", 42))
    if fallback_bottom > cleanup_bottom + 2 and fallback_bottom >= minimum:
        cleanup_bottom = fallback_bottom
        header_safe = True
        visible_header_height = min(
            cleanup_bottom,
            float(brand.layout("header_adaptive_max_height", 76)),
        )
        visible_header_height = max(
            min(float(brand.layout("header_height", 54)), cleanup_bottom),
            visible_header_height,
        )
        fallback_used = True
        reasons.append(
            f"first-page unsearchable-logo fallback extends cleanup to y={cleanup_bottom:.1f} for {meta.material_type}"
        )
    return cleanup_bottom, visible_header_height, header_safe, fallback_used


def make_document_plan(
    source: Path,
    meta,
    doc_profile: DocumentProfile,
    repeated: RepeatedBands,
    analyses: list[PageAnalysis],
    brand: BrandingProfile,
) -> DocumentPlan:
    pages: list[PagePlan] = []
    template = choose_header_template(meta.material_type)
    allow_crop = bool(brand.layout("allow_rebuild_crop", False))
    gap = float(brand.layout("safety_gap", 7))

    for a in analyses:
        geometry = dict(
            protected_text_rects=a.protected_text_rects,
            protected_visual_rects=a.protected_visual_rects,
            text_extraction_ok=a.text_extraction_ok,
            image_only=a.image_only,
            legacy_logo_rects=a.legacy_logo_rects,
            legacy_image_digests=a.legacy_image_digests,
            raster_ocr_checked=a.raster_ocr_checked,
            raster_ocr_error=a.raster_ocr_error,
            raster_ocr_unsafe=a.raster_ocr_unsafe,
        )
        if a.page_type == PageType.BLANK:
            pages.append(PagePlan(
                page_number=a.page_number,
                page_type=a.page_type,
                strategy=RenderStrategy.PRESERVE,
                page_strategy=PageStrategy.LEAVE_UNTOUCHED,
                header_template=HeaderTemplate.NONE,
                confidence=1.0,
                reasons=["blank page remains untouched"],
                **geometry,
            ))
            continue

        # Designed / dark pages never receive the generic dynamic header.  They may
        # still get surgical legacy-logo replacement, verified side-text cleanup and
        # an in-place footer replacement if a repeated source footer exists.
        if a.page_type in {PageType.DESIGNED, PageType.DARK}:
            logo_rect = a.legacy_rects[0] if a.legacy_rects else None
            # Designed pages are preserved 1:1.  Do not impose the generic dynamic
            # footer even if the rest of the document has a repeated source footer.
            # The page's original designed footer/page marker is left intact.
            footer_rect = None
            f_reasons = ["designed page: generic dynamic footer suppressed"]
            if logo_rect:
                render_strategy = RenderStrategy.REPLACE_LOGO_ONLY
                page_strategy = PageStrategy.DESIGNED_PAGE_LOGO_ONLY
                confidence = 0.97
                detail = "replace verified legacy logo only; preserve designed artwork 1:1"
            else:
                render_strategy = RenderStrategy.PRESERVE
                page_strategy = PageStrategy.DESIGNED_PAGE_PRESERVE
                confidence = 0.92 if "designed chapter opener" in " ".join(a.reasons) else 0.78
                detail = "preserve designed artwork 1:1; generic header insertion forbidden"
            pages.append(PagePlan(
                page_number=a.page_number,
                page_type=a.page_type,
                strategy=render_strategy,
                page_strategy=page_strategy,
                header_template=HeaderTemplate.NONE,
                confidence=confidence,
                footer_rect=footer_rect,
                vertical_text_rects=a.vertical_text_rects,
                logo_replace_rect=logo_rect,
                logo_background=a.local_logo_background,
                reasons=a.reasons + [detail] + f_reasons,
                **geometry,
            ))
            continue

        cleanup_bottom, visible_header_height, header_safe, h_reasons = _safe_header_geometry(a, repeated, brand)
        cleanup_bottom, visible_header_height, header_safe, fallback_used = _first_page_fallback(
            a, meta, brand, cleanup_bottom, visible_header_height, header_safe, h_reasons
        )
        footer_rect, f_reasons = _safe_footer_rect(a, repeated, brand)
        if not a.text_extraction_ok:
            header_safe, footer_rect = False, None
            h_reasons.append("text extraction failed: preserve content; QA requires explicit failure")
        cleanup_rects: list[RectData] = []
        header_rect: RectData | None = None
        reasons = list(a.reasons) + h_reasons + f_reasons

        if header_safe:
            cleanup_rects.append(RectData(0, 0, a.width, cleanup_bottom))
            # A corner logo can extend below the compact replacement header.
            # Its independently verified rectangle still needs full cleanup.
            cleanup_rects.extend(a.legacy_rects)
            header_rect = RectData(0, 0, a.width, visible_header_height)
            template_for_page = template
            render_strategy = RenderStrategy.OVERLAY
            page_strategy = (
                PageStrategy.FIRST_PAGE_LARGE_LOGO_REPLACEMENT
                if fallback_used or (a.page_number == 1 and cleanup_bottom > visible_header_height + 8)
                else PageStrategy.STANDARD_HEADER_FOOTER_REPLACEMENT
            )
            reasons.append(
                f"replace legacy top band in-place through y={cleanup_bottom:.1f}; body coordinates unchanged"
            )
        elif a.legacy_rects or a.vertical_text_rects:
            if a.legacy_rects:
                render_strategy = RenderStrategy.REPLACE_LOGO_ONLY
            else:
                render_strategy = RenderStrategy.OVERLAY
            template_for_page = HeaderTemplate.NONE
            page_strategy = PageStrategy.LEGACY_ONLY_CLEANUP
            reasons.append("dynamic header unsafe; remove only verified legacy regions")
        else:
            template_for_page = HeaderTemplate.NONE
            render_strategy = RenderStrategy.PRESERVE
            page_strategy = PageStrategy.LEAVE_UNTOUCHED
            reasons.append("no safe header band and no verified legacy region; leave page untouched")

        crop_top = max(repeated.header_bottom, _max_legacy_bottom(a))
        if a.first_content_y is not None:
            crop_top = min(crop_top, max(0.0, a.first_content_y - gap))
        crop_bottom = max(0.0, a.height - repeated.footer_top) if repeated.footer_top is not None else 0.0
        if a.last_content_y is not None:
            crop_bottom = min(crop_bottom, max(0.0, a.height - a.last_content_y - gap))
        crop_safe = (
            a.text_extraction_ok
            and not a.image_only
            and header_safe
            and repeated.header_confidence >= 0.72
            and crop_top > 0
            and crop_top + .1 >= max(repeated.header_bottom, _max_legacy_bottom(a))
            and (a.first_content_y is None or crop_top <= a.first_content_y - gap + 0.1)
            and (repeated.footer_top is None or repeated.footer_confidence >= 0.65)
            and (repeated.footer_top is None or a.last_content_y is None or a.last_content_y + gap <= repeated.footer_top)
        )
        if allow_crop and crop_safe and render_strategy == RenderStrategy.OVERLAY:
            render_strategy = RenderStrategy.REBUILD_CROP
            reasons.append("crop/reflow explicitly enabled by profile after high-confidence safety checks")

        confidence = min(0.99, max(
            0.50,
            doc_profile.confidence * 0.52
            + max(repeated.header_confidence, 0.35) * 0.28
            + (0.15 if header_safe else 0.0)
            + (0.05 if a.legacy_rects else 0.0),
        ))

        pages.append(PagePlan(
            page_number=a.page_number,
            page_type=a.page_type,
            strategy=render_strategy,
            page_strategy=page_strategy,
            header_template=template_for_page,
            confidence=confidence,
            header_rect=header_rect,
            footer_rect=footer_rect,
            cleanup_rects=cleanup_rects,
            logo_replace_rect=a.legacy_rects[0] if render_strategy == RenderStrategy.REPLACE_LOGO_ONLY else None,
            logo_background=a.local_logo_background,
            vertical_text_rects=a.vertical_text_rects,
            recommended_crop_top=crop_top,
            recommended_crop_bottom=crop_bottom,
            crop_is_safe=crop_safe,
            reasons=reasons,
            **geometry,
        ))
        if render_strategy == RenderStrategy.REBUILD_CROP:
            pages[-1].prepare_crop_geometry(a.width, a.height)

    # Every independently verified mark needs an explicit operation, including
    # footer marks and secondary logos outside the main header cleanup band.
    for page in pages:
        covered = page.cleanup_rects + ([page.footer_rect] if page.footer_rect else [])
        if page.logo_replace_rect:
            covered.append(page.logo_replace_rect)
        page.additional_logo_rects = [region for region in page.legacy_logo_rects
                                      if not any(fitz.Rect(*existing.as_tuple()).contains(fitz.Rect(*region.as_tuple()))
                                                 for existing in covered)]
    return DocumentPlan(source=source, meta=meta, profile=doc_profile, repeated_bands=repeated, pages=pages)
