from __future__ import annotations

import re
from pathlib import Path

import pymupdf as fitz

from .models import DocumentMeta, DocumentProfile, DocumentType

SUBJECT_ALIASES = {
    "physics": ("physics", "phy"),
    "mathematics": ("mathematics", "maths", "math"),
    "chemistry": ("chemistry", "chem"),
    "biology": ("biology", "bio"),
}

CLASS_PATTERNS = (
    re.compile(r"(?:class|std|standard|grade)[ _-]*(6|7|8|9|10)\b", re.I),
    re.compile(r"\b(6|7|8|9|10)(?:th|st|nd|rd)?\b", re.I),
)

SCIENCE_CHAPTER_SUBJECTS: dict[str, dict[str, str]] = {
    "6": {
        "components of food": "biology", "sorting materials into groups": "chemistry",
        "separation of substances": "chemistry", "getting to know plants": "biology",
        "body movements": "biology", "the living organisms characteristics and habitats": "biology",
        "measurement and motion": "physics", "light shadows and reflection": "physics",
        "electricity and circuits": "physics", "fun with magnets": "physics", "air around us": "chemistry",
    },
    "7": {
        "nutrition in plants": "biology", "nutrition in animals": "biology", "heat": "physics",
        "acids bases and salts": "chemistry", "physical and chemical changes": "chemistry",
        "respiration in organisms": "biology", "transportation in plants and animals": "biology",
        "reproduction in plants": "biology", "motion and time": "physics",
        "electric current and its effects": "physics", "light": "physics",
        "forests our lifeline": "biology", "waste water story": "biology",
    },
    "8": {
        "crop production and management": "biology", "microorganisms friend and foe": "biology",
        "coal and petroleum": "chemistry", "combustion and flame": "chemistry",
        "conservation of plants and animals": "biology", "reproduction in animals": "biology",
        "reaching the age of adolescence": "biology", "force and pressure": "physics",
        "friction": "physics", "sound": "physics", "chemical effects of electric current": "chemistry",
        "some natural phenomena": "physics", "light": "physics", "pressure winds storms and cyclones": "physics",
    },
    "9": {
        "matter in our surroundings": "chemistry", "is matter around us pure": "chemistry",
        "atoms and molecules": "chemistry", "structure of the atom": "chemistry",
        "the fundamental unit of life": "biology", "tissues": "biology", "motion": "physics",
        "gravitation": "physics", "thrust and pressure": "physics", "work and energy": "physics",
        "sound": "physics", "natural resources": "biology", "improvement in food resources": "biology",
    },
}

EXCLUDED_PATH_PARTS = {"excluded"}
ADMIN_FILENAME_PATTERNS = (
    re.compile(r"(?:^|[_\-\s])(plan|planner)(?:$|[_\-\s])", re.I),
    re.compile(r"(?:^|[_\-\s])schedule(?:$|[_\-\s])", re.I),
    re.compile(r"(?:^|[_\-\s])timetable(?:$|[_\-\s])", re.I),
    re.compile(r"(?:^|[_\-\s])calendar(?:$|[_\-\s])", re.I),
)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def candidate_parts(path: Path, root: Path) -> list[str]:
    try:
        rel_parts = list(path.relative_to(root).parts)
    except ValueError:
        rel_parts = []
    full_parts = list(path.parts)
    out, seen = [], set()
    for part in rel_parts + full_parts:
        if part not in seen:
            seen.add(part)
            out.append(part)
    return out


def infer_class(path: Path, root: Path) -> str | None:
    # Class information belongs to the document's nearby folder hierarchy.  Scan
    # from the file upward so unrelated ancestors (for example a temp directory
    # named ``pytest-9`` or a dated batch folder) cannot override ``6th class``.
    parts = list(reversed(path.parts))
    # Prefer explicit labels such as Class 6 / 6th class before accepting a bare
    # grade-like number.
    for pat in CLASS_PATTERNS:
        for part in parts:
            m = pat.search(norm(part))
            if m:
                return m.group(1)
    return None


def _strip_chapter_number(text: str) -> str:
    return re.sub(r"^\d+\s+", "", norm(text)).strip()


def _science_subject_from_chapter(path: Path, root: Path, class_name: str | None) -> str | None:
    if class_name not in SCIENCE_CHAPTER_SUBJECTS:
        return None
    raw = candidate_parts(path, root)
    parts = [norm(p) for p in raw]
    science_indexes = [i for i, p in enumerate(parts) if p == "science"]
    for idx in reversed(science_indexes):
        if idx + 1 >= len(raw):
            continue
        chapter = _strip_chapter_number(raw[idx + 1])
        table = SCIENCE_CHAPTER_SUBJECTS[class_name]
        if chapter in table:
            return table[chapter]
        for known, subject in table.items():
            if known in chapter or chapter in known:
                return subject
    return None


def infer_subject(path: Path, root: Path, class_name: str | None) -> str | None:
    for part in reversed(candidate_parts(path, root)):
        words = set(norm(Path(part).stem).split())
        for canonical, aliases in SUBJECT_ALIASES.items():
            if any(alias in words for alias in aliases):
                return canonical
    return _science_subject_from_chapter(path, root, class_name)


def infer_chapter(path: Path, root: Path) -> tuple[str, str | None]:
    parts = candidate_parts(path, root)
    subject_markers = {a for vals in SUBJECT_ALIASES.values() for a in vals} | {"science"}
    for i, part in enumerate(parts[:-1]):
        if norm(part) in subject_markers and i + 1 < len(parts):
            candidate = parts[i + 1]
            if norm(candidate) not in {"unsorted", "excluded"}:
                m = re.match(r"^\s*(\d+)\s+(.*)$", candidate)
                if m:
                    return m.group(2).strip(), m.group(1)
                return candidate.strip(), None
    parent = path.parent.name
    m = re.match(r"^\s*(\d+)\s+(.*)$", parent)
    return (m.group(2).strip(), m.group(1)) if m else (parent.strip(), None)


def infer_material_type(path: Path) -> str:
    n = f" {norm(path.stem)} "
    rules = [
        ("KEY POINTS", ("key point", "key points")),
        ("CHAPTER TEST", ("chapter test",)),
        ("PRACTICE TEST", ("practice test",)),
        ("PRACTICE SHEET", ("practice sheet", "practice practice sheet")),
        ("NCERT PRACTICE", ("ncert practice",)),
        ("NCERT SOLUTIONS", ("ncert solutions", "ncert solution", "ncert exercise solutions", "ncert questions solutions")),
        ("PRACTICE EXERCISE", ("practice exercise",)),
        ("EXERCISE", ("exercise solutions", "exercise solution", "exercise")),
        ("DPP", ("practice race", "race solutions", "race solution", " race ")),
        ("TEST", ("test solutions", "test solution", " test ")),
        ("NOTES", ("notes",)),
    ]
    for label, terms in rules:
        if any(t in n for t in terms):
            return label
    return "NOTES"


def detect_duration_marks(doc: fitz.Document) -> tuple[str | None, str | None]:
    if not doc:
        return None, None
    text = doc[0].get_text("text")
    dm = re.search(r"(?:Time|Estimate\s*Time)\s*[:\-]?\s*(\d+\s*(?:minutes?|mins?|hours?|hrs?))", text, re.I)
    mm = re.search(r"(?:Maximum\s*Marks|Max\.?\s*Marks|Marks)\s*[:\-]?\s*(\d+)", text, re.I)
    return (dm.group(1).strip() if dm else None, mm.group(1).strip() if mm else None)


def build_meta(path: Path, root: Path, doc: fitz.Document) -> DocumentMeta | None:
    cls = infer_class(path, root)
    subject = infer_subject(path, root, cls)
    if not cls or not subject:
        return None
    chapter, chapter_no = infer_chapter(path, root)
    duration, marks = detect_duration_marks(doc)
    return DocumentMeta(
        class_name=cls,
        subject=subject,
        chapter=chapter,
        chapter_number=chapter_no,
        material_type=infer_material_type(path),
        page_count=len(doc),
        duration=duration,
        max_marks=marks,
    )


def detect_document_profile(path: Path, doc: fitz.Document, meta: DocumentMeta) -> DocumentProfile:
    material = meta.material_type
    mapping = {
        "KEY POINTS": DocumentType.KEY_POINTS,
        "TEST": DocumentType.TEST,
        "PRACTICE TEST": DocumentType.TEST,
        "CHAPTER TEST": DocumentType.TEST,
        "DPP": DocumentType.DPP,
        "PRACTICE SHEET": DocumentType.PRACTICE_SHEET,
        "PRACTICE EXERCISE": DocumentType.EXERCISE,
        "EXERCISE": DocumentType.EXERCISE,
        "NCERT PRACTICE": DocumentType.NCERT,
        "NCERT SOLUTIONS": DocumentType.NCERT,
        "NOTES": DocumentType.NOTES,
    }
    kind = mapping.get(material, DocumentType.NORMAL)
    reasons = [f"material type normalized as {material}"]
    confidence = 0.78
    if len(doc):
        text = norm(doc[0].get_text("text"))
        if kind == DocumentType.TEST and "this test contains" in text:
            confidence = 0.96
            reasons.append("test instructions found on first page")
        elif kind == DocumentType.DPP and "dpp" in text:
            confidence = 0.94
            reasons.append("DPP text found on first page")
        elif kind == DocumentType.NCERT and "ncert" in text:
            confidence = 0.93
            reasons.append("NCERT heading found")
    return DocumentProfile(path=path, document_type=kind, confidence=confidence, reasons=reasons)


def exclusion_reason(path: Path, root: Path) -> str | None:
    parts = {norm(p) for p in candidate_parts(path, root)[:-1]}
    if parts & EXCLUDED_PATH_PARTS:
        return "inside excluded folder"
    stem = f" {norm(path.stem)} "
    if any(p.search(stem) for p in ADMIN_FILENAME_PATTERNS):
        return "administrative plan/schedule file"
    return None
