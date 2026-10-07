"""OCR rendered header/footer bands; never infer deletion from an OCR word box."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import pymupdf as fitz

from .image_info import image_info as page_image_info


@dataclass
class RasterBrandScan:
    hits: list[tuple[fitz.Rect, str]] = field(default_factory=list)
    unsafe_hits: list[fitz.Rect] = field(default_factory=list)
    error: str | None = None


def _standalone_brand(page, item, terms, model):
    """Prove the candidate image itself is a logo, independently of its X position."""
    pixmap = fitz.Pixmap(page.parent, item["xref"])
    mask_kind, mask_value = page.parent.xref_get_key(item["xref"], "SMask")
    if mask_kind == "xref" and not pixmap.alpha:
        pixmap = fitz.Pixmap(pixmap, fitz.Pixmap(page.parent, int(mask_value.split()[0])))
    if pixmap.colorspace and pixmap.colorspace.n != 3:
        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
    # White logos need a dark field; black logos need a light one. Never infer
    # deletion geometry from the OCR line box, whose height can be exaggerated.
    width = min(600, max(180, pixmap.width))
    height = width * pixmap.height / pixmap.width
    for background in ((1, 1, 1), (0, 0, 0)) if pixmap.alpha else ((1, 1, 1),):
        with fitz.open() as isolated:
            surface = isolated.new_page(width=width, height=height)
            surface.draw_rect(surface.rect, color=None, fill=background)
            surface.insert_image(surface.rect, pixmap=pixmap)
            rendered = surface.get_pixmap(matrix=fitz.Matrix(3, 3), colorspace=fitz.csRGB, alpha=False)
            rendered.set_dpi(216, 216)
            with fitz.open(stream=rendered.pdfocr_tobytes(language="eng", tessdata=str(model.parent)), filetype="pdf") as ocr:
                words = {re.sub("[^A-Z]", "", word[4].upper()) for word in ocr[0].get_text("words")}
            words.discard("")
            if words & terms and not words - terms - {"DIGITAL"}:
                return True
    return False


def scan_header(page: fitz.Page, brand) -> RasterBrandScan:
    """Scan image-bearing header and footer bands (public name kept for callers)."""
    raw = brand.raw.get("assets", {}).get("ocr_eng")
    if not raw:
        return RasterBrandScan(error="No OCR language model configured")
    model = Path(raw)
    if not model.is_absolute():
        model = brand.source_path.parent / model
    model = model.resolve()
    terms = {re.sub("[^A-Z]", "", term.upper()) for term in brand.legacy_terms}
    # Clean output bands need only bounds. Resolve native image identities
    # lazily after a brand word is recognized; importing background forms can
    # leave thousands of unused image resources on an otherwise clean page.
    images = page.get_image_info()
    resolved = False
    height = page.rect.height
    width = page.rect.width
    bands = [fitz.Rect(0, 0, width, min(140, height * .17)),
             fitz.Rect(0, height - min(90, height * .13), width, height)]
    clips = []
    for band in bands:
        if not any(fitz.Rect(image["bbox"]).intersects(band) for image in images):
            continue
        for x0, x1 in ((0, width * .44), (width * .56, width), (width * .20, width * .80)):
            clips.append((fitz.Rect(x0, band.y0, x1, band.y1), band))
    result, seen, verified = RasterBrandScan(), set(), {}
    try:
        display_list = page.get_displaylist()
        for clip, band in clips:
            pixmap = display_list.get_pixmap(matrix=fitz.Matrix(3, 3), clip=clip, colorspace=fitz.csRGB, alpha=False)
            pixmap.set_dpi(216, 216)
            data = pixmap.pdfocr_tobytes(language="eng", tessdata=str(model.parent))
            with fitz.open(stream=data, filetype="pdf") as ocr:
                words = ocr[0].get_text("words")
            for word in words:
                if re.sub("[^A-Z]", "", word[4].upper()) not in terms:
                    continue
                if not resolved:
                    images = page_image_info(page)
                    resolved = True
                box = fitz.Rect(word[:4]) + (pixmap.x/3, pixmap.y/3, pixmap.x/3, pixmap.y/3)
                candidates = []
                for image in images:
                    region = fitz.Rect(image["bbox"])
                    overlap = max(0, min(box.x1, region.x1)-max(box.x0, region.x0))
                    vertical_overlap = max(0, min(box.y1, region.y1)-max(box.y0, region.y0))
                    if (image.get("xref") and 8 <= region.width <= min(330, width * .7)
                            and 6 <= region.height <= min(110, band.height) and band.contains(region)
                            and overlap >= box.width * .88 and .6 <= box.width/region.width <= 1.2):
                        if vertical_overlap < min(box.height, region.height) * .35:
                            continue
                        digest = image["digest"].hex()
                        if digest not in verified:
                            verified[digest] = _standalone_brand(page, image, terms, model)
                        if verified[digest]:
                            candidates.append(image)
                if candidates:
                    # The topmost, closely fitting image actually paints this
                    # recognized word; hidden/clipped background fragments do not.
                    image = max(candidates, key=lambda item: item["number"])
                    region = fitz.Rect(image["bbox"])
                    digest = image["digest"].hex()
                    key = tuple(round(v, 1) for v in region)
                    if key not in seen:
                        result.hits.append((region, digest))
                        seen.add(key)
                else:
                    # OCR word boxes can include line height far beyond the ink.
                    # Hold such scans for review instead of erasing a guessed box.
                    result.unsafe_hits.append(box)
    except Exception as exc:
        result.error = str(exc)
    return result
