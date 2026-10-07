from __future__ import annotations

from pathlib import Path
from io import BytesIO
import math

import pymupdf as fitz

from .image_info import image_info as page_image_info
from PIL import Image, ImageFilter

from .config import BrandingProfile
from .models import DocumentMeta, DocumentPlan, HeaderTemplate, PagePlan, RectData, RenderStrategy
from .native_cleanup import remove_region_text, make_image_transparent


def _rect(r: RectData) -> fitz.Rect:
    return fitz.Rect(*r.as_tuple())


def _fit_text(text: str, font: str, max_size: float, min_size: float, max_width: float) -> float:
    size = max_size
    while size > min_size and fitz.get_text_length(text, fontname=font, fontsize=size) > max_width:
        size -= 0.5
    return size


def _logo_ratio(path: Path) -> float:
    pix = fitz.Pixmap(str(path))
    try:
        return pix.width / max(pix.height, 1)
    finally:
        pix = None


def _draw_logo(page: fitz.Page, path: Path, ratio: float, x: float, y: float, h: float, max_w: float | None = None) -> float:
    w = h * ratio
    if max_w and w > max_w:
        w = max_w
        h = w / ratio
    page.insert_image(fitz.Rect(x, y, x + w, y + h), filename=str(path), keep_proportion=True, overlay=True)
    return w


def _material_label(meta: DocumentMeta) -> str:
    if meta.material_type == "NOTES":
        return meta.chapter
    if meta.material_type == "KEY POINTS":
        return f"{meta.chapter} — KEY POINTS"
    return f"{meta.material_type}  ·  {meta.chapter}"


def draw_header(page: fitz.Page, plan: PagePlan, meta: DocumentMeta, brand: BrandingProfile, logo: Path, ratio: float) -> None:
    if plan.header_rect is None or plan.header_template == HeaderTemplate.NONE:
        return
    r = _rect(plan.header_rect)
    navy, blue, red = brand.color("navy"), brand.color("blue"), brand.color("red")
    gray, white = brand.color("gray"), brand.color("white")
    h = r.height
    page.draw_rect(r, fill=white, color=None, overlay=True)
    page.draw_line(fitz.Point(16, r.y1 - 1.4), fitz.Point(page.rect.width - 16, r.y1 - 1.4), color=blue, width=0.8, overlay=True)
    page.draw_line(fitz.Point(16, 3), fitz.Point(80, 3), color=blue, width=2.0, overlay=True)
    page.draw_line(fitz.Point(80, 3), fitz.Point(106, 3), color=red, width=2.0, overlay=True)

    logo_h = min(31.0, max(20.0, h - 14.0))
    logo_w = _draw_logo(page, logo, ratio, 18, max(6.0, (h - logo_h) / 2), logo_h, max_w=36)
    brand_x = 24 + logo_w
    left_zone_end = page.rect.width * 0.44
    brand_width = max(100.0, left_zone_end - brand_x - 8)
    fs = _fit_text(brand.school_short_name, "hebo", 10.2, 7.2, brand_width)
    baseline = min(28.0, max(13.5, h * 0.34))
    page.insert_text(fitz.Point(brand_x, baseline), brand.school_short_name, fontsize=fs, fontname="hebo", color=navy, overlay=True)
    if h >= 38:
        page.insert_text(fitz.Point(brand_x, baseline + 9.8), brand.school_line2, fontsize=6.3, fontname="hebo", color=red, overlay=True)

    right_x = max(page.rect.width * 0.46, left_zone_end + 8)
    right_w = page.rect.width - right_x - 18
    cs = f"Class {meta.class_name}  |  {meta.subject.title()}"
    page.insert_text(
        fitz.Point(right_x, min(27.0, max(14.0, h * 0.29))), cs,
        fontsize=_fit_text(cs, "hebo", 8.8, 6.4, right_w), fontname="hebo", color=navy, overlay=True,
    )
    label = _material_label(meta)
    label_y = min(54.0, max(30.0, h * 0.61))
    page.insert_text(
        fitz.Point(right_x, label_y), label,
        fontsize=_fit_text(label, "hebo", 10.2, 6.8, right_w), fontname="hebo", color=navy, overlay=True,
    )
    if h >= 49 and (meta.duration or meta.max_marks):
        bits = []
        if meta.duration:
            bits.append(f"Time: {meta.duration}")
        if meta.max_marks:
            bits.append(f"Marks: {meta.max_marks}")
        aux = "   |   ".join(bits)
        page.insert_text(
            fitz.Point(right_x, h - 5.2), aux,
            fontsize=_fit_text(aux, "helv", 6.4, 5.3, right_w), fontname="helv", color=gray, overlay=True,
        )


def draw_footer(page: fitz.Page, plan: PagePlan, meta: DocumentMeta, brand: BrandingProfile, total_pages: int) -> None:
    if plan.footer_rect is None:
        return
    r = _rect(plan.footer_rect)
    blue, gray, white, navy = brand.color("blue"), brand.color("gray"), brand.color("white"), brand.color("navy")
    page.draw_rect(r, fill=white, color=None, overlay=True)
    page.draw_line(fitz.Point(16, r.y0 + 1), fitz.Point(page.rect.width - 16, r.y0 + 1), color=blue, width=0.6, overlay=True)
    left = f"Class {meta.class_name}  |  {meta.subject.title()}  |  {meta.chapter}"
    right = f"{plan.page_number} / {total_pages}"
    lfs = _fit_text(left, "helv", 7.0, 5.6, page.rect.width - 105)
    y = r.y1 - 4.4
    page.insert_text(fitz.Point(18, y), left, fontsize=lfs, fontname="helv", color=gray, overlay=True)
    tw = fitz.get_text_length(right, fontname="hebo", fontsize=7.0)
    page.insert_text(fitz.Point(page.rect.width - 18 - tw, y), right, fontsize=7.0, fontname="hebo", color=navy, overlay=True)


def _apply_redactions(page: fitz.Page, rects: list[RectData], fill: tuple[float, float, float]) -> None:
    if not rects:
        return
    regions = [_rect(item) for item in rects if item.x1 > item.x0 and item.y1 > item.y0]
    if remove_region_text(page, regions):
        for region in regions:
            page.draw_rect(region, fill=fill, color=None, overlay=True)
        return
    for item in rects:
        r = _rect(item)
        if r.width > 0 and r.height > 0:
            page.add_redact_annot(r, fill=fill)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_PIXELS,
        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )


def _flat_logo_background_patch(page: fitz.Page, r: fitz.Rect, bg: tuple[float, float, float]) -> bytes | None:
    """Return a feathered full-clean patch when the logo sits on a locally flat field.

    The real Key Points pages use large ALLEN artwork with a faint dark shadow.
    Pixel-selective cleanup can remove the white letters yet leave that low-contrast
    shadow visible.  When most of the replacement zone already matches one local
    background colour, the safest operation is to rebuild the *interior* of that
    zone from the sampled background and then place the transparent school emblem.

    The outer ~3 pt frame is protected and the patch edge is feathered so nearby
    page rules / lane separators are not flattened.  Non-uniform / textured regions
    fall back to selective foreground cleanup below.
    """
    if r.width < 8 or r.height < 8:
        return None
    scale = 3.0
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=r, alpha=False)
    except Exception:
        return None
    if pix.width < 8 or pix.height < 8 or pix.n < 3:
        return None

    mode = "RGB" if pix.n == 3 else "CMYK"
    try:
        src = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        if mode != "RGB":
            src = src.convert("RGB")
    except Exception:
        return None

    bg8 = tuple(max(0, min(255, int(round(c * 255)))) for c in bg)
    distances: list[float] = []
    for rr, gg, bb in src.get_flattened_data() if hasattr(src, "get_flattened_data") else src.getdata():
        distances.append(((rr - bg8[0]) ** 2 + (gg - bg8[1]) ** 2 + (bb - bg8[2]) ** 2) ** 0.5)
    if not distances:
        return None
    distances.sort()
    p55 = distances[min(len(distances) - 1, int(len(distances) * 0.55))]
    close_fraction = sum(d <= 8.0 for d in distances) / len(distances)

    # Require a genuine flat/background-dominated region.  This is intentionally
    # strict: if the artwork varies materially, preserve it and use the selective
    # cleanup path instead.
    if p55 > 6.0 or close_fraction < 0.55:
        return None

    mask = Image.new("L", src.size, 0)
    mp = mask.load()
    inset = max(5, int(round(scale * 2.6)))  # protect ~2.6 PDF points on every side
    if src.width <= 2 * inset + 2 or src.height <= 2 * inset + 2:
        return None
    for y in range(inset, src.height - inset):
        for x in range(inset, src.width - inset):
            mp[x, y] = 255
    # Feather only a little; the flatness test means this is mainly anti-seam insurance.
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(1.0, scale * 0.7)))
    patch = Image.new("RGBA", src.size, bg8 + (0,))
    patch.putalpha(mask)
    buf = BytesIO()
    patch.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _selective_logo_cleanup_patch(page: fitz.Page, r: fitz.Rect, bg: tuple[float, float, float]) -> bytes | None:
    """Create a transparent overlay that removes *both* light and dark legacy-logo pixels.

    Designed pages frequently place the old logo on a dark flat/graded field. Some
    source logos are bright, while others are dark blue/black or have a dark outline.
    V3.4.4 assumed a foreground direction from page luminance, which could leave a
    faint dark ``ALLEN`` ghost on brown/navy/green pages.

    V3.4.6 first uses a flat-field rebuild when safe; otherwise it measures colour-distance from the locally sampled background and
    uses an adaptive threshold derived from the crop edge noise. This is intentionally
    bidirectional: sufficiently different pixels are removed whether they are lighter
    or darker than the background. Only the detected foreground receives an opaque
    background-colour repair; the rest of the designed artwork remains untouched.
    """
    if r.width < 4 or r.height < 4:
        return None
    scale = 3.0
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=r, alpha=False)
    except Exception:
        return None
    if pix.width < 3 or pix.height < 3 or pix.n < 3:
        return None

    mode = "RGB" if pix.n == 3 else "CMYK"
    try:
        src = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        if mode != "RGB":
            src = src.convert("RGB")
    except Exception:
        return None

    bg8 = tuple(max(0, min(255, int(round(c * 255)))) for c in bg)
    bg_luma = 0.2126 * bg8[0] + 0.7152 * bg8[1] + 0.0722 * bg8[2]
    sp = src.load()

    # Estimate how much ordinary JPEG/gradient variation exists around the crop
    # boundary. Sparse border rules are deliberately tolerated by using a robust
    # percentile rather than the maximum deviation.
    edge_probe = max(3, int(round(min(src.size) * 0.075)))
    border_deltas: list[float] = []
    for y in range(src.height):
        for x in range(src.width):
            if not (x < edge_probe or x >= src.width - edge_probe or y < edge_probe or y >= src.height - edge_probe):
                continue
            rr, gg, bb = sp[x, y]
            d = ((rr - bg8[0]) ** 2 + (gg - bg8[1]) ** 2 + (bb - bg8[2]) ** 2) ** 0.5
            border_deltas.append(d)
    if border_deltas:
        border_deltas.sort()
        # A logo can touch the crop edge, so a high percentile can mistake the
        # logo itself for background noise.  Use the median edge deviation instead.
        p50 = border_deltas[min(len(border_deltas) - 1, int(len(border_deltas) * 0.50))]
    else:
        p50 = 0.0

    # Lower floor than V3.4.5 so low-contrast dark shadows cannot survive.  The
    # selective path is used only when the flat-field rebuild was rejected.
    threshold = max(10.0, min(22.0, p50 * 1.45 + 5.0))

    mask = Image.new("L", src.size, 0)
    mp = mask.load()
    # Preserve a slightly wider outer frame. This protects page borders / lane
    # separators that sometimes touch the fallback box on infographic pages.
    protect = max(3, int(round(min(src.size) * 0.035)))
    for y in range(protect, src.height - protect):
        for x in range(protect, src.width - protect):
            rr, gg, bb = sp[x, y]
            dr, dg, db = rr - bg8[0], gg - bg8[1], bb - bg8[2]
            dist = (dr * dr + dg * dg + db * db) ** 0.5
            if dist < threshold:
                continue
            lum = 0.2126 * rr + 0.7152 * gg + 0.0722 * bb
            max_channel = max(abs(dr), abs(dg), abs(db))
            # Requiring a modest luminance/channel departure avoids treating tiny
            # compression noise as foreground while still catching dark-blue text.
            if abs(lum - bg_luma) >= 6.0 or max_channel >= 10:
                mp[x, y] = 255

    # Expand around detected cores to remove anti-aliased fringes and thin outlines.
    # At 3x render scale a 7px max-filter is only about one PDF point each side.
    mask = mask.filter(ImageFilter.MaxFilter(37))
    # Dilation must not consume page rules that touch the fallback rectangle.
    # Re-clear the protected outer frame after expansion.
    mp = mask.load()
    protect_left = max(3, protect - 1)
    protect_right = max(protect + 4, int(round(min(src.size) * 0.055)))
    protect_top = protect
    protect_bottom = protect
    for y in range(mask.height):
        for x in range(mask.width):
            if (
                x < protect_left
                or x >= mask.width - protect_right
                or y < protect_top
                or y >= mask.height - protect_bottom
            ):
                mp[x, y] = 0
    if mask.getbbox() is None:
        return None

    patch = Image.new("RGBA", src.size, bg8 + (0,))
    patch.putalpha(mask)
    buf = BytesIO()
    patch.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _uniform_background_clip(page: fitz.Page, r: fitz.Rect, bg: tuple[float, float, float]) -> fitz.Rect | None:
    """Find a small, blank source field immediately outside the verified logo box."""
    size = 6.0
    bg8 = tuple(round(c * 255) for c in bg)
    page_area = page.rect.get_area()
    # Page-wide background paths are expected. Smaller artwork/text must not be
    # copied, even when a bounding box happens to contain mostly background pixels.
    obstacles = [
        drawing["rect"] for drawing in page.get_drawings()
        if drawing["rect"].get_area() < page_area * 0.5
    ]
    obstacles.extend(fitz.Rect(word[:4]) for word in page.get_text("words"))
    probes = [
        fitz.Rect(r.x1 + 3, r.y0, r.x1 + 20, r.y1),
        fitz.Rect(r.x0 - 18, r.y0, r.x0 - 3, r.y1),
        fitz.Rect(r.x0, r.y0 - 14, r.x1, r.y0 - 3),
        fitz.Rect(r.x0, r.y1 + 3, r.x1, r.y1 + 18),
    ]
    for probe in probes:
        strip = probe & page.rect
        if strip.is_empty or strip.width < size or strip.height < size:
            continue
        # Render each search strip once. Re-rendering this complex PDF for
        # every 6pt probe made illustrated pages hundreds of times slower.
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=strip, alpha=False)
        if pix.n != 3:
            continue
        strip_image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        for y in range(math.ceil(strip.y0), math.floor(strip.y1 - size) + 1, 3):
            for x in range(math.ceil(strip.x0), math.floor(strip.x1 - size) + 1, 3):
                clip = fitz.Rect(x, y, x + size, y + size)
                if any(clip.intersects(obstacle) for obstacle in obstacles):
                    continue
                left, top = x * 3 - pix.x, y * 3 - pix.y
                data = strip_image.crop((left, top, left + int(size * 3), top + int(size * 3))).tobytes()
                if all(
                    max(abs(data[i + channel] - bg8[channel]) for channel in range(3)) <= 4
                    for i in range(0, len(data), pix.n)
                ):
                    return clip
    return None


def _repair_native_flat_background(
    page: fitz.Page, src_doc: fitz.Document, index: int,
    r: fitz.Rect, bg: tuple[float, float, float],
) -> bool:
    """Repair only a verified flat logo interior using the source PDF's own colors.

    CMYK/ICC transparency can compose differently after a page is imported and
    across viewers. Baking the source's sampled RGB into a PNG leaves a gray patch
    on the brown Key Points page. A blank native source clip retains its color
    space and transparency, so the repair follows the surrounding artwork.
    """
    src_page = src_doc[index]
    if _flat_logo_background_patch(src_page, r, bg) is None:
        return False
    inset = 2.6
    interior = fitz.Rect(r.x0 + inset, r.y0 + inset, r.x1 - inset, r.y1 - inset)
    if min(bg) >= .995:
        # An unpainted white PDF field is transparent when imported as a form;
        # copying it cannot cover old ink. White requires an opaque repair.
        page.draw_rect(interior, fill=(1, 1, 1), color=None, overlay=True)
        return True
    clip = _uniform_background_clip(src_page, r, bg)
    if clip is None:
        return False
    page.show_pdf_page(interior, src_doc, index, clip=clip, keep_proportion=False, overlay=True)
    return True


def _replace_logo_only(
    page: fitz.Page, plan: PagePlan, brand: BrandingProfile,
    color_logo: Path, color_ratio: float, src_doc: fitz.Document, index: int,
    authorized_image_regions: dict[int, list[fitz.Rect]] | None = None,
) -> None:
    if plan.logo_replace_rect is None:
        return
    r = _rect(plan.logo_replace_rect)
    bg = plan.logo_background or (1, 1, 1)
    image_repaired = False
    known = {bytes.fromhex(value) for value in brand.raw.get("legacy", {}).get("raster_logo_digests", [])}
    known |= {bytes.fromhex(value) for value in plan.legacy_image_digests}
    for image in page_image_info(page):
        if image.get("digest") in known and image.get("xref") and r.contains(fitz.Rect(image["bbox"])):
            # A verified standalone raster logo can be made transparent in its
            # native image object. This retains the original gradient/artwork
            # beneath it instead of painting a sampled-colour patch.
            if not make_image_transparent(page.parent, image["xref"], authorized_image_regions):
                plan.unsafe_image_cleanup = True
                plan.reasons.append(f"UNSAFE_SHARED_IMAGE_XREF: image {image['xref']} has an unverified placement or dependency; raster logo preserved for QA")
                return
            image_repaired = True

    # Remove searchable legacy text only at its exact hit rectangle.  Do not wipe
    # the whole logo zone: that was the source of the visible dark square in v3.4.3.
    tight_hits: list[fitz.Rect] = []
    for term in brand.legacy_terms:
        try:
            hits = page.search_for(term)
        except Exception:
            hits = []
        for hit in hits:
            inter = hit & r
            if inter.is_empty:
                continue
            pad = 1.5
            tight_hits.append(fitz.Rect(
                max(r.x0, hit.x0 - pad), max(r.y0, hit.y0 - pad),
                min(r.x1, hit.x1 + pad), min(r.y1, hit.y1 + pad),
            ))
    if tight_hits:
        _apply_redactions(page, [RectData.from_rect(hit) for hit in tight_hits], bg)

    # For raster/vector logos that are not searchable, cover only the actual legacy
    # foreground pixels. This preserves gradients, textures and illustrations around
    # the mark and eliminates the rectangular cleanup patch.
    # If the source logo sits on a genuinely flat field, rebuild the protected
    # interior completely.  This removes the low-contrast ALLEN shadow that can
    # survive pixel-threshold cleanup.  Textured regions still use selective repair.
    # A standalone logo may include an opaque background different from the
    # underlying artwork. Restore a verified matching source field even after
    # neutralizing its image; otherwise transparency can reveal a foreign color.
    native_field_repaired = _repair_native_flat_background(page, src_doc, index, r, bg)
    if not image_repaired and not native_field_repaired:
        patch = _flat_logo_background_patch(page, r, bg)
        if patch is None:
            patch = _selective_logo_cleanup_patch(page, r, bg)
        if patch:
            page.insert_image(r, stream=patch, keep_proportion=False, overlay=True)

    luma = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]
    logo = brand.white_logo_path if luma < 0.48 and brand.white_logo_path else color_logo
    ratio = _logo_ratio(logo) if logo != color_logo else color_ratio
    pad = max(1.5, min(r.width, r.height) * 0.05)
    avail_w, avail_h = max(5.0, r.width - 2 * pad), max(5.0, r.height - 2 * pad)
    logo_w = min(avail_w, 58.0)
    logo_h = logo_w / ratio
    if logo_h > avail_h:
        logo_h = avail_h
        logo_w = logo_h * ratio
    x = r.x0 + (r.width - logo_w) / 2
    y = r.y0 + (r.height - logo_h) / 2
    page.insert_image(fitz.Rect(x, y, x + logo_w, y + logo_h), filename=str(logo), keep_proportion=True, overlay=True)


def _render_overlay_page(dst: fitz.Page, src_doc: fitz.Document, index: int, plan: PagePlan, meta: DocumentMeta, brand: BrandingProfile, logo: Path, ratio: float, total: int) -> None:
    # 1:1 placement: body coordinates never move in the safe default strategy.
    _apply_redactions(dst, plan.cleanup_rects + plan.vertical_text_rects, brand.color("white"))
    if plan.footer_rect is not None:
        _apply_redactions(dst, [plan.footer_rect], brand.color("white"))
    draw_header(dst, plan, meta, brand, logo, ratio)
    draw_footer(dst, plan, meta, brand, total)


def _render_crop_page(dst: fitz.Page, src_doc: fitz.Document, index: int, plan: PagePlan, meta: DocumentMeta, brand: BrandingProfile, logo: Path, ratio: float, total: int) -> None:
    # Explicit opt-in only. Never used by the default SSKEMS profile.
    src_page = src_doc[index]
    plan.prepare_crop_geometry(src_page.rect.width, src_page.rect.height)
    clip = _rect(plan.source_clip_rect)
    content = _rect(plan.map_source_rect(plan.source_clip_rect))
    dst.show_pdf_page(content, src_doc, index, clip=clip, keep_proportion=False)
    mapped_cleanup = []
    for source_rect in plan.vertical_text_rects:
        clipped = _rect(source_rect) & clip
        if not clipped.is_empty:
            mapped_cleanup.append(plan.map_source_rect(RectData.from_rect(clipped)))
    # PDF form clipping hides ink but may leave off-clip source text searchable.
    # Remove the destination bands before inserting new searchable branding.
    mapped_cleanup.extend(r for r in (plan.header_rect, plan.footer_rect) if r is not None)
    _apply_redactions(dst, mapped_cleanup, brand.color("white"))
    draw_header(dst, plan, meta, brand, logo, ratio)
    draw_footer(dst, plan, meta, brand, total)


def render_document(src_doc: fitz.Document, plan: DocumentPlan, brand: BrandingProfile) -> fitz.Document:
    logo = brand.logo_path
    ratio = _logo_ratio(logo)
    # Clone the complete document, including catalog color state, page groups,
    # annotations and metadata. Page-only imports can change CMYK/ICC artwork.
    out = fitz.open(stream=src_doc.tobytes(), filetype="pdf")
    total = len(src_doc)
    authorized_image_regions: dict[int, list[fitz.Rect]] = {}
    for index, page_plan in enumerate(plan.pages):
        if page_plan.strategy == RenderStrategy.REPLACE_LOGO_ONLY and page_plan.logo_replace_rect:
            authorized_image_regions[index] = [_rect(page_plan.logo_replace_rect)]
        elif page_plan.strategy == RenderStrategy.OVERLAY:
            authorized_image_regions[index] = [
                _rect(legacy) for legacy in page_plan.legacy_logo_rects
                if any(_rect(cleanup).contains(_rect(legacy)) for cleanup in page_plan.cleanup_rects)
            ]
        authorized_image_regions.setdefault(index, []).extend(
            _rect(page_plan.map_source_rect(region)) for region in page_plan.additional_logo_rects)
    for index, page_plan in enumerate(plan.pages):
        src_page = src_doc[index]
        dst = out[index]
        if page_plan.page_type.value == "blank" and not src_page.get_contents():
            continue
        if page_plan.strategy == RenderStrategy.PRESERVE:
            # Preserve designed artwork 1:1, but verified legacy margin/footer
            # elements may still be replaced in-place.
            _apply_redactions(dst, page_plan.vertical_text_rects, brand.color("white"))
            if page_plan.footer_rect is not None:
                _apply_redactions(dst, [page_plan.footer_rect], brand.color("white"))
            draw_footer(dst, page_plan, plan.meta, brand, total)
        elif page_plan.strategy == RenderStrategy.REPLACE_LOGO_ONLY:
            _apply_redactions(dst, page_plan.vertical_text_rects, page_plan.logo_background or brand.color("white"))
            _replace_logo_only(dst, page_plan, brand, logo, ratio, src_doc, index, authorized_image_regions)
            if page_plan.footer_rect is not None:
                _apply_redactions(dst, [page_plan.footer_rect], brand.color("white"))
            draw_footer(dst, page_plan, plan.meta, brand, total)
        elif page_plan.strategy == RenderStrategy.REBUILD_CROP:
            empty = out.get_new_xref()
            out.update_object(empty, "<< >>")
            out.update_stream(empty, b"")
            dst.set_contents(empty)
            _render_crop_page(dst, src_doc, index, page_plan, plan.meta, brand, logo, ratio, total)
        else:
            _render_overlay_page(dst, src_doc, index, page_plan, plan.meta, brand, logo, ratio, total)
        if page_plan.additional_logo_rects:
            from .analysis import sample_local_background
            main_region, main_background = page_plan.logo_replace_rect, page_plan.logo_background
            try:
                for region in page_plan.additional_logo_rects:
                    if page_plan.source_clip_rect and not _rect(page_plan.source_clip_rect).contains(_rect(region)):
                        continue  # The declared crop already removes this margin.
                    page_plan.logo_replace_rect = page_plan.map_source_rect(region)
                    page_plan.logo_background = sample_local_background(src_page, _rect(region))
                    _replace_logo_only(dst, page_plan, brand, logo, ratio, src_doc, index, authorized_image_regions)
            finally:
                page_plan.logo_replace_rect, page_plan.logo_background = main_region, main_background
    return out
