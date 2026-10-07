from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import time
import warnings
import sys

from .cache import BatchCache
from .config import load_profile
from .engine import analyze_pdf, process_pdf
from .journal import JobJournal, RunLock, write_json
from .metadata import exclusion_reason, quick_meta
from .preview import render_plan_previews, render_review_bundle
from .serialization import to_jsonable

_source_hash = BatchCache.sha256

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
    attempts: int = 0
    elapsed_seconds: float = 0.0
    review_index: str | None = None
    qa_issues: list | None = None


def discover_pdfs(root: Path, output_root: Path) -> list[Path]:
    output_resolved = output_root.resolve()
    return sorted(path for path in root.rglob("*.pdf")
                  if not path.resolve().is_relative_to(output_resolved)
                  and not path.name.lower().endswith((".tmp.pdf", ".qa_failed.pdf")))


def _meta_quick(path: Path, root: Path):
    try:
        return quick_meta(path, root)
    except Exception:
        return None


def _match_filters(meta, args) -> bool:
    return not (
        (args.class_filter and meta.class_name != args.class_filter)
        or (args.subject_filter and meta.subject.lower() != args.subject_filter.lower())
        or (args.chapter_filter and args.chapter_filter.lower() not in meta.chapter.lower())
        or (args.material_filter and args.material_filter.lower() not in meta.material_type.lower())
    )


def _write_report(path: Path, data) -> None:
    write_json(path, to_jsonable(data))


def _process_one(source, output, root, brand, report_root, relative, review_root=None):
    started = time.perf_counter()
    result = process_pdf(source, output, root, brand, report_root, relative)
    raw = {"source": str(source), "output": str(result.output) if result.output else None,
           "status": result.status, "message": result.message,
           "elapsed_seconds": round(time.perf_counter() - started, 3)}
    qa, plan = getattr(result, "qa", None), getattr(result, "plan", None)
    if qa:
        raw.update(qa_issues=qa.to_dict()["issues"], qa_metrics=qa.metrics)
    if review_root and plan:
        try:
            bundle = render_review_bundle(source, plan, review_root / relative.parent / relative.stem,
                                          final=result.output, qa=qa)
            raw["review_index"] = bundle["index_html"]
        except Exception as exc:
            raw["review_error"] = str(exc)
            raw["message"] += f" Review generation failed: {exc}"
    return raw


def _worker_process(payload: tuple) -> dict:
    source, output, root, profile, report_root, relative, review_root = payload
    return _process_one(Path(source), Path(output), Path(root), load_profile(profile),
                        Path(report_root), Path(relative), Path(review_root) if review_root else None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PDF Branding: analyze, review, render, QA and resumable batches")
    p.add_argument("input_root", type=Path)
    p.add_argument("output_root", type=Path)
    p.add_argument("--profile", type=Path, default=Path(__file__).resolve().parent.parent / "profiles/sskem.json")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Discover/classify only")
    mode.add_argument("--analyze-only", action="store_true", help="Build plans without rendering PDFs")
    p.add_argument("--preview-plans", action="store_true")
    p.add_argument("--contact-sheets", action="store_true", help="Source/plan/output contact sheets and HTML review")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--resume", action="store_true", help="Resume checkpoints; verified completed outputs are skipped")
    p.add_argument("--retry-failed", action="store_true", help="Select failed, QA-failed, interrupted or cancelled jobs")
    p.add_argument("--retries", type=int, choices=range(0, 6), default=0, help="Retry processing errors; QA failures require review")
    p.add_argument("--cancel-file", type=Path, help="Stop scheduling after this file appears; active documents finish safely")
    p.add_argument("--show-skipped", action="store_true")
    p.add_argument("--class-filter", choices=["6", "7", "8", "9", "10"])
    p.add_argument("--subject-filter", choices=["physics", "mathematics", "chemistry", "biology"])
    p.add_argument("--chapter-filter")
    p.add_argument("--material-filter")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--selection-manifest", type=Path, help="Render root-relative fixtures from an inventory or fixture JSON")
    p.add_argument("--workers", type=int, choices=range(1, 9), default=1)
    return p


def main(argv: list[str] | None = None, *, event_callback=None, cancel_event=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(argv)
    input_root, output_root = args.input_root.resolve(), args.output_root.resolve()
    if input_root == output_root:
        raise ValueError("Input and output folders must be different")
    if not input_root.is_dir():
        raise ValueError(f"Input folder does not exist: {input_root}")
    if input_root.is_relative_to(output_root):
        raise ValueError("Output folder must not contain the input folder")
    output_root.mkdir(parents=True, exist_ok=True)
    brand = load_profile(args.profile)
    cache = BatchCache(output_root)
    fingerprint = cache.profile_hash(brand)
    workers = min(args.workers, max(1, os.cpu_count() or 1))
    records = {}

    def emit(event):
        if event_callback:
            try:
                event_callback(event)
            except Exception as exc:
                warnings.warn(f"Progress callback failed: {exc}", RuntimeWarning, stacklevel=2)

    def cancelled():
        return bool((cancel_event is not None and cancel_event.is_set())
                    or (args.cancel_file and args.cancel_file.exists()))

    with RunLock(output_root):
        options = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
        journal = JobJournal(output_root, input_root, fingerprint, options)

        def checkpoint(record, *, attempt=False, index=0, total=0):
            saved = journal.record(record, increment_attempt=attempt)
            records[saved["source"]] = saved
            _write_report(output_root / "batch_report.json", list(records.values()))
            emit({"type": "job", **saved, "index": index, "total": total})
            return saved

        try:
            found, eligible = discover_pdfs(input_root, output_root), []
            if args.selection_manifest:
                import json
                selection = json.loads(args.selection_manifest.read_text(encoding="utf-8"))
                if isinstance(selection, list):
                    fixtures = selection
                elif isinstance(selection, dict):
                    fixtures = selection.get("representatives", selection.get("fixtures", selection.get("sources", [])))
                else:
                    raise ValueError("Selection manifest must contain a fixture list")
                if not isinstance(fixtures, list) or not fixtures:
                    raise ValueError("Selection manifest has no fixtures")
                selected = set()
                for fixture in fixtures:
                    source_id = fixture.get("source_id") if isinstance(fixture, dict) else fixture
                    if not isinstance(source_id, str) or not source_id:
                        raise ValueError("Selected fixture must have a source_id")
                    path = (input_root / source_id).resolve()
                    if not path.is_relative_to(input_root):
                        raise ValueError("Selected fixture escapes the source library")
                    if not path.is_file() or path.suffix.lower() != ".pdf":
                        raise ValueError(f"Selected PDF does not exist: {source_id}")
                    if isinstance(fixture, dict) and fixture.get("sha256") and _source_hash(path) != fixture["sha256"]:
                        raise ValueError(f"Selected fixture hash changed: {source_id}")
                    selected.add(path)
                found = [path for path in found if path.resolve() in selected]
            emit({"type": "discovery", "total": len(found), "message": "Classifying source PDFs"})
            for source in found:
                reason = exclusion_reason(source, input_root)
                if reason:
                    checkpoint(asdict(BatchRecord(str(source), None, "excluded", message=reason)))
                    continue
                meta = _meta_quick(source, input_root)
                if meta is None:
                    checkpoint(asdict(BatchRecord(str(source), None, "unclassified", message="class/subject could not be inferred")))
                    continue
                if not _match_filters(meta, args):
                    continue
                previous = journal.get(source)
                if args.retry_failed and (not previous or previous["status"] not in {"failed", "qa_failed", "cancelled", "interrupted"}):
                    continue
                eligible.append((source, meta))
            if args.limit > 0:
                eligible = eligible[:args.limit]
            counts = {str(c): {s: 0 for s in ("mathematics", "physics", "chemistry", "biology")} for c in range(6, 11)}
            for _, meta in eligible:
                counts[meta.class_name][meta.subject] += 1
            print(f"Found PDFs: {len(found)}\nEligible after filters: {len(eligible)}\nExcluded/unclassified: {len(records)}")
            print(f"Profile: {brand.id} ({brand.source_path})\nWorkers: {workers}")
            for cls, subjects in counts.items():
                print(f"Class {cls}: " + " | ".join(f"{s.title()} {n}" for s, n in subjects.items()))
            if args.show_skipped:
                for record in records.values():
                    print(f"SKIP {record['status']}: {record['source']} - {record['message']}")
            report_root, review_root = output_root / "_reports", output_root / "_reviews"
            jobs, total = [], len(eligible)
            emit({"type": "planned", "total": total, "message": f"{total} eligible PDFs"})
            for index, (source, meta) in enumerate(eligible, 1):
                relative, output = source.relative_to(input_root), output_root / source.relative_to(input_root)
                base = asdict(BatchRecord(str(source), str(output), "pending", meta.class_name, meta.subject, meta.chapter, meta.material_type))
                base["profile_fingerprint"] = fingerprint
                try:
                    base["source_sha256"] = _source_hash(source)
                except OSError as exc:
                    base.update(status="failed", output=None, message=f"Cannot fingerprint source: {exc}")
                    checkpoint(base, index=index, total=total)
                    continue
                if args.dry_run:
                    base.update(status="discovered", output=None)
                    checkpoint(base, index=index, total=total)
                    print(f"DRY [{index}/{total}] {relative}")
                    continue
                if not args.analyze_only and not args.no_cache and (args.resume or not args.overwrite) and cache.is_fresh(relative, source, output, fingerprint):
                    base["status"] = "cached"
                    previous = journal.get(source) or {}
                    for field in ("review_index", "qa_issues", "qa_metrics"):
                        if previous.get(field) is not None:
                            base[field] = previous[field]
                    checkpoint(base, index=index, total=total)
                    print(f"CACHE {relative}")
                    continue
                checkpoint(base, total=total)
                jobs.append((source, relative, output, base))
            completed = 0

            def start_job(job):
                saved = checkpoint({**job[3], "status": "running", "message": "Processing"}, attempt=True, index=completed, total=len(jobs))
                job[3]["attempts"] = saved["attempts"]
                print(f"RUN [{completed + 1}/{len(jobs)}] {job[1]}")

            def finish_job(job, raw):
                nonlocal completed
                completed += 1
                record = checkpoint({**job[3], **raw}, index=completed, total=len(jobs))
                if record["status"] == "completed" and record.get("output"):
                    cache.update(job[1], job[0], Path(record["output"]), fingerprint, True)
                    cache.save()
                for issue in record.get("qa_issues") or []:
                    print(f"  QA {issue['code']} page {issue.get('page_number')}: {issue['message']}")
                if record["status"] == "failed":
                    print(f"  ERROR: {record['message']}")

            def payload(job):
                return (str(job[0]), str(job[2]), str(input_root), str(brand.source_path),
                        str(report_root), str(job[1]), str(review_root) if args.contact_sheets else None)

            if args.analyze_only:
                for job in jobs:
                    if cancelled():
                        break
                    start_job(job)
                    try:
                        plan = analyze_pdf(job[0], input_root, brand)
                        stem = report_root / job[1].parent / job[1].stem
                        _write_report(stem.with_name(stem.name + ".plan.json"), plan)
                        raw = {"status": "analyzed", "output": None, "message": ""}
                        if args.preview_plans:
                            render_plan_previews(job[0], plan, output_root / "_plan_previews" / job[1].parent / job[1].stem)
                        if args.contact_sheets:
                            bundle = render_review_bundle(job[0], plan, review_root / job[1].parent / job[1].stem)
                            raw["review_index"] = bundle["index_html"]
                    except Exception as exc:
                        raw = {"status": "failed", "output": None, "message": str(exc)}
                    finish_job(job, raw)
            elif workers == 1:
                for job in jobs:
                    if cancelled():
                        break
                    for attempt in range(args.retries + 1):
                        start_job(job)
                        try:
                            raw = _process_one(job[0], job[2], input_root, brand, report_root, job[1],
                                               review_root if args.contact_sheets else None)
                        except Exception as exc:
                            raw = {"status": "failed", "output": None, "message": str(exc)}
                        if raw["status"] != "failed" or attempt == args.retries or cancelled():
                            finish_job(job, raw)
                            break
                        checkpoint({**job[3], **raw, "message": f"Retry {attempt + 1}: {raw['message']}"}, total=len(jobs))
            else:
                pending, active, retries = list(jobs), {}, {}
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    while pending or active:
                        while pending and len(active) < workers and not cancelled():
                            job = pending.pop(0)
                            start_job(job)
                            active[pool.submit(_worker_process, payload(job))] = job
                        if not active:
                            break
                        done, _ = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                        for future in done:
                            job = active.pop(future)
                            try:
                                raw = future.result()
                            except Exception as exc:
                                raw = {"status": "failed", "output": None, "message": str(exc)}
                            key, attempts = str(job[0]), retries.get(str(job[0]), 0)
                            if raw["status"] == "failed" and attempts < args.retries and not cancelled():
                                retries[key] = attempts + 1
                                checkpoint({**job[3], **raw}, total=len(jobs))
                                pending.insert(0, job)
                            else:
                                finish_job(job, raw)
            if cancelled():
                for job in jobs:
                    if records[str(job[0])]["status"] == "pending":
                        checkpoint({**job[3], "status": "cancelled", "output": None, "message": "Not started; resume to continue"}, index=completed, total=len(jobs))
            cache.save()
            _write_report(output_root / "batch_report.json", list(records.values()))
            failed = any(r["status"] in {"failed", "qa_failed"} for r in records.values())
            status = "cancelled" if cancelled() else "failed" if failed else "completed"
            journal.finish(status)
            print("\nSummary:")
            for name in sorted({r["status"] for r in records.values()}):
                print(f"  {name}: {sum(r['status'] == name for r in records.values())}")
            print(f"Reports: {report_root}\nManifest: {output_root / 'batch_manifest.json'}")
            code = 130 if cancelled() else 2 if failed else 0
            emit({"type": "finished", "status": status, "exit_code": code, "total": total,
                  "message": f"Finished: {status}", "manifest": str(output_root / "batch_manifest.json")})
            return code
        except BaseException:
            journal.finish("interrupted")
            raise
        finally:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
