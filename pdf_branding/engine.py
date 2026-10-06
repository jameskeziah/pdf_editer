from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from .analysis import analyze_document
from .config import BrandingProfile
from .metadata import build_meta, detect_document_profile
from .models import DocumentPlan, QAReport
from .planner import make_document_plan
from .qa import run_qa
from .renderer import render_document
from .serialization import to_jsonable


@dataclass(slots=True)
class ProcessResult:
    status: str
    source: Path
    output: Path | None
    plan: DocumentPlan | None
    qa: QAReport | None
    message: str = ""


def analyze_pdf(source: Path, input_root: Path, brand: BrandingProfile) -> DocumentPlan:
    with fitz.open(source) as doc:
        meta = build_meta(source, input_root, doc)
        if meta is None:
            raise ValueError("Could not infer both class and subject")
        doc_profile = detect_document_profile(source, doc, meta)
        repeated, analyses = analyze_document(doc, meta, brand)
        return make_document_plan(source, meta, doc_profile, repeated, analyses, brand)


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(to_jsonable(data), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def process_pdf(
    source: Path,
    output: Path,
    input_root: Path,
    brand: BrandingProfile,
    report_base: Path | None = None,
    relative_path: Path | None = None,
) -> ProcessResult:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_pdf = output.with_name(output.stem + ".tmp.pdf")
    failed_pdf = output.with_name(output.stem + ".qa_failed.pdf")
    for stale in (tmp_pdf, failed_pdf):
        if stale.exists():
            stale.unlink()

    out_doc: fitz.Document | None = None
    try:
        with fitz.open(source) as src:
            meta = build_meta(source, input_root, src)
            if meta is None:
                return ProcessResult("unclassified", source, None, None, None, "Could not infer class/subject")
            doc_profile = detect_document_profile(source, src, meta)
            repeated, analyses = analyze_document(src, meta, brand)
            plan = make_document_plan(source, meta, doc_profile, repeated, analyses, brand)

            out_doc = render_document(src, plan, brand)
            out_doc.save(tmp_pdf, garbage=4, deflate=True, clean=True)
            out_doc.close()
            out_doc = None

            # Reopen and QA before the final filename becomes visible.
            qa = run_qa(src, tmp_pdf, plan, brand)
            final_path = output if qa.passed else failed_pdf
            tmp_pdf.replace(final_path)
            qa.output = str(final_path)

            if report_base is not None:
                rel = relative_path or Path(source.name)
                report_stem = report_base / rel.parent / rel.stem
                _write_json_atomic(report_stem.with_suffix(".plan.json"), plan)
                _write_json_atomic(report_stem.with_suffix(".qa.json"), qa.to_dict())

            if qa.passed:
                return ProcessResult("completed", source, final_path, plan, qa)
            return ProcessResult("qa_failed", source, final_path, plan, qa, "Output failed QA; final production filename was not replaced")
    except Exception as exc:
        if tmp_pdf.exists():
            tmp_pdf.unlink(missing_ok=True)
        return ProcessResult("failed", source, None, None, None, str(exc))
    finally:
        if out_doc is not None:
            out_doc.close()
