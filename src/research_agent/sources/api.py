"""FastAPI material center routes backed by SourceService."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .jobs import JobQueue
from .models import EvidenceRecord
from .repository import SQLiteRepository
from .search import SearchFilters
from .service import SourceService
from .storage import LocalObjectStore


class SourcePatch(BaseModel):
    title: str | None = None
    tags: list[str] | None = None
    source_tier: str | None = None
    confidentiality: str | None = None
    user_notes: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    source_ids: list[str] | None = None
    media_types: list[str] | None = None
    source_tiers: list[str] | None = None
    include_inactive: bool = False
    adjacent: int = Field(default=1, ge=0, le=3)


def build_runtime(data_dir: str | Path) -> tuple[SourceService, JobQueue]:
    root = Path(data_dir)
    repository = SQLiteRepository(root / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(root / "objects"))
    return service, JobQueue(repository)


def create_sources_router(service: SourceService, queue: JobQueue) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/sources")
    async def upload_source(project_id: str, file: UploadFile = File(...)):
        data = await file.read()
        try:
            result = service.register_bytes(project_id, file.filename or "upload.txt", data)
            job = queue.enqueue(project_id, "ingest", f"ingest:{project_id}:{result.source.sha256}", result.source.source_id)
            return {"source": result.source, "job": job, "deduplicated": result.deduplicated}
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/source-batches")
    async def upload_batch(project_id: str, files: Annotated[list[UploadFile], File(...)]):
        results = []
        for file in files:
            data = await file.read()
            try:
                result = service.register_bytes(project_id, file.filename or "upload.txt", data)
                job = queue.enqueue(project_id, "ingest", f"ingest:{project_id}:{result.source.sha256}", result.source.source_id)
                results.append({"source": result.source, "job": job, "deduplicated": result.deduplicated})
            except (ValueError, KeyError) as exc:
                results.append({"filename": file.filename, "error": str(exc)})
        return {"items": results}

    @router.get("/sources")
    async def list_sources(project_id: str, include_superseded: bool = False):
        return {"items": service.list_sources(project_id, include_superseded)}

    @router.get("/sources/{source_id}")
    async def get_source(project_id: str, source_id: str):
        try:
            source = service.get_source(project_id, source_id)
            document = service.repository.get_document(source_id, project_id)
            return {"source": source, "document": document}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/sources/{source_id}")
    async def patch_source(project_id: str, source_id: str, patch: SourcePatch):
        try:
            source = service.get_source(project_id, source_id)
            values = patch.model_dump(exclude_unset=True)
            for key, value in values.items():
                setattr(source, key, value)
            source.updated_at = __import__("research_agent.sources.models", fromlist=["utcnow"]).utcnow()
            service.repository.update_source(source)
            service.repository.put_audit(__import__("research_agent.sources.models", fromlist=["AuditEvent", "utcnow"]).AuditEvent(
                event_id=f"audit_patch_{source_id}", project_id=project_id, source_id=source_id, actor="user", action="source.updated", details=values))
            return source
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/sources/{source_id}")
    async def archive_source(project_id: str, source_id: str):
        try:
            return service.archive(project_id, source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/sources/{source_id}/activate")
    async def activate_source(project_id: str, source_id: str):
        try:
            return service.activate(project_id, source_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/sources/{source_id}/reprocess")
    async def reprocess_source(project_id: str, source_id: str):
        try:
            source = service.get_source(project_id, source_id)
            job = queue.enqueue(project_id, "reprocess", f"reprocess:{project_id}:{source.sha256}:{source.version}", source_id)
            return job
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/source-jobs/{job_id}")
    async def get_job(project_id: str, job_id: str):
        job = service.repository.get_job(job_id, project_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @router.post("/source-jobs/{job_id}/cancel")
    async def cancel_job(project_id: str, job_id: str):
        try:
            return queue.cancel(project_id, job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/source-search")
    async def search_sources(project_id: str, request: SearchRequest):
        filters = SearchFilters(source_ids=frozenset(request.source_ids) if request.source_ids else None,
                                media_types=frozenset(request.media_types) if request.media_types else None,
                                source_tiers=frozenset(request.source_tiers) if request.source_tiers else None,
                                include_inactive=request.include_inactive)
        return {"items": service.search(project_id, request.query, limit=request.limit, filters=filters, adjacent=request.adjacent)}

    @router.get("/sources/{source_id}/read")
    async def read_source(project_id: str, source_id: str, chunk_id: str | None = None):
        try:
            if chunk_id:
                chunk = service.read_chunk(project_id, chunk_id)
                if chunk["source"].source_id != source_id:
                    raise KeyError("chunk does not belong to source")
                return chunk
            source = service.get_source(project_id, source_id)
            return {"source": source, "document": service.repository.get_document(source_id, project_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/sources/{source_id}/evidence")
    async def inspect_evidence(project_id: str, source_id: str):
        try:
            service.get_source(project_id, source_id)
            return {"items": service.repository.list_evidence(project_id, source_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router


def create_app(data_dir: str | Path = ".data/sources") -> FastAPI:
    service, queue = build_runtime(data_dir)
    app = FastAPI(title="Research Agent Source Center")
    app.include_router(create_sources_router(service, queue))
    return app

