"""Remove verified text paints without rewriting unrelated graphics instructions.

MuPDF redaction can normalize implicit graphics state in older Word-generated
PDFs. Parsing and editing only text paints retains their original line widths,
transparency, colours and images. Unsupported form text uses the existing
redaction fallback and must still pass independent output QA.
"""
from __future__ import annotations

import pymupdf as fitz

from .image_info import image_info as page_image_info
import pikepdf
import re


TEXT_SHOW = {"Tj", "TJ", "'", '"'}


def _object_references(value: str) -> list[int]:
    return [int(match) for match in re.findall(r"\b(\d+)\s+\d+\s+R\b", value)]


def _appearance_uses_image(document: fitz.Document, annotation_xref: int, image_xref: int) -> bool:
    """Follow only appearance dependencies, avoiding annotation /P page cycles."""
    _, appearance = document.xref_get_key(annotation_xref, "AP")
    pending, visited = _object_references(appearance), set()
    while pending:
        candidate = pending.pop()
        if candidate == image_xref:
            return True
        if candidate in visited:
            continue
        visited.add(candidate)
        pending.extend(_object_references(document.xref_object(candidate)))
    return False


def image_occurrences_are_authorized(
    document: fitz.Document, xref: int,
    authorized_regions: dict[int, list[fitz.Rect]],
) -> bool:
    """Global image edits are safe only when every painted use is authorized.

    PDF image objects can be shared by body figures, other pages, masks and
    annotation appearances. A matching digest in one corner does not grant
    permission to remove those other placements. Unsupported scope is declined.
    """
    occurrences = 0
    try:
        if document.xref_get_key(xref, "Subtype") != ("name", "/Image"):
            return False
        for index, page in enumerate(document):
            for image in page.get_images(full=True):
                if len(image) > 1 and image[1] == xref:
                    return False  # This object is another image's soft mask.
            for annotation in page.annots() or ():
                if _appearance_uses_image(document, annotation.xref, xref):
                    return False
            for image in page_image_info(page):
                if image.get("xref") != xref:
                    continue
                region = fitz.Rect(image["bbox"])
                if region.is_empty or not any(allowed.contains(region) for allowed in authorized_regions.get(index, [])):
                    return False
                occurrences += 1
    except Exception:
        return False
    return occurrences > 0


def make_image_transparent(
    document: fitz.Document, xref: int,
    authorized_regions: dict[int, list[fitz.Rect]] | None = None,
) -> bool:
    if not authorized_regions or not image_occurrences_are_authorized(document, xref, authorized_regions):
        return False
    _neutralize_image_object(document, xref)
    return True


def _neutralize_image_object(document: fitz.Document, xref: int) -> None:
    mask = document.get_new_xref()
    document.update_object(mask, "<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 >>")
    document.update_stream(mask, b"\x00")
    document.update_object(xref, f"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /SMask {mask} 0 R >>")
    document.update_stream(xref, b"\xff")


def image_background_pixmap(page: fitz.Page, xref: int, region: fitz.Rect, zoom: float) -> fitz.Pixmap:
    """Render underlying artwork in a private, disposable reference only.

    No document escapes this helper and nothing is saved. Production image
    edits must use make_image_transparent's complete placement authorization.
    Avoid rescanning all source pages for every QA crop, which is quadratic for
    long textbooks and adds no protection to an unsaved diagnostic reference.
    """
    with fitz.open(stream=page.parent.tobytes(), filetype="pdf") as reference:
        _neutralize_image_object(reference, xref)
        return reference[page.number].get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=region,
                                                colorspace=fitz.csRGB, alpha=False)


def remove_region_text(page: fitz.Page, regions: list[fitz.Rect]) -> bool:
    if not page.get_contents():
        return True
    if not any(any(fitz.Rect(word[:4]).intersects(region) for region in regions)
               for word in page.get_text("words")):
        return True
    traces = page.get_texttrace()
    by_origin = []
    for span in traces:
        chars = span.get("chars", [])
        if chars and any(region.contains(fitz.Rect(span["bbox"])) for region in regions):
            by_origin.append((fitz.Point(chars[0][2]), fitz.Rect(span["bbox"])))
    with pikepdf.Pdf.new() as parser:
        instructions = pikepdf.parse_content_stream(parser.make_stream(page.read_contents()))
        ctm, stack, group, text_matrix, line_matrix = fitz.Matrix(1, 1), [], None, None, None
        groups = []
        for index, instruction in enumerate(instructions):
            if isinstance(instruction, pikepdf.ContentStreamInlineImage):
                continue
            operands, command = instruction
            command = str(command)
            if command == "q":
                stack.append(fitz.Matrix(ctm))
            elif command == "Q":
                ctm = stack.pop() if stack else fitz.Matrix(1, 1)
            elif command == "cm":
                ctm = fitz.Matrix(*map(float, operands)) * ctm
            elif command == "BT":
                group = {"shows": [], "safe": True, "clip": False}
                text_matrix = line_matrix = fitz.Matrix(1, 1)
            elif command == "ET":
                if group is not None:
                    groups.append(group)
                group = None
            elif group is not None:
                if command == "Tm":
                    text_matrix = line_matrix = fitz.Matrix(*map(float, operands))
                elif command in {"Td", "TD"} and line_matrix is not None:
                    line_matrix = fitz.Matrix(1, 0, 0, 1, *map(float, operands)) * line_matrix
                    text_matrix = fitz.Matrix(line_matrix)
                elif command == "T*":
                    text_matrix = None  # Decline unsupported implicit line advance.
                elif command == "Tr" and int(operands[0]) >= 4:
                    group["clip"] = True
                elif command in TEXT_SHOW:
                    group["shows"].append(index)
                    if text_matrix is None or command in {"'", '"'}:
                        group["safe"] = False
                    else:
                        origin = fitz.Point(0, 0) * text_matrix * ctm * page.transformation_matrix
                        candidates = [bounds for point, bounds in by_origin if abs(point.x-origin.x) < .8 and abs(point.y-origin.y) < .8]
                        if not candidates or not any(any(region.contains(bounds) for region in regions) for bounds in candidates):
                            group["safe"] = False
                    # Another paint must establish an explicit text position.
                    text_matrix = None
        remove = {index for group in groups if group["safe"] and not group["clip"] for index in group["shows"]}
        if remove:
            instructions = [instruction for index, instruction in enumerate(instructions) if index not in remove]
            xref = page.parent.get_new_xref()
            page.parent.update_object(xref, "<< >>")
            page.parent.update_stream(xref, pikepdf.unparse_content_stream(instructions))
            page.set_contents(xref)
    # A fallback is necessary for nested forms, partial glyph intersections or
    # unsupported implicit positioning. Geometry and visual QA still govern it.
    return not any(any(fitz.Rect(word[:4]).intersects(region) for region in regions)
                   for word in page.get_text("words"))
