from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from .analysis import analyze_document
from .atomic import FileLock, atomic_write_json
from .config import BrandingProfile
from .metadata import build_meta, detect_document_profile
from .models import DocumentPlan, QAReport
from .planner import make_document_plan
from .qa import run_qa
from .renderer import render_document
from .publication import PublicationError, PublicationTransaction, recover_publication
from .serialization import to_jsonable


@dataclass(slots=True)
class ProcessResult:
    status: str
    source: Path
    output: Path | None
    plan: DocumentPlan | None
    qa: QAReport | None
    message: str = ""
    publication_committed: bool = False
    recovery_required: bool = False
    recovered_publication: bool = False


def analyze_pdf(source: Path, input_root: Path, brand: BrandingProfile) -> DocumentPlan:
    with fitz.open(source) as doc:
        meta = build_meta(source, input_root, doc)
        if meta is None:
            raise ValueError("Could not infer both class and subject")
        doc_profile = detect_document_profile(source, doc, meta)
        repeated, analyses = analyze_document(doc, meta, brand)
        return make_document_plan(source, meta, doc_profile, repeated, analyses, brand)


def _write_json_atomic(path: Path, data) -> None:
    atomic_write_json(path, to_jsonable(data))


def process_pdf(
    source: Path,
    output: Path,
    input_root: Path,
    brand: BrandingProfile,
    report_base: Path | None = None,
    relative_path: Path | None = None,
) -> ProcessResult:
    source, output, input_root = Path(source), Path(output), Path(input_root)
    failed_pdf = output.with_name(output.stem + ".qa_failed.pdf")
    out_doc: fitz.Document | None = None
    plan: DocumentPlan | None = None
    qa: QAReport | None = None
    recovered = ""
    transaction: PublicationTransaction | None = None
    try:
        if source.resolve() in (output.resolve(), failed_pdf.resolve()):
            raise ValueError("Source PDF and output PDF must be different files")
        reports = []
        if report_base is not None:
            rel = Path(relative_path) if relative_path is not None else Path(source.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError("Report relative_path must stay within the report directory")
            report_stem = Path(report_base) / rel.parent / rel.stem
            # Append extensions instead of replacing the last dotted part of a name.
            reports = [report_stem.with_name(report_stem.name + suffix) for suffix in (".plan.json", ".qa.json")]
        allowed = [output, failed_pdf, *reports]
        with FileLock(output):
            recovered = recover_publication(output, allowed)
            with PublicationTransaction(output, allowed) as transaction:
                with fitz.open(source) as src:
                    meta = build_meta(source, input_root, src)
                    if meta is None:
                        return ProcessResult("unclassified", source, None, None, None,
                                             "Could not infer class/subject", recovered_publication=bool(recovered))
                    doc_profile = detect_document_profile(source, src, meta)
                    repeated, analyses = analyze_document(src, meta, brand)
                    plan = make_document_plan(source, meta, doc_profile, repeated, analyses, brand)
                    tmp_pdf = transaction.stage_path(output)
                    out_doc = render_document(src, plan, brand)
                    out_doc.save(tmp_pdf, garbage=4, deflate=True, clean=False)
                    out_doc.close()
                    out_doc = None
                    # QA the private staged PDF before replacing any published file.
                    # Use a fresh reference document after rendering. MuPDF's
                    # decoded image/font state from analysis and cross-document
                    # page grafting can otherwise affect subsequent comparisons.
                    with fitz.open(source) as qa_source:
                        qa = run_qa(qa_source, tmp_pdf, plan, brand)
                    final_path = output if qa.passed else failed_pdf
                    transaction.retarget_stage(output, final_path)
                    qa.output = str(final_path)
                    if reports:
                        _write_json_atomic(transaction.stage_path(reports[0]), plan)
                        _write_json_atomic(transaction.stage_path(reports[1]), qa.to_dict())
                    transaction.commit()
                    message = "; ".join(part for part in (recovered, transaction.warning) if part)
                    if qa.passed:
                        return ProcessResult("completed", source, final_path, plan, qa, message,
                                             publication_committed=True, recovered_publication=bool(recovered))
                    message = "; ".join(part for part in (
                        "Output failed QA; existing production PDF was preserved", message,
                    ) if part)
                    return ProcessResult("qa_failed", source, final_path, plan, qa, message,
                                         publication_committed=True, recovered_publication=bool(recovered))
    except Exception as exc:
        return ProcessResult("failed", source, None, plan, qa,
                             f"{type(exc).__name__}: {exc}",
                             publication_committed=bool(transaction and transaction.committed),
                             recovery_required=isinstance(exc, PublicationError) and exc.recovery_required,
                             recovered_publication=bool(recovered))
    finally:
        if out_doc is not None:
            out_doc.close()
