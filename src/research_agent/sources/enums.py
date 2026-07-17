"""Stable enums for source lifecycle and evidence quality."""
from enum import Enum


class SourceStatus(str, Enum):
    CREATED = "created"
    UPLOADING = "uploading"
    QUARANTINED = "quarantined"
    VALIDATING = "validating"
    NEEDS_PASSWORD = "needs_password"
    PARSING = "parsing"
    OCR = "ocr"
    INDEXING = "indexing"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    ARCHIVED = "archived"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONTRADICTED = "contradicted"
    STALE = "stale"


class BlockType(str, Enum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    TABLE_CAPTION = "table_caption"
    FIGURE = "figure"
    FOOTNOTE = "footnote"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_BREAK = "page_break"
    OCR_TEXT = "ocr_text"
    CODE = "code"
    QUOTE = "quote"


class LocatorType(str, Enum):
    PAGE = "page"
    PARAGRAPH = "paragraph"
    CELL = "cell"
    SLIDE = "slide"
    SHEET = "sheet"
    RANGE = "range"
    OFFSET = "offset"
    ZIP_MEMBER = "zip_member"
