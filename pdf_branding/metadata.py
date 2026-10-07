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
    re.compile(r"\b(6|7|8|9|10)(?:th|st|nd|rd)?[ _-]+(?:class|std|standard|grade)\b", re.I),
    re.compile(r"^(6|7|8|9|10)(?:th|st|nd|rd)?$", re.I),
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
    """Use the input hierarchy; unrelated dated/numbered ancestors are not metadata."""
    try:
        parts = list(path.resolve().relative_to(root.resolve()).parts)
        # A chapter or subject may itself be used as input_root. Recover only
        # the adjacent class/subject hierarchy, not arbitrary outer ancestors.
        markers = {alias for aliases in SUBJECT_ALIASES.values() for alias in aliases} | {"science"}
        contextual_class = None
        if any(pattern.search(norm(root.name)) for pattern in CLASS_PATTERNS[:2]):
            contextual_class = root
        elif norm(root.name) in markers:
            contextual_class = root.parent
        elif norm(root.parent.name) in markers:
            contextual_class = root.parent.parent
        if contextual_class and any(pattern.search(norm(contextual_class.name)) for pattern in CLASS_PATTERNS[:2]):
            return [contextual_class.name] + list(path.resolve().relative_to(contextual_class.resolve()).parts)
        return parts
    except ValueError:
        return list(path.parts)


def infer_class(path: Path, root: Path) -> str | None:
    # Explicit class labels outrank chapter numbers and filename numbers. Bare
    # grades are accepted only as complete folder names, never ``10 Sound``.
    parts = list(reversed(candidate_parts(path, root)[:-1]))
    for pat in CLASS_PATTERNS:
        for part in parts:
            match = pat.search(norm(part))
            if match:
                return match.group(1)
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
        ("KEY POINTS", ("key point", "key points", "keypoint", "keypoints")),
        ("CHAPTER TEST", ("chapter test",)),
        ("PRACTICE TEST", ("practice test",)),
        ("PRACTICE SHEET", ("practice sheet", "practice practice sheet")),
        ("NCERT PRACTICE", ("ncert practice",)),
        ("NCERT SOLUTIONS", ("ncert solutions", "ncert solution", "ncert exercise solutions", "ncert questions solutions", "ncert question solutions", "ncert question exercise solution")),
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


# These exceptional paths were reviewed against their actual first-page
# provenance/academic content. They remain in their original Unsorted folders.
# Required content anchors prevent a similarly named replacement being silently
# accepted. Chapter numbers follow the library hierarchy, not publisher modules.
CONTENT_METADATA = {
    "6th class/science/Unsorted/NCERT_Exercise_solutions_The_Living_Organisms_Characteristics_and.pdf":
        ("6", "biology", "The Living Organisms Characteristics and Habitats", "06", "EXERCISE", ("biology", "the living organisms")),
    "6th class/science/Unsorted/NCERT_Practice_solutions_The_Living_Organisms_Characteristics_and.pdf":
        ("6", "biology", "The Living Organisms Characteristics and Habitats", "06", "NCERT PRACTICE", ("biology", "characteristics and habitats")),
    "6th class/science/Unsorted/NCERT_Question_solutions_The_Living_Organisms_Characteristics_and.pdf":
        ("6", "biology", "The Living Organisms Characteristics and Habitats", "06", "NCERT SOLUTIONS", ("biology", "characteristics and habitats")),
    "9th class/science/Unsorted/original (1).pdf":
        ("9", "biology", "Tissues", "02", "PRACTICE SHEET", ("biology", "tissues", "practice sheet")),
    "8th class/math/Unsorted/NCERT_Practice_Exercise_Solution_Introduction_to_Graphs_and_Data.pdf":
        ("8", "mathematics", "Introduction to Graphs and Data Handling", "11", "NCERT SOLUTIONS", ("mathematics", "histogram", "tally marks")),
}


def _content_override(path: Path, root: Path, doc: fitz.Document) -> tuple | None:
    rel = "/".join(candidate_parts(path, root))
    record = CONTENT_METADATA.get(rel)
    if record is None or not len(doc):
        return None
    text = norm(doc[0].get_text("text"))
    if all(anchor in text for anchor in record[5]) and infer_class(path, root) == record[0]:
        return record
    return None


def classification_evidence(path: Path, root: Path, doc: fitz.Document | None = None) -> dict:
    """Record how classification was obtained for inventory and review reports."""
    rel = "/".join(candidate_parts(path, root))
    if rel in CONTENT_METADATA:
        record = _content_override(path, root, doc) if doc is not None else None
        return {"method": "reviewed_content_record", "validated": record is not None,
                "required_content_anchors": list(CONTENT_METADATA[rel][5]),
                "recorded_metadata": dict(zip(("class_name", "subject", "chapter", "chapter_number", "material_type"), CONTENT_METADATA[rel][:5])),
                "reason": "Reviewed publisher provenance and academic content; source path preserved"}
    cls = infer_class(path, root)
    return {"method": "folder_hierarchy", "validated": bool(cls and infer_subject(path, root, cls)),
            "reason": "Explicit class folder and subject/chapter hierarchy"}


def quick_meta(path: Path, root: Path) -> DocumentMeta | None:
    """Fast filter metadata; ambiguous/Unsorted PDFs need content validation."""
    if any(norm(part) == "unsorted" for part in candidate_parts(path, root)[:-1]):
        with fitz.open(path) as doc:
            return build_meta(path, root, doc)
    cls = infer_class(path, root)
    subject = infer_subject(path, root, cls)
    if not cls or not subject:
        with fitz.open(path) as doc:
            return build_meta(path, root, doc)
    chapter, number = infer_chapter(path, root)
    return DocumentMeta(cls, subject, chapter, number, infer_material_type(path), 0)


def build_meta(path: Path, root: Path, doc: fitz.Document) -> DocumentMeta | None:
    override = _content_override(path, root, doc)
    if override:
        cls, subject, chapter, chapter_no, material = override[:5]
    else:
        # Unsorted is not a valid chapter: require a reviewed content record.
        if any(norm(part) == "unsorted" for part in candidate_parts(path, root)[:-1]):
            return None
        cls = infer_class(path, root)
        subject = infer_subject(path, root, cls)
        chapter, chapter_no = infer_chapter(path, root)
        material = infer_material_type(path)
    if not cls or not subject:
        return None
    duration, marks = detect_duration_marks(doc)
    return DocumentMeta(class_name=cls, subject=subject, chapter=chapter,
                        chapter_number=chapter_no, material_type=material,
                        page_count=len(doc), duration=duration, max_marks=marks)


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
