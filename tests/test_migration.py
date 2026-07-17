from pathlib import Path

from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.migration import find_legacy_citations, import_legacy_raw_directory, migrate_legacy_report


def test_legacy_citation_migration_requires_exact_mapping(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    source = service.register_bytes("project", "old.txt", b"Historical value 10").source
    service.parse_source("project", source.source_id); service.index_source("project", source.source_id)
    chunk = repository.all_chunks("project")[0]
    assert find_legacy_citations("Claim [src: old-1, p.2] and [src:old-2]") == [("old-1", "p.2"), ("old-2", None)]
    result = migrate_legacy_report(service, "project", "Claim [src: old-1] [src:missing]", {"old-1": (source.source_id, chunk.chunk_id, "Historical value 10")})
    assert len(result.migrated_evidence) == 1
    assert result.unresolved_citations == ["missing"]
    blocked = migrate_legacy_report(service, "project", "[src:old-1]", {"old-1": (source.source_id, chunk.chunk_id, "invented")})
    assert blocked.unresolved_citations == ["old-1"]
    repository.close()


def test_legacy_raw_import_stays_in_explicit_project_boundary(tmp_path: Path) -> None:
    project_dir = tmp_path / "legacy"; raw = project_dir / "03_raw_data"; raw.mkdir(parents=True)
    (raw / "facts.txt").write_text("facts", encoding="utf-8")
    outside = tmp_path / "outside.txt"; outside.write_text("outside", encoding="utf-8")
    (raw / "link.txt").symlink_to(outside)
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    result = import_legacy_raw_directory(service, "project", project_dir)
    assert len(result.imported_sources) == 1
    assert all(source.original_filename != "link.txt" for source in service.list_sources("project"))
    repository.close()
