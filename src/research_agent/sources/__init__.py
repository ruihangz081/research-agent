"""Research material source and evidence infrastructure."""
from .enums import BlockType, JobStatus, LocatorType, SourceStatus, VerificationStatus
from .models import (
    AuditEvent, ContentBlock, EvidenceRecord, ExtractionQuality, ExtractionWarning,
    ImageBlock, Job, PageInfo, SearchResult, SourceAsset, SourceChunk, SourceDocument,
    SourceIngestResult, SourceLocator, TableBlock, TableCell,
)
from .repository import SQLiteRepository
from .service import SourceService
from .storage import LocalObjectStore

__all__ = [
    "AuditEvent", "BlockType", "ContentBlock", "EvidenceRecord", "ExtractionQuality",
    "ExtractionWarning", "ImageBlock", "Job", "JobStatus", "LocalObjectStore", "LocatorType",
    "PageInfo", "SearchResult", "SourceAsset", "SourceChunk", "SourceDocument", "SourceIngestResult",
    "SourceLocator", "SourceService", "SourceStatus", "SQLiteRepository", "TableBlock", "TableCell",
    "VerificationStatus",
]
