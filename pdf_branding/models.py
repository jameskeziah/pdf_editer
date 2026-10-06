from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class DocumentType(str, Enum):
    NORMAL = "normal"
    NOTES = "notes"
    KEY_POINTS = "key_points"
    TEST = "test"
    DPP = "dpp"
    PRACTICE_SHEET = "practice_sheet"
    EXERCISE = "exercise"
    NCERT = "ncert"
    UNKNOWN = "unknown"


class PageType(str, Enum):
    BLANK = "blank"
    STANDARD = "standard"
    DESIGNED = "designed"
    DARK = "dark"


class RenderStrategy(str, Enum):
    PRESERVE = "preserve"
    OVERLAY = "overlay"
    REPLACE_LOGO_ONLY = "replace_logo_only"
    REBUILD_CROP = "rebuild_crop"  # planned/opt-in; never selected by the safe default planner


class PageStrategy(str, Enum):
    STANDARD_HEADER_FOOTER_REPLACEMENT = "standard_header_footer_replacement"
    FIRST_PAGE_LARGE_LOGO_REPLACEMENT = "first_page_large_logo_replacement"
    DESIGNED_PAGE_PRESERVE = "designed_page_preserve"
    DESIGNED_PAGE_LOGO_ONLY = "designed_page_logo_only"
    LEGACY_ONLY_CLEANUP = "legacy_only_cleanup"
    LEAVE_UNTOUCHED = "leave_untouched"


class HeaderTemplate(str, Enum):
    NOTES = "notes"
    CHAPTER = "chapter"
    TEST = "test"
    PRACTICE = "practice"
    NONE = "none"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


@dataclass(slots=True)
class RectData:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_rect(cls, r: Any) -> "RectData":
        return cls(float(r.x0), float(r.y0), float(r.x1), float(r.y1))

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0, self.x1, self.y1


@dataclass(slots=True)
class DocumentMeta:
    class_name: str
    subject: str
    chapter: str
    chapter_number: str | None
    material_type: str
    page_count: int
    duration: str | None = None
    max_marks: str | None = None


@dataclass(slots=True)
class DocumentProfile:
    path: Path
    document_type: DocumentType
    confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RepeatedBands:
    header_bottom: float = 0.0
    footer_top: float | None = None
    header_confidence: float = 0.0
    footer_confidence: float = 0.0
    header_signatures: list[str] = field(default_factory=list)
    footer_signatures: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PageAnalysis:
    page_number: int
    width: float
    height: float
    page_type: PageType
    blank: bool
    dark_ratio: float
    first_content_y: float | None
    last_content_y: float | None
    legacy_rects: list[RectData] = field(default_factory=list)
    vertical_text_rects: list[RectData] = field(default_factory=list)
    local_logo_background: tuple[float, float, float] | None = None
    large_image_ratio: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PagePlan:
    page_number: int
    page_type: PageType
    strategy: RenderStrategy
    page_strategy: PageStrategy
    header_template: HeaderTemplate
    confidence: float
    header_rect: RectData | None = None
    footer_rect: RectData | None = None
    cleanup_rects: list[RectData] = field(default_factory=list)
    vertical_text_rects: list[RectData] = field(default_factory=list)
    logo_replace_rect: RectData | None = None
    logo_background: tuple[float, float, float] | None = None
    recommended_crop_top: float = 0.0
    recommended_crop_bottom: float = 0.0
    crop_is_safe: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DocumentPlan:
    source: Path
    meta: DocumentMeta
    profile: DocumentProfile
    repeated_bands: RepeatedBands
    pages: list[PagePlan]


@dataclass(slots=True)
class QAIssue:
    severity: Severity
    code: str
    message: str
    page_number: int | None = None


@dataclass(slots=True)
class QAReport:
    source: str
    output: str
    passed: bool
    page_count_source: int
    page_count_output: int
    issues: list[QAIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["issues"] = [
            {**asdict(issue), "severity": issue.severity.value} for issue in self.issues
        ]
        return data
