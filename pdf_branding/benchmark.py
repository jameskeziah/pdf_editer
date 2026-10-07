"""Measure real batch throughput, process-tree memory and parallel PDF integrity."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pymupdf as fitz

from .cache import BatchCache
from .journal import write_json


def render_signature(path: Path) -> dict:
    """Ignore nondeterministic PDF IDs; compare actual page dimensions and pixels."""
    pages = []
    with fitz.open(path) as document:
        for page in document:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1, 1), colorspace=fitz.csRGB, alpha=False)
            pages.append({"size_pt": list(page.rect), "pixels": [pixmap.width, pixmap.height],
                          "render_sha256": hashlib.sha256(pixmap.samples).hexdigest()})
    return {"page_count": len(pages), "pages": pages}


def sample_process_tree(pid: int) -> tuple[int, dict[int, float]]:
    try:
        parent = psutil.Process(pid)
        processes = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return 0, {}
    rss, cpu = 0, {}
    for process in processes:
        try:
            rss += process.memory_info().rss
            times = process.cpu_times()
            cpu[process.pid] = times.user + times.system
        except psutil.Error:
            pass
    return rss, cpu


def _run_batch(command: list[str], log: Path) -> dict:
    log.parent.mkdir(parents=True, exist_ok=True)
    peak, cpu = 0, {}
    started = time.perf_counter()
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   cwd=Path(__file__).resolve().parent.parent)
        while process.poll() is None:
            memory, sample = sample_process_tree(process.pid)
            peak = max(peak, memory)
            for pid, seconds in sample.items():
                cpu[pid] = max(cpu.get(pid, 0), seconds)
            time.sleep(.05)
        code = process.wait()
    return {"exit_code": code, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "peak_process_tree_rss_mb": round(peak / 1024 ** 2, 2),
            "sampled_cpu_seconds": round(sum(cpu.values()), 3), "log": str(log.resolve())}


def compare_runs(baseline: dict, candidate: dict) -> list[dict]:
    issues = []
    if baseline.keys() != candidate.keys():
        issues.append({"code": "OUTPUT_SET_CHANGED", "message": "Worker counts produced different source sets"})
    for source_id in sorted(baseline.keys() & candidate.keys()):
        left, right = baseline[source_id], candidate[source_id]
        if left["status"] != right["status"]:
            issues.append({"code": "STATUS_CHANGED", "source_id": source_id})
        if left.get("signature") != right.get("signature"):
            issues.append({"code": "RENDER_CHANGED", "source_id": source_id})
    return issues


def choose_workers(runs: list[dict], available_memory_mb: float) -> int:
    """Use measured successful runs; reserve at least half of available memory."""
    successful = [run for run in runs if run["exit_code"] == 0 and not run.get("integrity_issues")
                  and run["peak_process_tree_rss_mb"] <= available_memory_mb * .5]
    if not successful:
        return 1
    fastest = min(run["elapsed_seconds"] for run in successful)
    # Prefer fewer workers when throughput differs by less than ten percent.
    return min(run["workers"] for run in successful if run["elapsed_seconds"] <= fastest * 1.1)


def run_benchmark(input_root: Path, output_root: Path, manifest_path: Path,
                  profile: Path, workers: list[int], progress=None) -> dict:
    input_root, output_root = input_root.resolve(), output_root.resolve()
    if not input_root.is_dir() or input_root.is_relative_to(output_root) or output_root.is_relative_to(input_root):
        raise ValueError("Benchmark output must be outside the source library")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixtures = manifest if isinstance(manifest, list) else manifest.get("representatives", manifest.get("sources", manifest.get("fixtures", [])))
    if not fixtures:
        raise ValueError("Benchmark needs a nonempty representative fixture manifest")
    output_root.mkdir(parents=True, exist_ok=True)
    selection = output_root / "selection.json"
    normalized, before = [], {}
    for fixture in fixtures:
        source_id = fixture if isinstance(fixture, str) else fixture["source_id"]
        source = (input_root / source_id).resolve()
        if not source.is_relative_to(input_root):
            raise ValueError("Benchmark fixture escapes source library")
        digest = BatchCache.sha256(source)
        if isinstance(fixture, dict) and fixture.get("sha256", digest) != digest:
            raise ValueError(f"Fixture hash changed: {source_id}")
        before[source_id] = digest
        normalized.append({"source_id": source_id, "sha256": digest})
    write_json(selection, normalized)
    available = psutil.virtual_memory().available / 1024 ** 2
    runs, baseline = [], None
    for count in sorted(set([1, *workers])):
        if count not in range(1, 9):
            raise ValueError("Worker counts must be between 1 and 8")
        destination = output_root / f"workers_{count}"
        if progress:
            progress({"type": "benchmark", "workers": count, "message": "Starting measured batch"})
        command = [sys.executable, "-u", "-m", "pdf_branding.batch", str(input_root), str(destination),
                   "--profile", str(profile.resolve()), "--selection-manifest", str(selection),
                   "--workers", str(count), "--overwrite", "--no-cache"]
        run = {"workers": count, **_run_batch(command, output_root / f"workers_{count}.log")}
        report_path = destination / "batch_report.json"
        records = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else []
        outputs, pages = {}, 0
        for record in records:
            source_id = Path(record["source"]).relative_to(input_root).as_posix()
            item = {"status": record["status"]}
            output = record.get("output")
            if output and Path(output).is_file():
                item["signature"] = render_signature(Path(output))
                item["output_sha256"] = BatchCache.sha256(Path(output))
                pages += item["signature"]["page_count"]
            outputs[source_id] = item
        run.update(outputs=outputs, statuses=dict(Counter(r["status"] for r in records)), page_count=pages,
                   pages_per_second=round(pages / max(run["elapsed_seconds"], .001), 3))
        if baseline is None:
            baseline = outputs
        run["integrity_issues"] = compare_runs(baseline, outputs)
        if set(outputs) != set(before):
            run["integrity_issues"].append({"code": "MISSING_FIXTURES"})
        run["source_preserved"] = all(BatchCache.sha256(input_root / key) == digest for key, digest in before.items())
        if not run["source_preserved"]:
            run["integrity_issues"].append({"code": "SOURCE_CHANGED"})
        runs.append(run)
        write_json(output_root / "benchmark.json", {"schema_version": 1, "runs": runs, "status": "running"})
        if progress:
            progress({"type": "benchmark", "workers": count, "elapsed_seconds": run["elapsed_seconds"],
                      "peak_rss_mb": run["peak_process_tree_rss_mb"], "exit_code": run["exit_code"],
                      "message": "Measured batch complete"})
    passed = all(run["exit_code"] == 0 and not run["integrity_issues"] for run in runs)
    result = {"schema_version": 1, "input_root": str(input_root), "status": "completed",
              "passed": passed, "available_memory_mb": round(available, 2),
              "recommended_workers": choose_workers(runs, available),
              "memory_method": "50ms samples of summed parent and worker RSS; peak may miss short allocations",
              "integrity_method": "Every output page size and RGB pixel SHA-256, plus unchanged source SHA-256",
              "runs": runs}
    write_json(output_root / "benchmark.json", result)
    lines = ["# PDF batch benchmark", "", f"Passed: {passed}", f"Recommended workers: {result['recommended_workers']}", "",
             "| Workers | Seconds | Pages/sec | Peak RSS MB | Exit | Pixel/source integrity |", "| ---: | ---: | ---: | ---: | ---: | --- |"]
    lines += [f"| {r['workers']} | {r['elapsed_seconds']} | {r['pages_per_second']} | {r['peak_process_tree_rss_mb']} | {r['exit_code']} | {'pass' if not r['integrity_issues'] else 'FAIL'} |" for r in runs]
    lines += ["", result["memory_method"], "", "QA failures remain failures and exclude a run from worker recommendations."]
    (output_root / "benchmark.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_root", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parent.parent / "profiles/sskem.json")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    args = parser.parse_args(argv)
    result = run_benchmark(args.input_root, args.output_root, args.manifest, args.profile, args.workers,
                           lambda event: print(json.dumps(event), flush=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
