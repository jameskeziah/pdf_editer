"""Content-aware image metadata reuse without repeated native bitmap decoding."""
from __future__ import annotations

import hashlib
import pymupdf as fitz


def _state(document, xref):
    return (document.xref_object(xref), hashlib.sha256(document.xref_stream_raw(xref) or b"").digest())


def image_info(page):
    document = page.parent
    if not document.is_pdf:
        return page.get_image_info(hashes=True)
    resources = page.get_images()
    cache = getattr(document, "_branding_image_digests", None)
    if cache is None:
        cache = document._branding_image_digests = {}
    states, digests = [], {}
    for resource in resources:
        xref = resource[0]
        state = _state(document, xref)
        states.append((xref, state))
        cached = cache.get(xref)
        if cached is None or cached[0] != state:
            cached = cache[xref] = (state, fitz.Pixmap(document, xref).digest)
        digests[cached[1]] = xref
        if resource[1]:
            states.append((resource[1], _state(document, resource[1])))
    states.extend((item[0], _state(document, item[0])) for item in page.get_xobjects())
    signature = (_state(document, page.xref), tuple(page.rect), tuple(page.transformation_matrix), page.rotation,
                 hashlib.sha256(page.read_contents()).digest(), states)
    page_cache = getattr(document, "_branding_page_images", None)
    if page_cache is None:
        page_cache = document._branding_page_images = {}
    previous = page_cache.get(page.xref)
    if previous is not None and previous[0] == signature:
        return [dict(item) for item in previous[1]]
    if getattr(page, "_branding_image_info_signature", None) != signature:
        if hasattr(page, "_image_info"):
            del page._image_info
        page._branding_image_info_signature = signature
    results = page.get_image_info(hashes=True)
    for item in results:
        item["xref"] = digests.get(item["digest"], 0)
    page_cache[page.xref] = (signature, [dict(item) for item in results])
    return results
