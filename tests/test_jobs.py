from datetime import timedelta
from pathlib import Path

from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.enums import JobStatus
from research_agent.sources.jobs import JobQueue, SourceWorker
from research_agent.sources.models import utcnow


def build(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    return repository, service, JobQueue(repository)


def test_duplicate_job_submission_is_idempotent(tmp_path: Path) -> None:
    repository, service, queue = build(tmp_path)
    source = service.register_bytes("project", "report.txt", b"body").source
    first = queue.enqueue("project", "ingest", "ingest:project:sha", source.source_id)
    second = queue.enqueue("project", "ingest", "ingest:project:sha", source.source_id)
    assert first.job_id == second.job_id
    repository.close()


def test_worker_processes_job_and_persists_progress(tmp_path: Path) -> None:
    repository, service, queue = build(tmp_path)
    source = service.register_bytes("project", "report.txt", b"body").source
    job = queue.enqueue("project", "ingest", "ingest:1", source.source_id)
    SourceWorker(service, "worker-test").run_once()
    completed = repository.get_job(job.job_id, "project")
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.progress == 100
    assert repository.get_document(source.source_id, "project") is not None
    repository.close()


def test_worker_crash_recovery_requeues_stale_job(tmp_path: Path) -> None:
    repository, service, queue = build(tmp_path)
    source = service.register_bytes("project", "report.txt", b"body").source
    job = queue.enqueue("project", "ingest", "ingest:2", source.source_id)
    claimed = repository.claim_next_job("dead-worker")
    claimed.heartbeat_at = utcnow() - timedelta(hours=1)
    repository.update_job(claimed)
    recovered = repository.recover_stale_jobs(timeout_seconds=1)
    assert recovered[0].status == JobStatus.QUEUED
    SourceWorker(service, "replacement-worker").run_once()
    assert repository.get_job(job.job_id).status == JobStatus.SUCCEEDED
    repository.close()


def test_queued_job_can_be_cancelled_with_project_boundary(tmp_path: Path) -> None:
    repository, service, queue = build(tmp_path)
    job = queue.enqueue("project", "rebuild_index", "rebuild:1", "src_missing")
    cancelled = queue.cancel("project", job.job_id)
    assert cancelled.status == JobStatus.CANCELLED
    try:
        queue.cancel("other-project", job.job_id)
    except KeyError:
        pass
    else:
        raise AssertionError("cross-project cancellation was accepted")
    assert SourceWorker(service).run_once() is None
    repository.close()


def test_rebuild_index_recreates_missing_chunks(tmp_path: Path) -> None:
    repository, service, queue = build(tmp_path)
    source = service.register_bytes("project", "report.txt", b"rebuild this evidence").source
    service.parse_source("project", source.source_id)
    assert repository.all_chunks("project") == []
    job = queue.enqueue("project", "rebuild_index", "rebuild:actual", source.source_id)
    SourceWorker(service).run_once()
    assert repository.get_job(job.job_id).status == JobStatus.SUCCEEDED
    assert repository.all_chunks("project")
    repository.close()
