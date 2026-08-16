"""Project-scoped domain service shared by Web, CLI, workers, and agent tools."""
from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

from .enums import SourceStatus
from .models import AuditEvent, SourceAsset, SourceIngestResult, utcnow
from .repository import SQLiteRepository
from .security import SourceSecurityError, inspect_zip, inspect_zip_stream, safe_filename, sha256_bytes
from .storage import LocalObjectStore
from .observability import metrics


_MEDIA_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _logical_source_id(filename: str) -> str:
    stem = Path(filename).stem.casefold()
    slug = re.sub(r"[^a-z0-9一-鿿]+", "-", stem).strip("-")
    return slug or uuid.uuid4().hex


def detect_media_type(filename: str, data: bytes) -> str:
    for signature, media_type in _MEDIA_SIGNATURES:
        if data.startswith(signature):
            if media_type == "application/zip":
                extension = Path(filename).suffix.lower()
                return {
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                }.get(extension, media_type)
            return media_type
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or "application/octet-stream"


class SourceService:
    def __init__(self, repository: SQLiteRepository, object_store: LocalObjectStore, max_upload_bytes: int = 512 * 1024 * 1024, embedding_provider=None):
        self.repository = repository
        self.object_store = object_store
        self.max_upload_bytes = max_upload_bytes
        self.embedding_provider = embedding_provider

    def register_bytes(
        self,
        project_id: str,
        filename: str,
        data: bytes,
        *,
        collection_id: str | None = None,
        logical_source_id: str | None = None,
        actor: str = "user",
        confidentiality: str = "internal",
    ) -> SourceIngestResult:
        if not project_id or "/" in project_id or "\\" in project_id or project_id in {".", ".."}:
            raise SourceSecurityError("invalid project_id")
        filename = safe_filename(filename)
        if not data:
            raise SourceSecurityError("empty uploads are not allowed")
        if len(data) > self.max_upload_bytes:
            raise SourceSecurityError("upload exceeds configured size limit")
        suffix = Path(filename).suffix.lower()
        if data.startswith(b"PK\x03\x04"):
            if suffix not in {".zip", ".docx", ".xlsx", ".pptx"}:
                raise SourceSecurityError("zip container extension does not match content")
            inspect_zip(data)
        if data.startswith(b"%PDF-") and suffix != ".pdf":
            raise SourceSecurityError("PDF signature does not match extension")
        if (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff")) and suffix not in {".png", ".jpg", ".jpeg"}:
            raise SourceSecurityError("image signature does not match extension")
        digest = sha256_bytes(data)
        existing = self.repository.get_by_sha256(project_id, digest)
        if existing:
            self._audit(project_id, existing.source_id, actor, "source.deduplicated", {"sha256": digest})
            return SourceIngestResult(source=existing, deduplicated=True)

        digest, storage_uri = self.object_store.put(data)
        return self._register_object(project_id, filename, len(data), digest, storage_uri,
                                     detect_media_type(filename, data), collection_id=collection_id,
                                     logical_source_id=logical_source_id, actor=actor, confidentiality=confidentiality)

    def register_stream(
        self, project_id: str, filename: str, stream, *, collection_id: str | None = None,
        logical_source_id: str | None = None, actor: str = "user", confidentiality: str = "internal",
    ) -> SourceIngestResult:
        if not project_id or "/" in project_id or "\\" in project_id or project_id in {".", ".."}:
            raise SourceSecurityError("invalid project_id")
        filename = safe_filename(filename)
        stream.seek(0, 2)
        size = stream.tell()
        if size <= 0:
            raise SourceSecurityError("empty uploads are not allowed")
        if size > self.max_upload_bytes:
            raise SourceSecurityError("upload exceeds configured size limit")
        stream.seek(0)
        head = stream.read(32)
        stream.seek(0)
        suffix = Path(filename).suffix.lower()
        if head.startswith(b"PK\x03\x04"):
            if suffix not in {".zip", ".docx", ".xlsx", ".pptx"}:
                raise SourceSecurityError("zip container extension does not match content")
            inspect_zip_stream(stream)
        if head.startswith(b"%PDF-") and suffix != ".pdf":
            raise SourceSecurityError("PDF signature does not match extension")
        if (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff")) and suffix not in {".png", ".jpg", ".jpeg"}:
            raise SourceSecurityError("image signature does not match extension")
        digest, storage_uri, stored_size = self.object_store.put_stream(stream)
        return self._register_object(project_id, filename, stored_size, digest, storage_uri,
                                     detect_media_type(filename, head), collection_id=collection_id,
                                     logical_source_id=logical_source_id, actor=actor, confidentiality=confidentiality)

    def _register_object(
        self, project_id: str, filename: str, file_size: int, digest: str, storage_uri: str,
        media_type: str, *, collection_id: str | None, logical_source_id: str | None,
        actor: str, confidentiality: str,
    ) -> SourceIngestResult:
        existing = self.repository.get_by_sha256(project_id, digest)
        if existing:
            self._audit(project_id, existing.source_id, actor, "source.deduplicated", {"sha256": digest})
            return SourceIngestResult(source=existing, deduplicated=True)
        logical_id = logical_source_id or _logical_source_id(filename)
        latest = self.repository.latest_version(project_id, logical_id)
        version = latest + 1
        now = utcnow()
        source = SourceAsset(
            source_id=_id("src"), project_id=project_id, collection_id=collection_id,
            logical_source_id=logical_id, version=version, original_filename=filename,
            detected_media_type=media_type, file_size=file_size, sha256=digest,
            storage_uri=storage_uri, status=SourceStatus.QUARANTINED, enabled=False,
            confidentiality=confidentiality, created_at=now, updated_at=now,
        )
        if not self.repository.put_source(source):
            winner = self.repository.get_by_sha256(project_id, digest)
            if winner:
                return SourceIngestResult(source=winner, deduplicated=True)
            raise RuntimeError("source uniqueness conflict")
        if latest:
            for previous in self.repository.list_sources(project_id, include_superseded=True):
                if previous.logical_source_id == logical_id and previous.version == latest:
                    previous.status = SourceStatus.SUPERSEDED
                    previous.enabled = False
                    previous.updated_at = now
                    self.repository.update_source(previous)
                    break
        self._audit(project_id, source.source_id, actor, "source.registered", {
            "filename": filename, "sha256": digest, "version": version, "media_type": source.detected_media_type,
        })
        metrics.event("source.registered", project_id=project_id, source_id=source.source_id, bytes=file_size)
        return SourceIngestResult(source=source)

    def update_metadata(self, project_id: str, source_id: str, values: dict, actor: str = "user") -> SourceAsset:
        def apply(source: SourceAsset) -> None:
            for key, value in values.items():
                setattr(source, key, value)
            source.updated_at = utcnow()

        source = self.repository.mutate_source(source_id, project_id, apply)
        self._audit(project_id, source_id, actor, "source.updated", values)
        return source

    def parse_source(self, project_id: str, source_id: str, actor: str = "worker"):
        """Parse the immutable object and persist only a derived document layer."""
        from .parsers import ParseError, parse_bytes
        source = self.get_source(project_id, source_id)
        source.status = SourceStatus.PARSING
        source.updated_at = utcnow()
        self.repository.update_source(source)
        try:
            with metrics.timed("source.parse.seconds"):
                document = parse_bytes(source.source_id, self.object_store.get(source.sha256), source.original_filename).document
        except ParseError as exc:
            source.status = SourceStatus.FAILED
            source.updated_at = utcnow()
            self.repository.update_source(source)
            self._audit(project_id, source_id, actor, "source.parse_failed", {"error": str(exc)})
            raise
        self.repository.put_document(document)
        source.status = SourceStatus.NEEDS_REVIEW if any(w.severity in {"high", "error"} for w in document.warnings) else SourceStatus.READY
        source.parser_version = "registry-1"
        source.updated_at = utcnow()
        self.repository.update_source(source)
        self._audit(project_id, source_id, actor, "source.parsed", {
            "blocks": len(document.blocks), "tables": len(document.tables), "warnings": len(document.warnings),
        })
        metrics.event("source.parsed", project_id=project_id, source_id=source_id, warnings=len(document.warnings))
        return document

    def run_ocr(self, project_id: str, source_id: str, engine=None, actor: str = "worker"):
        from .ocr import TesseractEngine, ocr_document, render_pdf_pages
        source = self.get_source(project_id, source_id)
        document = self.repository.get_document(source_id, project_id)
        if document is None:
            document = self.parse_source(project_id, source_id, actor)
        if engine is None:
            engine = TesseractEngine()
        if source.original_filename.lower().endswith(".pdf"):
            page_numbers = [page.page_number for page in document.pages if page.is_scanned]
            image_pages = render_pdf_pages(self.raw_bytes(project_id, source_id), page_numbers)
        else:
            import io
            from PIL import Image
            image = Image.open(io.BytesIO(self.raw_bytes(project_id, source_id)))
            image_pages = {1: image.copy()}
        image_pages = {page: image for page, image in image_pages.items() if image is not None}
        document = ocr_document(document, image_pages, engine)
        self.repository.put_document(document)
        source.status = SourceStatus.NEEDS_REVIEW if any(w.severity in {"high", "error"} for w in document.warnings) else SourceStatus.READY
        source.updated_at = utcnow()
        self.repository.update_source(source)
        self._audit(project_id, source_id, actor, "source.ocr_completed", {"pages": document.quality.ocr_pages})
        return document

    def index_source(self, project_id: str, source_id: str, actor: str = "worker"):
        from .chunking import chunk_document
        source = self.get_source(project_id, source_id)
        document = self.repository.get_document(source_id, project_id)
        if document is None:
            document = self.parse_source(project_id, source_id, actor)
        with metrics.timed("source.index.seconds"):
            chunks = chunk_document(source, document)
            if self.embedding_provider and chunks:
                vectors = self.embedding_provider.embed([chunk.text for chunk in chunks])
                for chunk in chunks:
                    chunk.embedding_model = self.embedding_provider.model_name
                    chunk.embedding_version = "1"
            self.repository.replace_chunks(source_id, chunks)
            if self.embedding_provider and chunks:
                self.repository.put_chunk_embeddings(chunks, self.embedding_provider.model_name, vectors)
        self._audit(project_id, source_id, actor, "source.indexed", {"chunks": len(chunks)})
        return chunks

    def search(self, project_id: str, query: str, **kwargs):
        from .search import HybridSearchIndex
        with metrics.timed("source.search.seconds"):
            results = HybridSearchIndex(self.repository, self.embedding_provider).search(project_id, query, **kwargs)
        metrics.increment("source.search.requests")
        return results

    def read_chunk(self, project_id: str, chunk_id: str):
        chunk = self.repository.get_chunk(chunk_id, project_id)
        if chunk is None:
            raise KeyError("chunk not found in project")
        source = self.get_source(project_id, chunk.source_id)
        return {"source": source, "chunk": chunk, "raw_excerpt": chunk.text}

    def record_evidence(self, evidence, actor: str = "agent"):
        source = self.get_source(evidence.project_id, evidence.source_id)
        if source.version != evidence.source_version:
            raise ValueError(
                "evidence source version is stale: "
                f"received {evidence.source_version}, current version is {source.version}; "
                "call ReadProjectSource again and use its source_version"
            )
        chunk = self.repository.get_chunk(evidence.chunk_id, evidence.project_id)
        if chunk is None:
            raise ValueError(
                f"evidence chunk {evidence.chunk_id!r} was not found in this project; "
                "use a chunk_id returned by SearchProjectSources or ListProjectSourceChunks"
            )
        if chunk.source_id != evidence.source_id:
            raise ValueError(
                f"evidence chunk {evidence.chunk_id!r} belongs to source "
                f"{chunk.source_id!r}, not {evidence.source_id!r}; "
                "use the source_id and chunk_id returned together"
            )
        if evidence.excerpt not in chunk.text:
            raise ValueError(
                f"evidence excerpt is not present in project chunk {evidence.chunk_id!r}; "
                "copy an exact contiguous substring from ReadProjectSource.text"
            )
        if not chunk.locators:
            raise ValueError(
                f"evidence chunk {evidence.chunk_id!r} has no stable locator and cannot be recorded"
            )
        locator = evidence.locator.model_dump(mode="json", exclude_none=True)
        if not any(locator == item.model_dump(mode="json", exclude_none=True) for item in chunk.locators):
            raise ValueError(
                f"evidence locator is not present in project chunk {evidence.chunk_id!r}; "
                "copy one complete locator from ReadProjectSource.locators without editing it"
            )
        self.repository.put_evidence(evidence)
        self._audit(evidence.project_id, evidence.source_id, actor, "evidence.recorded", {"evidence_id": evidence.evidence_id})
        return evidence

    def quality_gate(self, project_id: str, requirements):
        from .quality import QualityGate
        return QualityGate(self.repository).evaluate(project_id, requirements)

    def get_source(self, project_id: str, source_id: str) -> SourceAsset:
        source = self.repository.get_source(source_id, project_id)
        if source is None:
            raise KeyError("source not found in project")
        return source

    def list_sources(self, project_id: str, include_superseded: bool = False) -> list[SourceAsset]:
        return self.repository.list_sources(project_id, include_superseded)

    def raw_bytes(self, project_id: str, source_id: str) -> bytes:
        source = self.get_source(project_id, source_id)
        return self.object_store.get(source.sha256)

    def activate(self, project_id: str, source_id: str, actor: str = "user") -> SourceAsset:
        def apply(source: SourceAsset) -> None:
            if source.status not in {SourceStatus.READY, SourceStatus.NEEDS_REVIEW, SourceStatus.ACTIVE}:
                raise ValueError(f"source cannot be activated from {source.status.value}")
            source.status = SourceStatus.ACTIVE
            source.enabled = True
            source.updated_at = utcnow()

        source = self.repository.mutate_source(source_id, project_id, apply)
        self._audit(project_id, source_id, actor, "source.activated", {})
        return source

    def archive(self, project_id: str, source_id: str, actor: str = "user") -> SourceAsset:
        def apply(source: SourceAsset) -> None:
            source.status = SourceStatus.ARCHIVED
            source.enabled = False
            source.updated_at = utcnow()

        source = self.repository.mutate_source(source_id, project_id, apply)
        self._audit(project_id, source_id, actor, "source.archived", {})
        return source

    def _audit(self, project_id: str, source_id: str | None, actor: str, action: str, details: dict) -> None:
        self.repository.put_audit(AuditEvent(
            event_id=_id("audit"), project_id=project_id, source_id=source_id,
            actor=actor, action=action, details=details,
        ))
