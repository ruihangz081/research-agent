"""SQLite repository for source metadata, derived documents, jobs, and audit."""
from __future__ import annotations

import sqlite3
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .enums import JobStatus, SourceStatus
from .models import AuditEvent, EvidenceRecord, Job, SourceAsset, SourceChunk, SourceDocument


_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, logical_source_id TEXT NOT NULL,
  version INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL, enabled INTEGER NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(project_id, sha256), UNIQUE(project_id, logical_source_id, version)
);
CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project_id, status, created_at);
CREATE TABLE IF NOT EXISTS documents (source_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, source_id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS chunk_embeddings (chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, model TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_source ON chunk_embeddings(source_id, model);
CREATE TABLE IF NOT EXISTS evidence (evidence_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_evidence_project ON evidence(project_id, source_id);
CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, updated_at);
CREATE TABLE IF NOT EXISTS audit (event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT, action TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class SQLiteRepository:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.execute("PRAGMA user_version = 1")
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def backup_to(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            with self._lock:
                self._connection.backup(target)
        finally:
            target.close()
        return destination

    def put_source(self, source: SourceAsset) -> bool:
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source.source_id, source.project_id, source.logical_source_id, source.version,
                     source.sha256, source.status.value, int(source.enabled), source.model_dump_json(),
                     source.created_at.isoformat(), source.updated_at.isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def update_source(self, source: SourceAsset) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE sources SET logical_source_id=?, version=?, sha256=?, status=?, enabled=?, payload=?, updated_at=? WHERE source_id=?",
                (source.logical_source_id, source.version, source.sha256, source.status.value, int(source.enabled),
                 source.model_dump_json(), source.updated_at.isoformat(), source.source_id),
            )

    def mutate_source(self, source_id: str, project_id: str, mutator: Callable[[SourceAsset], None]) -> SourceAsset:
        """Apply a source mutation atomically against the latest persisted payload."""
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload FROM sources WHERE source_id=? AND project_id=?",
                (source_id, project_id),
            ).fetchone()
            if not row:
                raise KeyError("source not found in project")
            source = SourceAsset.model_validate_json(row[0])
            mutator(source)
            self._connection.execute(
                "UPDATE sources SET logical_source_id=?, version=?, sha256=?, status=?, enabled=?, payload=?, updated_at=? WHERE source_id=? AND project_id=?",
                (source.logical_source_id, source.version, source.sha256, source.status.value, int(source.enabled),
                 source.model_dump_json(), source.updated_at.isoformat(), source.source_id, project_id),
            )
            return source

    def get_source(self, source_id: str, project_id: str | None = None) -> SourceAsset | None:
        query = "SELECT payload FROM sources WHERE source_id=?"
        params: list[object] = [source_id]
        if project_id is not None:
            query += " AND project_id=?"
            params.append(project_id)
        row = self._connection.execute(query, params).fetchone()
        return SourceAsset.model_validate_json(row[0]) if row else None

    def get_by_sha256(self, project_id: str, digest: str) -> SourceAsset | None:
        row = self._connection.execute("SELECT payload FROM sources WHERE project_id=? AND sha256=?", (project_id, digest)).fetchone()
        return SourceAsset.model_validate_json(row[0]) if row else None

    def latest_version(self, project_id: str, logical_source_id: str) -> int:
        row = self._connection.execute("SELECT MAX(version) FROM sources WHERE project_id=? AND logical_source_id=?", (project_id, logical_source_id)).fetchone()
        return int(row[0] or 0)

    def list_sources(self, project_id: str, include_superseded: bool = False) -> list[SourceAsset]:
        query = "SELECT payload FROM sources WHERE project_id=?"
        args: list[object] = [project_id]
        if not include_superseded:
            query += " AND status != ?"
            args.append(SourceStatus.SUPERSEDED.value)
        query += " ORDER BY created_at"
        return [SourceAsset.model_validate_json(row[0]) for row in self._connection.execute(query, args)]

    def put_document(self, document: SourceDocument) -> None:
        with self._lock, self._connection:
            self._connection.execute("INSERT OR REPLACE INTO documents VALUES (?, ?)", (document.source_id, document.model_dump_json()))

    def get_document(self, source_id: str, project_id: str | None = None) -> SourceDocument | None:
        if project_id and not self.get_source(source_id, project_id):
            return None
        row = self._connection.execute("SELECT payload FROM documents WHERE source_id=?", (source_id,)).fetchone()
        return SourceDocument.model_validate_json(row[0]) if row else None

    def replace_chunks(self, source_id: str, chunks: Iterable[SourceChunk]) -> None:
        chunks = list(chunks)
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM chunk_embeddings WHERE source_id=?", (source_id,))
            self._connection.execute("DELETE FROM chunks_fts WHERE source_id=?", (source_id,))
            self._connection.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            for chunk in chunks:
                self._connection.execute("INSERT INTO chunks VALUES (?, ?, ?)", (chunk.chunk_id, source_id, chunk.model_dump_json()))
                self._connection.execute("INSERT INTO chunks_fts VALUES (?, ?, ?)", (chunk.chunk_id, source_id, chunk.text))

    def put_chunk_embeddings(self, chunks: list[SourceChunk], model: str, vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("embedding count does not match chunks")
        with self._lock, self._connection:
            for chunk, vector in zip(chunks, vectors):
                self._connection.execute(
                    "INSERT OR REPLACE INTO chunk_embeddings VALUES (?, ?, ?, ?)",
                    (chunk.chunk_id, chunk.source_id, model, json.dumps(vector, separators=(",", ":"))),
                )

    def get_chunk_embeddings(self, chunk_ids: list[str], model: str) -> dict[str, list[float]]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self._connection.execute(
            f"SELECT chunk_id, payload FROM chunk_embeddings WHERE model=? AND chunk_id IN ({placeholders})",
            [model, *chunk_ids],
        )
        return {row[0]: json.loads(row[1]) for row in rows}

    def get_chunk(self, chunk_id: str, project_id: str | None = None) -> SourceChunk | None:
        row = self._connection.execute("SELECT payload FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        chunk = SourceChunk.model_validate_json(row[0]) if row else None
        if chunk and project_id and not self.get_source(chunk.source_id, project_id):
            return None
        return chunk

    def all_chunks(self, project_id: str) -> list[SourceChunk]:
        return [SourceChunk.model_validate_json(row[0]) for row in self._connection.execute(
            "SELECT c.payload FROM chunks c JOIN sources s ON s.source_id=c.source_id WHERE s.project_id=?", (project_id,))]

    def put_evidence(self, evidence: EvidenceRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute("INSERT OR REPLACE INTO evidence VALUES (?, ?, ?, ?)",
                                     (evidence.evidence_id, evidence.project_id, evidence.source_id, evidence.model_dump_json()))

    def list_evidence(self, project_id: str, source_id: str | None = None) -> list[EvidenceRecord]:
        query = "SELECT payload FROM evidence WHERE project_id=?"
        args: list[object] = [project_id]
        if source_id:
            query += " AND source_id=?"
            args.append(source_id)
        return [EvidenceRecord.model_validate_json(row[0]) for row in self._connection.execute(query, args)]

    def delete_evidence(self, evidence_id: str, project_id: str) -> bool:
        """Remove one evidence record. Used when evidence is retracted or invalidated."""
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM evidence WHERE evidence_id=? AND project_id=?",
                (evidence_id, project_id),
            )
        return cursor.rowcount > 0

    def put_job(self, job: Job) -> bool:
        with self._lock, self._connection:
            try:
                self._connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
                                         (job.job_id, job.project_id, job.status.value, job.idempotency_key,
                                          job.model_dump_json(), job.created_at.isoformat()))
                return True
            except sqlite3.IntegrityError:
                return False

    def get_job_by_idempotency(self, idempotency_key: str, project_id: str | None = None) -> Job | None:
        query = "SELECT payload FROM jobs WHERE idempotency_key=?"
        args: list[object] = [idempotency_key]
        if project_id:
            query += " AND project_id=?"
            args.append(project_id)
        row = self._connection.execute(query, args).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def update_job(self, job: Job) -> None:
        with self._lock, self._connection:
            self._connection.execute("UPDATE jobs SET status=?, payload=?, updated_at=? WHERE job_id=?",
                                     (job.status.value, job.model_dump_json(), datetime.now(timezone.utc).isoformat(), job.job_id))

    def claim_next_job(self, worker_id: str) -> Job | None:
        """Atomically move one queued job to running, safe across processes."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT payload FROM jobs WHERE status=? ORDER BY updated_at",
                    (JobStatus.QUEUED.value,),
                ).fetchall()
                now = datetime.now(timezone.utc)
                job = next(
                    (
                        candidate
                        for candidate in (Job.model_validate_json(row[0]) for row in rows)
                        if candidate.next_attempt_at is None or candidate.next_attempt_at <= now
                    ),
                    None,
                )
                if job is None:
                    self._connection.commit()
                    return None
                job.status = JobStatus.RUNNING
                job.worker_id = worker_id
                job.attempts += 1
                job.next_attempt_at = None
                job.started_at = job.started_at or now
                job.heartbeat_at = now
                self._connection.execute("UPDATE jobs SET status=?, payload=?, updated_at=? WHERE job_id=?",
                                         (job.status.value, job.model_dump_json(), now.isoformat(), job.job_id))
                self._connection.commit()
                return job
            except Exception:
                self._connection.rollback()
                raise

    def recover_stale_jobs(self, timeout_seconds: int = 900) -> list[Job]:
        cutoff = datetime.now(timezone.utc).timestamp() - timeout_seconds
        recovered: list[Job] = []
        for row in self._connection.execute("SELECT payload FROM jobs WHERE status=?", (JobStatus.RUNNING.value,)):
            job = Job.model_validate_json(row[0])
            if job.heartbeat_at and job.heartbeat_at.timestamp() < cutoff:
                job.status = JobStatus.QUEUED if job.attempts < job.max_attempts else JobStatus.FAILED
                job.error = "worker heartbeat expired"
                self.update_job(job)
                recovered.append(job)
        return recovered

    def get_job(self, job_id: str, project_id: str | None = None) -> Job | None:
        query = "SELECT payload FROM jobs WHERE job_id=?"
        args: list[object] = [job_id]
        if project_id:
            query += " AND project_id=?"
            args.append(project_id)
        row = self._connection.execute(query, args).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def queued_jobs(self, limit: int = 10) -> list[Job]:
        rows = self._connection.execute("SELECT payload FROM jobs WHERE status=? ORDER BY updated_at LIMIT ?", (JobStatus.QUEUED.value, limit))
        return [Job.model_validate_json(row[0]) for row in rows]

    def put_audit(self, event: AuditEvent) -> None:
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?)",
                                     (event.event_id, event.project_id, event.source_id, event.action, event.model_dump_json(), event.created_at.isoformat()))

    def audit_events(self, project_id: str, source_id: str | None = None) -> list[AuditEvent]:
        query = "SELECT payload FROM audit WHERE project_id=?"
        args: list[object] = [project_id]
        if source_id:
            query += " AND source_id=?"
            args.append(source_id)
        return [AuditEvent.model_validate_json(row[0]) for row in self._connection.execute(query, args)]
