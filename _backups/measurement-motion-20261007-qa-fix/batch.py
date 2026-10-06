from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf as fitz

from .cache import BatchCache
from .config import load_profile
from .engine import analyze_pdf, process_pdf
from .metadata import build_meta, exclusion_reason
from .preview import render_plan_previews
from .serialization import to_jsonable


@dataclass(slots=True)
class BatchRecord:
    source: str
    output: str | None
    status: str
    class_name: str | None = None
    subject: str | None = None
    chapter: str | None = None
    material_type: str | None = None
    message: str = ""


def discover_pdfs(root: Path, output_root: Path) -> list[Path]:
    output_resolved = output_root.resolve()
    result: list[Path] = []
    for path in root.rglob("*.pdf"):
        try:
            if path.resolve().is_relative_to(output_resolved):
                continue
        except Exception:
            pass
        lower = path.name.lower()
        if lower.endswith((".tmp.pdf", ".qa_failed.pdf")):
            continue
        result.append(path)
    return sorted(result)


def _meta_quick(path: Path, root: Path):
    try:
        with fitz.open(path) as doc:
            return build_meta(path, root, doc)
    except Exception:
        return None


def _match_filters(meta, args) -> bool:
    if args.class_filter and meta.class_name != str(args.class_filter):
        return False
    if args.subject_filter and meta.subject.lower() != args.subject_filter.lower():
        return False
    if args.chapter_filter and args.chapter_filter.lower() not in meta.chapter.lower():
        return False
    if args.material_filter and args.material_filter.lower() not in meta.material_type.lower():
        return False
    return True


def _write_report(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(to_jsonable(data), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _worker_process(payload: tuple[str, str, str, str, str, str]) -> dict:
    source_s, output_s, root_s, profile_s, report_root_s, rel_s = payload
    source, output, root = Path(source_s), Path(output_s), Path(root_s)
    brand = load_profile(profile_s)
    result = process_pdf(source, output, root, brand, Path(report_root_s), Path(rel_s))
    return {
        "source": source_s,
        "output": str(result.output) if result.output else None,
        "status": result.status,
        "message": result.message,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PDF Branding V3.2 — analyze → PagePlan → render → QA")
    p.add_argument("input_root", type=Path)
    p.add_argument("output_root", type=Path)
    default_profile = Path(__file__).resolve().parent.parent / "profiles" / "sskem.json"
    p.add_argument("--profile", type=Path, default=default_profile)
    p.add_argument("--dry-run", action="store_true", help="Discover/classify only")
    p.add_argument("--analyze-only", action="store_true", help="Build plans without modifying PDFs")
    p.add_argument("--preview-plans", action="store_true", help="Render annotated plan PNGs during analysis")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--show-skipped", action="store_true")
    p.add_argument("--class-filter", choices=["6", "7", "8", "9", "10"])
    p.add_argument("--subject-filter", choices=["physics", "mathematics", "chemistry", "biology"])
    p.add_argument("--chapter-filter")
    p.add_argument("--material-filter")
    p.add_argument("--limit", type=int, default=0, help="Process at most N eligible PDFs (0 = all)")
    p.add_argument("--workers", type=int, default=1, help="Parallel PDF workers; use 1 while validating layouts")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_root = args.input_root.resolve()
    output_root = args.output_root.resolve()
    brand = load_profile(args.profile)
    output_root.mkdir(parents=True, exist_ok=True)
    report_root = output_root / "_reports"
    preview_root = output_root / "_plan_previews"
    cache = BatchCache(output_root)
    profile_hash = cache.profile_hash(brand.raw)

    found = discover_pdfs(input_root, output_root)
    eligible: list[tuple[Path, object]] = []
    skipped: list[BatchRecord] = []
    counts = {str(c): {s: 0 for s in ("mathematics", "physics", "chemistry", "biology")} for c in range(6, 11)}

    for path in found:
        reason = exclusion_reason(path, input_root)
        if reason:
            skipped.append(BatchRecord(str(path), None, "excluded", message=reason))
            continue
        meta = _meta_quick(path, input_root)
        if meta is None:
            skipped.append(BatchRecord(str(path), None, "unclassified", message="class/subject could not be inferred"))
            continue
        if not _match_filters(meta, args):
            continue
        eligible.append((path, meta))
        counts[meta.class_name][meta.subject] += 1

    if args.limit > 0:
        eligible = eligible[:args.limit]

    print(f"Found PDFs: {len(found)}")
    print(f"Eligible after filters: {len(eligible)}")
    print(f"Excluded/unclassified: {len(skipped)}")
    print(f"Profile: {brand.id} ({brand.source_path})")
    print(f"Workers: {max(1, args.workers)}")
    for cls in counts:
        c = counts[cls]
        print(f"Class {cls}: Mathematics {c['mathematics']} | Physics {c['physics']} | Chemistry {c['chemistry']} | Biology {c['biology']}")
    print()

    if args.show_skipped:
        for rec in skipped:
            print(f"SKIP  {rec.status:12} {rec.source} — {rec.message}")

    records: list[BatchRecord] = list(skipped)
    if args.dry_run:
        for i, (path, meta) in enumerate(eligible, 1):
            print(f"DRY   [{i}/{len(eligible)}] Class {meta.class_name} | {meta.subject.title()} | {meta.material_type} | {path.relative_to(input_root)}")
        _write_report(output_root / "batch_report.json", [asdict(r) for r in records])
        return 0

    if args.analyze_only:
        failures = 0
        for i, (source, meta) in enumerate(eligible, 1):
            rel = source.relative_to(input_root)
            print(f"PLAN  [{i}/{len(eligible)}] {rel}")
            try:
                plan = analyze_pdf(source, input_root, brand)
                stem = report_root / rel.parent / rel.stem
                _write_report(stem.with_suffix(".plan.json"), plan)
                if args.preview_plans:
                    render_plan_previews(source, plan, preview_root / rel.parent / rel.stem)
                records.append(BatchRecord(str(source), None, "analyzed", meta.class_name, meta.subject, meta.chapter, meta.material_type))
            except Exception as exc:
                failures += 1
                records.append(BatchRecord(str(source), None, "failed", meta.class_name, meta.subject, meta.chapter, meta.material_type, str(exc)))
                print(f"      ERROR — {exc}")
        _write_report(output_root / "batch_report.json", [asdict(r) for r in records])
        print(f"\nPlan reports: {report_root}")
        if args.preview_plans:
            print(f"Plan previews: {preview_root}")
        return 2 if failures else 0

    jobs: list[tuple[Path, object, Path, Path]] = []
    for source, meta in eligible:
        rel = source.relative_to(input_root)
        output = output_root / rel
        if not args.overwrite and not args.no_cache and cache.is_fresh(rel, source, output, profile_hash):
            print(f"CACHE {rel}")
            records.append(BatchRecord(str(source), str(output), "cached", meta.class_name, meta.subject, meta.chapter, meta.material_type))
            continue
        jobs.append((source, meta, rel, output))

    workers = max(1, int(args.workers))
    # Avoid accidental overload on a validation workstation.
    workers = min(workers, max(1, min(8, os.cpu_count() or 1)))

    if workers == 1:
        for i, (source, meta, rel, output) in enumerate(jobs, 1):
            print(f"RUN   [{i}/{len(jobs)}] {rel}")
            result = process_pdf(source, output, input_root, brand, report_root, rel)
            records.append(BatchRecord(str(source), str(result.output) if result.output else None, result.status, meta.class_name, meta.subject, meta.chapter, meta.material_type, result.message))
            if result.status == "completed" and result.output:
                cache.update(rel, source, result.output, profile_hash, True)
                cache.save()
            elif result.status == "qa_failed":
                print(f"      QA FAILED — inspect {result.output}")
            elif result.status == "failed":
                print(f"      ERROR — {result.message}")
    else:
        meta_by_source = {str(source): (meta, rel, output) for source, meta, rel, output in jobs}
        payloads = [
            (str(source), str(output), str(input_root), str(brand.source_path), str(report_root), str(rel))
            for source, meta, rel, output in jobs
        ]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker_process, payload): payload[0] for payload in payloads}
            done = 0
            for future in as_completed(futures):
                done += 1
                source_s = futures[future]
                meta, rel, output = meta_by_source[source_s]
                try:
                    raw = future.result()
                    status, output_s, message = raw["status"], raw["output"], raw["message"]
                except Exception as exc:
                    status, output_s, message = "failed", None, str(exc)
                print(f"DONE  [{done}/{len(jobs)}] {status:9} {rel}")
                records.append(BatchRecord(source_s, output_s, status, meta.class_name, meta.subject, meta.chapter, meta.material_type, message))
                if status == "completed" and output_s:
                    cache.update(rel, Path(source_s), Path(output_s), profile_hash, True)
                    cache.save()

    cache.save()
    _write_report(output_root / "batch_report.json", [asdict(r) for r in records])
    statuses = sorted({r.status for r in records})
    print("\nSummary:")
    for status in statuses:
        print(f"  {status}: {sum(1 for r in records if r.status == status)}")
    print(f"Reports: {report_root}")
    return 2 if any(r.status == "failed" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
