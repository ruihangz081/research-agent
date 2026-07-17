"""Operator utilities for consistency checks and safe evidence exports."""
from __future__ import annotations

import json
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
