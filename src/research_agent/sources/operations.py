"""Operator utilities for consistency checks and safe evidence exports."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import AuditEvent
from .repository import SQLiteRepository
from .storage import LocalObjectStore


def verify_consistency(repository: SQLiteRepository, object_store: LocalObjectStore, project_id: str | None = None) -> dict:
    sources = repository.list_sources(project_id, include_superseded=True) if project_id else []
    missing_objects = [source.source_id for source in sources if not object_store.exists(source.sha256)]
    invalid_evidence = []
    projects = {source.project_id for source in sources}
    for project in projects:
        for evidence in repository.list_evidence(project):
            source = repository.get_source(evidence.source_id, project)
            chunk = repository.get_chunk(evidence.chunk_id, project)
            if not source or not chunk or source.version != evidence.source_version or evidence.excerpt not in chunk.text:
                invalid_evidence.append(evidence.evidence_id)
    return {"sources": len(sources), "missing_objects": missing_objects, "invalid_evidence": invalid_evidence, "ok": not missing_objects and not invalid_evidence}


def export_project(repository: SQLiteRepository, project_id: str, destination: str | Path) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {"project_id": project_id,
               "sources": [item.model_dump(mode="json") for item in repository.list_sources(project_id, include_superseded=True)],
               "evidence": [item.model_dump(mode="json") for item in repository.list_evidence(project_id)],
               "audit": [item.model_dump(mode="json") for item in repository.audit_events(project_id)]}
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def backup_source_data(repository: SQLiteRepository, object_store: LocalObjectStore, destination: str | Path) -> Path:
    """Create a consistent catalog snapshot plus immutable object copy."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    repository.backup_to(destination / "catalog.sqlite3")
    target_objects = destination / "objects"
    if target_objects.exists():
        raise FileExistsError(f"backup object directory already exists: {target_objects}")
    shutil.copytree(object_store.root, target_objects)
    return destination


def rebuild_project_indexes(service, project_id: str) -> dict:
    rebuilt = []
    failed = {}
    for source in service.list_sources(project_id, include_superseded=True):
        if source.status.value in {"archived", "superseded", "failed"}:
            continue
        try:
            service.index_source(project_id, source.source_id, actor="operator")
            rebuilt.append(source.source_id)
        except Exception as exc:
            failed[source.source_id] = f"{type(exc).__name__}: {exc}"
    return {"project_id": project_id, "rebuilt": rebuilt, "failed": failed, "ok": not failed}
