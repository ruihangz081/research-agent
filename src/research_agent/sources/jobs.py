"""Durable source jobs and independent worker runtime."""
from __future__ import annotations

import argparse
import signal
import time
import uuid
from datetime import timedelta
from pathlib import Path

from .enums import JobStatus, SourceStatus
from .models import Job, utcnow
from .repository import SQLiteRepository
from .service import SourceService
from .storage import LocalObjectStore


class JobQueue:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def enqueue(self, project_id: str, job_type: str, idempotency_key: str, source_id: str | None = None, max_attempts: int = 3) -> Job:
        existing = self.repository.get_job_by_idempotency(idempotency_key, project_id)
        if existing:
            return existing
        job = Job(job_id=f"job_{uuid.uuid4().hex}", project_id=project_id, source_id=source_id,
                  job_type=job_type, idempotency_key=idempotency_key, max_attempts=max_attempts)
        if not self.repository.put_job(job):
            winner = self.repository.get_job_by_idempotency(idempotency_key, project_id)
            if winner:
                return winner
            raise RuntimeError("job idempotency conflict")
        return job

    def cancel(self, project_id: str, job_id: str) -> Job:
        job = self.repository.get_job(job_id, project_id)
        if not job:
            raise KeyError("job not found in project")
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED
            job.finished_at = utcnow()
        elif job.status == JobStatus.RUNNING:
            job.status = JobStatus.CANCEL_REQUESTED
        self.repository.update_job(job)
        return job


class SourceWorker:
    def __init__(self, service: SourceService, worker_id: str | None = None, poll_interval: float = 1.0):
        self.service = service
        self.repository = service.repository
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
        self.poll_interval = poll_interval
        self.stopping = False

    def request_stop(self, *_args) -> None:
        self.stopping = True

    def run_once(self) -> Job | None:
        job = self.repository.claim_next_job(self.worker_id)
        if not job:
            return None
        try:
            self._check_cancel(job)
            self._progress(job, 10)
            if job.job_type in {"ingest", "reprocess"}:
                if not job.source_id:
                    raise ValueError("source job requires source_id")
                self.service.parse_source(job.project_id, job.source_id, actor=self.worker_id)
                self._progress(job, 75)
                document = self.repository.get_document(job.source_id, job.project_id)
                if document and (document.images or any(page.is_scanned for page in document.pages)):
                    try:
                        self.service.run_ocr(job.project_id, job.source_id, actor=self.worker_id)
                    except RuntimeError as exc:
                        # Missing OCR executable is a diagnosable review state, not silent success.
                        source = self.service.get_source(job.project_id, job.source_id)
                        source.status = SourceStatus.NEEDS_REVIEW
                        source.updated_at = utcnow()
                        self.repository.update_source(source)
                        job.error = str(exc)
                self.service.index_source(job.project_id, job.source_id, actor=self.worker_id)
                self._progress(job, 90)
            elif job.job_type == "delete":
                if not job.source_id:
                    raise ValueError("delete job requires source_id")
                self.service.archive(job.project_id, job.source_id, actor=self.worker_id)
            elif job.job_type == "rebuild_index":
                if not job.source_id:
                    raise ValueError("rebuild job requires source_id")
                document = self.repository.get_document(job.source_id, job.project_id)
                if document is None:
                    self.service.parse_source(job.project_id, job.source_id, actor=self.worker_id)
                self.service.index_source(job.project_id, job.source_id, actor=self.worker_id)
            self._check_cancel(job)
            job.status = JobStatus.SUCCEEDED
            job.progress = 100
            job.finished_at = utcnow()
            job.heartbeat_at = utcnow()
            self.repository.update_job(job)
        except CancelledError:
            job.status = JobStatus.CANCELLED
            job.finished_at = utcnow()
            self.repository.update_job(job)
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            job.heartbeat_at = utcnow()
            if job.attempts < job.max_attempts:
                job.status = JobStatus.QUEUED
                job.next_attempt_at = utcnow() + timedelta(seconds=2 ** max(job.attempts - 1, 0))
            else:
                job.status = JobStatus.FAILED
                job.finished_at = utcnow()
            self.repository.update_job(job)
        return job

    def run_forever(self) -> None:
        self.repository.recover_stale_jobs()
        while not self.stopping:
            if self.run_once() is None:
                time.sleep(self.poll_interval)

    def _progress(self, job: Job, value: int) -> None:
        current = self.repository.get_job(job.job_id, job.project_id)
        if current and current.status == JobStatus.CANCEL_REQUESTED:
            raise CancelledError()
        job.progress = value
        job.heartbeat_at = utcnow()
        self.repository.update_job(job)

    def _check_cancel(self, job: Job) -> None:
        current = self.repository.get_job(job.job_id, job.project_id)
        if current and current.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}:
            raise CancelledError()


class CancelledError(Exception):
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Agent source ingestion worker")
    parser.add_argument("--data-dir", type=Path, default=Path(".data/sources"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    repository = SQLiteRepository(args.data_dir / "catalog.sqlite3")
    worker = SourceWorker(SourceService(repository, LocalObjectStore(args.data_dir / "objects")))
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    if args.once:
        worker.run_once()
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
