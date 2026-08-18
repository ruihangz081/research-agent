"""Canonical source, document, locator, chunk, evidence, and job contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

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
    origin_url: str | None = None
    retrieved_at: datetime | None = None
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


class Claim(BaseModel):
    """Agent4 产出的结构化结论（R4）。

    每条 Claim 与分析报告中的一句结论一一对应：`text` 必须与报告正文逐字一致。
    `critical` 结论必须有至少一条支持证据，且其 `question_id` 必须来自研究需求清单。
    """

    claim_id: str
    question_id: str
    kind: Literal["fact", "derivation", "judgment"]
    importance: Literal["critical", "major", "minor"] = "major"
    text: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("claim_id", "question_id")
    @classmethod
    def _identifier_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("claim_id 与 question_id 不能为空")
        return cleaned

    @field_validator("text")
    @classmethod
    def _text_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("结论文本不能为空")
        return cleaned

    @model_validator(mode="after")
    def _critical_claim_must_have_support(self) -> "Claim":
        if self.importance == "critical" and not self.supporting_evidence_ids:
            raise ValueError("critical 结论必须至少有一条支持证据")
        return self


class ClaimsFile(BaseModel):
    """`04_claims.json`：Agent4 结论台账的顶层容器。"""

    schema_version: Literal["1.0"] = "1.0"
    claims: list[Claim]

    @model_validator(mode="after")
    def _claim_ids_are_unique(self) -> "ClaimsFile":
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in self.claims:
            if item.claim_id in seen:
                duplicates.append(item.claim_id)
            seen.add(item.claim_id)
        if duplicates:
            raise ValueError(f"claim_id 重复：{', '.join(sorted(set(duplicates)))}")
        return self


class ResearchTaskUpdate(BaseModel):
    """Agent3 在 ``feedback.tasks`` 中提出的新任务或状态更新。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    question_id: str
    description: str = Field(min_length=1)
    task_type: Literal[
        "coverage_gap", "corroboration", "conflict_resolution", "analysis_gap"
    ] = "coverage_gap"
    priority: Literal["critical", "normal"] = "normal"
    target_period: str | None = None
    min_source_tier: Literal["S", "A", "B", "D"] | None = None
    required_independent_sources: int = Field(default=1, ge=1)
    completion_criteria: str = ""
    status: Literal["pending", "completed", "blocked", "waived"] = "pending"
    completed_evidence_ids: list[str] = Field(default_factory=list)
    created_round: int | None = Field(default=None, ge=1)
    completed_round: int | None = None
    blocked_reason: str | None = None

    @field_validator("task_id", "target_period", "blocked_reason")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("question_id")
    @classmethod
    def _question_id_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question_id 不能为空")
        return cleaned

    @field_validator("description")
    @classmethod
    def _description_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("任务描述不能为空")
        return cleaned

    @field_validator("completion_criteria")
    @classmethod
    def _strip_completion_criteria(cls, value: str) -> str:
        return value.strip()

    @field_validator("completed_evidence_ids")
    @classmethod
    def _deduplicate_evidence_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def _status_contract(self) -> "ResearchTaskUpdate":
        if self.status == "completed" and not self.completed_evidence_ids:
            raise ValueError("completed 任务必须回填至少一条 completed_evidence_ids")
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("blocked 任务必须填写 blocked_reason")
        return self


class ResearchTask(BaseModel):
    """持久化在 ``03_tasks.json`` 中的结构化补研任务。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    question_id: str
    description: str = Field(min_length=1)
    task_type: Literal[
        "coverage_gap", "corroboration", "conflict_resolution", "analysis_gap"
    ] = "coverage_gap"
    priority: Literal["critical", "normal"] = "normal"
    target_period: str | None = None
    min_source_tier: Literal["S", "A", "B", "D"] | None = None
    required_independent_sources: int = Field(default=1, ge=1)
    completion_criteria: str = ""
    status: Literal["pending", "completed", "blocked", "waived"] = "pending"
    source_ids: list[str] = Field(default_factory=list)
    completed_evidence_ids: list[str] = Field(default_factory=list)
    created_round: int = Field(ge=1)
    updated_round: int | None = Field(default=None, ge=1)
    completed_round: int | None = None
    blocked_reason: str | None = None

    @field_validator("task_id", "question_id")
    @classmethod
    def _identifier_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("task_id 与 question_id 不能为空")
        return cleaned

    @field_validator("description")
    @classmethod
    def _description_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("任务描述不能为空")
        return cleaned

    @field_validator("target_period", "blocked_reason")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("source_ids", "completed_evidence_ids")
    @classmethod
    def _deduplicate_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def _status_contract(self) -> "ResearchTask":
        if self.status == "completed" and not self.completed_evidence_ids:
            raise ValueError("completed 任务必须回填至少一条 completed_evidence_ids")
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("blocked 任务必须填写 blocked_reason")
        return self


class ResearchTasksFile(BaseModel):
    """`03_tasks.json`：补研任务台账的顶层容器。"""

    schema_version: Literal["1.0"] = "1.0"
    tasks: list[ResearchTask] = Field(default_factory=list)

    @model_validator(mode="after")
    def _task_ids_are_unique(self) -> "ResearchTasksFile":
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in self.tasks:
            if item.task_id in seen:
                duplicates.append(item.task_id)
            seen.add(item.task_id)
        if duplicates:
            raise ValueError(f"task_id 重复：{', '.join(sorted(set(duplicates)))}")
        return self


class ResearchTaskExecution(BaseModel):
    """Agent2 对单个补研任务的本轮执行回填。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    status: Literal["sourced", "blocked"]
    source_ids: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None

    @field_validator("task_id")
    @classmethod
    def _strip_task_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_ids")
    @classmethod
    def _deduplicate_source_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def _execution_contract(self) -> "ResearchTaskExecution":
        if self.status == "sourced" and not self.source_ids:
            raise ValueError("sourced 任务必须回填至少一个 source_id")
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("blocked 任务必须填写 blocked_reason")
        return self


class ResearchTaskExecutionReport(BaseModel):
    """Agent2 每轮写入 ``03_raw_data/task_results_round_N.json`` 的回填。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    round: int = Field(ge=1)
    results: list[ResearchTaskExecution] = Field(default_factory=list)

    @model_validator(mode="after")
    def _task_ids_are_unique(self) -> "ResearchTaskExecutionReport":
        task_ids = [item.task_id for item in self.results]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("任务回填中存在重复 task_id")
        return self


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
    next_attempt_at: datetime | None = None


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
