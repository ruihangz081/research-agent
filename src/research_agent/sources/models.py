"""Canonical source, document, locator, chunk, evidence, and job contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import BlockType, JobStatus, LocatorType, SourceStatus, VerificationStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PageInfo(BaseModel):
    page_number: int
    width: float | None = None
    height: float | None = None
    rotation: int = 0
    text: str = ""
    is_scanned: bool = False
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)


class ExtractionWarning(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "high", "error"] = "warning"
    page_number: int | None = None
    block_id: str | None = None
    method: str | None = None


class ExtractionQuality(BaseModel):
    score: float = Field(ge=0, le=1)
    text_coverage: float = Field(default=0, ge=0, le=1)
    layout_coverage: float = Field(default=0, ge=0, le=1)
    table_coverage: float = Field(default=0, ge=0, le=1)
    ocr_pages: int = 0
    warnings: int = 0


class DocumentMetadata(BaseModel):
    title: str | None = None
    publisher: str | None = None
    authors: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    language: str | None = None
    document_number: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    confidence: dict[str, float] = Field(default_factory=dict)
    user_overrides: dict[str, Any] = Field(default_factory=dict)


class SourceLocator(BaseModel):
    locator_type: LocatorType
    page_number: int | None = Field(default=None, ge=1)
    section: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    paragraph_index: int | None = None
    table_id: str | None = None
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    cell_range: str | None = None
    sheet_name: str | None = None
    slide_number: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    zip_member: str | None = None


class ContentBlock(BaseModel):
    block_id: str
    source_id: str
    block_type: BlockType
    text: str = ""
    order: int = 0
    page_number: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    locator: SourceLocator | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    is_derived: bool = False
    derivation_method: str | None = None
    source_block_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TableCell(BaseModel):
    row: int
    column: int
    value: str = ""
    formula: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class TableBlock(BaseModel):
    table_id: str
    source_id: str
    title: str | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    range: str | None = None
    rows: int = 0
    columns: int = 0
    cells: list[TableCell] = Field(default_factory=list)
    locator: SourceLocator | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    is_derived: bool = False
    derivation_method: str | None = None


class ImageBlock(BaseModel):
    image_id: str
    source_id: str
    page_number: int | None = None
    storage_uri: str | None = None
    alt_text: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    ocr_text: str | None = None
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)


class SourceDocument(BaseModel):
    source_id: str
    document_id: str
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    pages: list[PageInfo] = Field(default_factory=list)
    blocks: list[ContentBlock] = Field(default_factory=list)
    tables: list[TableBlock] = Field(default_factory=list)
    images: list[ImageBlock] = Field(default_factory=list)
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    quality: ExtractionQuality = Field(default_factory=lambda: ExtractionQuality(score=0))


class SourceAsset(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    source_id: str
    project_id: str
    collection_id: str | None = None
    logical_source_id: str
    version: int = Field(ge=1)
    original_filename: str
    detected_media_type: str
    file_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_uri: str
    status: SourceStatus
    enabled: bool = False
    title: str | None = None
    publisher: str | None = None
    authors: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    language: str | None = None
    source_tier: Literal["S", "A", "B", "D", "unclassified"] = "unclassified"
    confidentiality: Literal["public", "internal", "restricted"] = "internal"
    tags: list[str] = Field(default_factory=list)
    user_notes: str = ""
    parser_version: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SourceChunk(BaseModel):
    chunk_id: str
    source_id: str
    source_version: int
    text: str
    heading_path: list[str] = Field(default_factory=list)
    locators: list[SourceLocator] = Field(default_factory=list)
    block_ids: list[str] = Field(default_factory=list)
    token_count: int = 0
    embedding_model: str | None = None
    embedding_version: str | None = None
    content_hash: str
    ordinal: int = 0


class EvidenceRecord(BaseModel):
    evidence_id: str
    project_id: str
    research_question_id: str
    claim: str
    normalized_value: str | float | int | None = None
    unit: str | None = None
    period: str | None = None
    source_id: str
    source_version: int
    chunk_id: str
    locator: SourceLocator
    excerpt: str
    source_tier: str = "unclassified"
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_notes: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    created_at: datetime = Field(default_factory=utcnow)


class Job(BaseModel):
    job_id: str
    project_id: str
    source_id: str | None = None
    job_type: Literal["ingest", "reprocess", "rebuild_index", "delete"]
    status: JobStatus = JobStatus.QUEUED
    progress: int = Field(default=0, ge=0, le=100)
    attempts: int = 0
    max_attempts: int = 3
    idempotency_key: str
    error: str | None = None
    worker_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None


class AuditEvent(BaseModel):
    event_id: str
    project_id: str
    source_id: str | None = None
    actor: str
    action: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class SearchResult(BaseModel):
    chunk: SourceChunk
    source: SourceAsset
    score: float
    keyword_score: float = 0
    semantic_score: float = 0
    highlights: list[str] = Field(default_factory=list)


class SourceIngestResult(BaseModel):
    source: SourceAsset
    document: SourceDocument | None = None
    job: Job | None = None
    deduplicated: bool = False
