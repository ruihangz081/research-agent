from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService, SourceStatus
from research_agent.sources.security import SourceSecurityError, inspect_zip, safe_filename


@pytest.fixture
def service(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    source_service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    yield source_service
    repository.close()


def test_registers_immutable_object_and_audit(service: SourceService) -> None:
    result = service.register_bytes("project-a", "report.txt", b"verified original text")
    source = result.source
    assert source.status == SourceStatus.QUARANTINED
    assert service.raw_bytes("project-a", source.source_id) == b"verified original text"
    events = service.repository.audit_events("project-a", source.source_id)
    assert [event.action for event in events] == ["source.registered"]


def test_sha256_deduplication_is_project_scoped(service: SourceService) -> None:
    first = service.register_bytes("project-a", "one.txt", b"same")
    duplicate = service.register_bytes("project-a", "renamed.txt", b"same")
    other_project = service.register_bytes("project-b", "one.txt", b"same")
    assert duplicate.deduplicated is True
    assert duplicate.source.source_id == first.source.source_id
    assert other_project.source.source_id != first.source.source_id


def test_new_logical_version_supersedes_previous(service: SourceService) -> None:
    first = service.register_bytes("project-a", "report.txt", b"v1")
    second = service.register_bytes("project-a", "report.txt", b"v2")
    assert second.source.version == 2
    previous = service.get_source("project-a", first.source.source_id)
    assert previous.status == SourceStatus.SUPERSEDED
    assert service.raw_bytes("project-a", previous.source_id) == b"v1"


def test_project_boundary_is_enforced(service: SourceService) -> None:
    source = service.register_bytes("project-a", "report.txt", b"secret").source
    with pytest.raises(KeyError):
        service.get_source("project-b", source.source_id)
    with pytest.raises(KeyError):
        service.raw_bytes("project-b", source.source_id)


@pytest.mark.parametrize("name", ["../secret.txt", "/tmp/secret.txt", "folder\\secret.txt"])
def test_filename_traversal_is_rejected(name: str) -> None:
    with pytest.raises(SourceSecurityError):
        safe_filename(name)


def test_container_signature_and_extension_must_match(service: SourceService) -> None:
    with pytest.raises(SourceSecurityError, match="signature"):
        service.register_bytes("project", "fake.txt", b"%PDF-1.7 fake")
    with pytest.raises(SourceSecurityError, match="container"):
        service.register_bytes("project", "fake.txt", b"PK\\x03\\x04fake")


def test_zip_traversal_and_bomb_are_rejected() -> None:
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../outside.txt", "bad")
    with pytest.raises(SourceSecurityError, match="traversal"):
        inspect_zip(traversal.getvalue())

    oversized = io.BytesIO()
    with zipfile.ZipFile(oversized, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.txt", "x" * 1024)
    with pytest.raises(SourceSecurityError, match="size"):
        inspect_zip(oversized.getvalue(), max_uncompressed=100)
