"""FastAPI material center routes backed by SourceService."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import anyio
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .jobs import JobQueue, SourceWorker
from .repository import SQLiteRepository
from .search import SearchFilters
from .quality import ResearchRequirement
from .service import SourceService
from .storage import LocalObjectStore
from .observability import metrics


def drain_source_jobs(service: SourceService) -> None:
    worker = SourceWorker(service)
    while worker.run_once() is not None:
        pass


def authorize_source_project(project_id: str, x_source_api_key: str | None = Header(default=None)) -> None:
    """Optional key-to-project ACL. Empty config keeps local development open."""
    raw = os.getenv("SOURCE_API_KEYS_JSON", "").strip()
    if not raw:
        return
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="invalid SOURCE_API_KEYS_JSON") from exc
    allowed = mapping.get(x_source_api_key or "")
    if allowed == "*" or isinstance(allowed, list) and ("*" in allowed or project_id in allowed):
        return
    raise HTTPException(status_code=403, detail="source project access denied")


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


def create_sources_router(service: SourceService, queue: JobQueue, *, process_in_background: bool = False) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}", dependencies=[Depends(authorize_source_project)])

    @router.post("/sources")
    async def upload_source(background_tasks: BackgroundTasks, project_id: str, file: UploadFile = File(...)):
        try:
            result = await anyio.to_thread.run_sync(lambda: service.register_stream(project_id, file.filename or "upload.txt", file.file))
            job = queue.enqueue(project_id, "ingest", f"ingest:{project_id}:{result.source.sha256}", result.source.source_id)
            if process_in_background and job.status.value == "queued":
                background_tasks.add_task(drain_source_jobs, service)
            return {"source": result.source, "job": job, "deduplicated": result.deduplicated}
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/source-batches")
    async def upload_batch(background_tasks: BackgroundTasks, project_id: str, files: Annotated[list[UploadFile], File(...)]):
        results = []
        for file in files:
            try:
                result = await anyio.to_thread.run_sync(lambda file=file: service.register_stream(project_id, file.filename or "upload.txt", file.file))
                job = queue.enqueue(project_id, "ingest", f"ingest:{project_id}:{result.source.sha256}", result.source.source_id)
                results.append({"source": result.source, "job": job, "deduplicated": result.deduplicated})
            except (ValueError, KeyError) as exc:
                results.append({"filename": file.filename, "error": str(exc)})
        if process_in_background and any(item.get("job") and item["job"].status.value == "queued" for item in results):
            background_tasks.add_task(drain_source_jobs, service)
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
            values = patch.model_dump(exclude_unset=True)
            return service.update_metadata(project_id, source_id, values)
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
    async def reprocess_source(background_tasks: BackgroundTasks, project_id: str, source_id: str):
        try:
            source = service.get_source(project_id, source_id)
            job = queue.enqueue(project_id, "reprocess", f"reprocess:{project_id}:{source.sha256}:{source.version}:{uuid.uuid4().hex}", source_id)
            if process_in_background:
                background_tasks.add_task(drain_source_jobs, service)
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

    @router.post("/quality-gate")
    async def quality_gate(project_id: str, requirements: list[dict]):
        return service.quality_gate(project_id, [ResearchRequirement(**item) for item in requirements])

    @router.get("/sources/{source_id}/evidence")
    async def inspect_evidence(project_id: str, source_id: str):
        try:
            service.get_source(project_id, source_id)
            return {"items": service.repository.list_evidence(project_id, source_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/source-metrics")
    async def source_metrics(project_id: str):
        return metrics.snapshot()

    return router


def create_app(data_dir: str | Path = ".data/sources") -> FastAPI:
    service, queue = build_runtime(data_dir)
    app = FastAPI(title="Research Agent Source Center")
    app.include_router(create_sources_router(service, queue))
    return app
