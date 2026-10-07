"""Real-library inventory and reproducible representative PDF regression runs.

Fixtures are manifest references to original PDFs, never large copied sources.
The matrix renders into a separate validation directory and records source hashes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable

import pymupdf as fitz

from .config import load_profile
from .engine import process_pdf
from . import metadata as metadata_module
from .metadata import build_meta, classification_evidence, detect_document_profile, exclusion_reason
from .preview import render_review_bundle
from .serialization import to_jsonable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(to_jsonable(data), indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def page_layout_flags(page: fitz.Page) -> dict:
    """Conservative observed layout flags, not content or semantic classifiers."""
    words = page.get_text("words")
    area = max(page.rect.width * page.rect.height, 1)
    image_rects = [fitz.Rect(item["bbox"]) for item in page.get_image_info()]
    image_coverage = min(1.0, sum((rect & page.rect).get_area() for rect in image_rects) / area)
    body_images = [r for r in image_rects if r.y1 > page.rect.height * .18
                   and r.y0 < page.rect.height * .88 and r.get_area() > area * .008]
    drawings = [] if image_coverage > .9 else page.get_drawings()
    hlines = vlines = shapes = 0
    for drawing in drawings:
        for item in drawing.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1:3]
                if abs(p1.y - p2.y) < 1 and abs(p1.x - p2.x) > 20:
                    hlines += 1
                if abs(p1.x - p2.x) < 1 and abs(p1.y - p2.y) > 12:
                    vlines += 1
            elif item[0] == "re":
                rect = item[1]
                hlines += 2
                vlines += 2
                if rect.get_area() > area * .002:
                    shapes += 1
            elif item[0] in {"c", "qu"}:
                shapes += 1
    flags = []
    if image_rects:
        flags.append("raster_images")
    if len(words) < 12 and image_coverage > .65:
        flags.append("image_only")
    if len(words) >= 450:
        flags.append("dense_text")
    if hlines >= 6 and vlines >= 6:
        flags.append("tables")
    if (body_images and image_coverage < .85) or shapes >= 10:
        flags.append("diagrams")
    if not words and not image_rects and not drawings:
        flags.append("blank")
    return {"page_number": page.number + 1, "flags": flags, "word_count": len(words),
            "image_coverage": round(image_coverage, 4), "image_count": len(image_rects),
            "drawing_count": len(drawings), "size_pt": [round(page.rect.width, 3), round(page.rect.height, 3)]}


def build_inventory(
    root: Path, manifest_path: Path | None = None,
    progress: Callable[[dict], None] | None = None, reuse: bool = True,
) -> dict:
    """Inventory every source PDF, including exclusions and unreadable documents.

    Cached entries are reused only when file size and mtime_ns match. Hashes and
    full page layout records are generated when an entry is new or changed.
    """
    root = Path(root).resolve()
    classifier_signature = hashlib.sha256(Path(metadata_module.__file__).read_bytes()).hexdigest()
    cached = {}
    if reuse and manifest_path and Path(manifest_path).is_file():
        previous = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if (previous.get("schema_version") == 1 and previous.get("source_root") == str(root)
                and previous.get("classifier_signature") == classifier_signature):
            cached = {entry["source_id"]: entry for entry in previous.get("documents", [])}
    paths = sorted(p for p in root.rglob("*.pdf") if not p.name.lower().endswith((".qa_failed.pdf", ".tmp.pdf")))
    entries = []
    for number, path in enumerate(paths, 1):
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        old = cached.get(relative)
        if old and old.get("size_bytes") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns:
            entry = old
        else:
            entry = {"source_id": relative,
                     "document_id": hashlib.sha256(relative.casefold().encode()).hexdigest()[:20],
                     "sha256": _sha256(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            reason = exclusion_reason(path, root)
            try:
                with fitz.open(path) as document:
                    meta = build_meta(path, root, document)
                    entry["page_count"] = len(document)
                    entry["classification"] = to_jsonable(meta) if meta else None
                    entry["classification_evidence"] = classification_evidence(path, root, document)
                    entry["status"] = "excluded" if reason else ("classified" if meta else "unclassified")
                    entry["exclusion_reason"] = reason
                    entry["family"] = detect_document_profile(path, document, meta).document_type.value if meta else "unknown"
                    entry["pages"] = [page_layout_flags(page) for page in document]
                    flags = {flag for page in entry["pages"] for flag in page["flags"]}
                    if meta and meta.material_type == "KEY POINTS":
                        flags.add("designed")
                    entry["layout_flags"] = sorted(flags)
            except Exception as exc:
                entry.update(status="unreadable", error=str(exc), classification=None,
                             family="unknown", page_count=0, pages=[], layout_flags=[])
        entries.append(entry)
        if progress:
            progress({"type": "inventory", "source": str(path), "index": number,
                      "total": len(paths), "status": entry["status"]})
    summary = Counter(entry["status"] for entry in entries)
    class_subject = Counter(f'{entry["classification"]["class_name"]}/{entry["classification"]["subject"]}'
                            for entry in entries if entry.get("classification") and entry["status"] == "classified")
    manifest = {"schema_version": 1, "source_root": str(root),
                "classifier_signature": classifier_signature,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "layout_flag_method": "Observed text/image/vector geometry; diagram/table flags are review candidates",
                "summary": {"documents": len(entries), "statuses": dict(summary),
                            "class_subject": dict(sorted(class_subject.items())),
                            "families": dict(Counter(e["family"] for e in entries if e["status"] == "classified")),
                            "layout_flags": dict(Counter(f for e in entries for f in e["layout_flags"]))},
                "documents": entries}
    manifest["representatives"] = select_representatives(manifest)
    observed_flags = set(manifest["summary"]["layout_flags"])
    manifest["coverage_notes"] = {
        "source_layouts_not_observed": sorted({"image_only", "dense_text", "tables", "diagrams", "designed"} - observed_flags),
        "note": "Absent real-source layouts are covered by synthetic tests; they are not claimed as real-PDF validation",
    }
    if manifest_path:
        _write_json(Path(manifest_path), manifest)
        write_inventory_report(manifest, Path(manifest_path).with_suffix(".md"))
    return manifest


def _coverage(entry: dict) -> set[str]:
    meta = entry.get("classification") or {}
    keys = {f'class:{meta.get("class_name")}', f'subject:{meta.get("subject")}',
            f'family:{entry["family"]}', f'material:{meta.get("material_type")}',
            f'class_subject:{meta.get("class_name")}/{meta.get("subject")}'}
    keys.update(f"layout:{flag}" for flag in entry.get("layout_flags", []))
    return keys


def select_representatives(manifest: dict, max_documents: int = 0) -> list[dict]:
    """Deterministic greedy coverage of classes, subjects, families and layouts."""
    candidates = [entry for entry in manifest["documents"] if entry["status"] == "classified"]
    uncovered = set().union(*(_coverage(entry) for entry in candidates)) if candidates else set()
    selected = []
    while uncovered and candidates and (not max_documents or len(selected) < max_documents):
        entry = min(candidates, key=lambda item: (-len(_coverage(item) & uncovered), item["page_count"], item["source_id"]))
        covered = _coverage(entry) & uncovered
        selected.append({"source_id": entry["source_id"], "sha256": entry["sha256"],
                         "covers": sorted(covered), "family": entry["family"],
                         "page_count": entry["page_count"], "layout_flags": entry["layout_flags"]})
        uncovered -= covered
        candidates.remove(entry)
    return selected


def write_inventory_report(manifest: dict, path: Path) -> None:
    summary = manifest["summary"]
    lines = ["# PDF library inventory", "", f"Source PDFs: {summary['documents']}",
             f"Statuses: {summary['statuses']}", "", "## Class / subject", "",
             "| Class / subject | PDFs |", "| --- | ---: |"]
    lines += [f"| {name} | {count} |" for name, count in summary["class_subject"].items()]
    lines += ["", "## Representative source references", "", "Original PDFs remain in their library folders. SHA-256 identifies each fixture version.", "",
              "| Source ID | Pages | Coverage |", "| --- | ---: | --- |"]
    lines += [f"| {row['source_id']} | {row['page_count']} | {', '.join(row['covers'])} |" for row in manifest["representatives"]]
    problems = [e for e in manifest["documents"] if e["status"] in {"unclassified", "unreadable"}]
    lines += ["", "## Unresolved documents", ""]
    lines += [f"- {e['source_id']}: {e.get('error', 'class/subject/chapter unresolved')}" for e in problems] or ["None."]
    lines += ["", "## Layout coverage limits", "",
              "Observed layout flags are geometry-based review candidates, not semantic labels.",
              f"Real-source layouts not observed: {', '.join(manifest.get('coverage_notes', {}).get('source_layouts_not_observed', [])) or 'none'}."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_matrix(
    root: Path, output_root: Path, brand, manifest: dict,
    selected: list[dict] | None = None, contact_sheets: bool = True,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Render referenced fixtures separately and verify source bytes afterwards."""
    root, output_root = Path(root).resolve(), Path(output_root).resolve()
    if output_root == root or output_root.is_relative_to(root) or root.is_relative_to(output_root):
        raise ValueError("Matrix output must be a separate directory outside the source library")
    selected = selected if selected is not None else manifest["representatives"]
    records = []
    for index, fixture in enumerate(selected, 1):
        relative = Path(fixture["source_id"])
        source = (root / relative).resolve()
        if not source.is_relative_to(root):
            raise ValueError("Fixture path escapes the source library")
        expected = fixture["sha256"]
        before = _sha256(source)
        if before != expected:
            record = {"source_id": fixture["source_id"], "status": "source_changed",
                      "message": "Source hash differs from the inventory fixture", "expected_sha256": expected,
                      "actual_sha256": before}
        else:
            output = output_root / "pdfs" / relative
            result = process_pdf(source, output, root, brand, output_root / "reports", relative)
            record = {"source_id": fixture["source_id"], "sha256": before,
                      "status": result.status, "output": str(result.output) if result.output else None,
                      "message": result.message, "covers": fixture.get("covers", []),
                      "qa": result.qa.to_dict() if result.qa else None,
                      "source_preserved": _sha256(source) == before}
            if not record["source_preserved"]:
                record["status"] = "source_modified"
            if contact_sheets and result.plan:
                review_dir = output_root / "review" / relative.parent / relative.stem
                try:
                    record["review"] = render_review_bundle(source, result.plan, review_dir, result.output, result.qa)
                except Exception as exc:
                    record["review_error"] = str(exc)
        records.append(record)
        if progress:
            progress({"type": "matrix", "source": str(source), "index": index,
                      "total": len(selected), "status": record["status"], "message": record.get("message", "")})
    counts = Counter(record["status"] for record in records)
    report = {"schema_version": 1, "source_root": str(root), "output_root": str(output_root),
              "profile": brand.id, "summary": dict(counts),
              "passed": bool(records) and all(r["status"] == "completed" and not r.get("review_error") for r in records),
              "records": records}
    _write_json(output_root / "matrix.json", report)
    lines = ["# Real PDF regression matrix", "", f"Profile: {brand.id}",
             f"Overall: {'passed' if report['passed'] else 'needs review'}", f"Results: {dict(counts)}", "",
             "| Source | Result | Source unchanged | Review |", "| --- | --- | --- | --- |"]
    for row in records:
        review = row.get("review", {}).get("index_html")
        link = Path(review).relative_to(output_root).as_posix() if review else None
        lines.append(f"| {row['source_id']} | {row['status']} | {row.get('source_preserved', 'not rendered')} | {'[open](<' + link + '>)' if link else '-'} |")
    (output_root / "matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Inventory and real-PDF representative regression matrix")
    parser.add_argument("mode", choices=["inventory", "matrix"])
    parser.add_argument("input_root", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parent.parent / "profiles" / "sskem.json")
    parser.add_argument("--max-documents", type=int, default=0)
    parser.add_argument("--chapter-filter")
    parser.add_argument("--no-contact-sheets", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    args.output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest or args.output_root / "inventory.json"
    def progress(event):
        if event["type"] != "inventory" or event["index"] % 25 == 0 or event["index"] == event["total"]:
            print(f"{event['type']} [{event['index']}/{event['total']}] {event['status']} {event['source']}", flush=True)
    manifest = build_inventory(args.input_root, manifest_path, progress, reuse=not args.refresh)
    if args.mode == "inventory":
        print(json.dumps(manifest["summary"], indent=2))
        return 2 if any(e["status"] in {"unclassified", "unreadable"} for e in manifest["documents"]) else 0
    if args.chapter_filter:
        selected = [{"source_id": e["source_id"], "sha256": e["sha256"], "covers": sorted(_coverage(e))}
                    for e in manifest["documents"] if e["status"] == "classified"
                    and args.chapter_filter.casefold() in e["classification"]["chapter"].casefold()]
        if args.max_documents:
            selected = selected[:args.max_documents]
    else:
        selected = select_representatives(manifest, args.max_documents)
    report = run_matrix(args.input_root, args.output_root, load_profile(args.profile), manifest,
                        selected, not args.no_contact_sheets, progress)
    print(f"Matrix reports: {args.output_root.resolve()}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
