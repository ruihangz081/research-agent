from pathlib import Path

from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.enums import LocatorType
from research_agent.sources.models import EvidenceRecord, SourceLocator
from research_agent.sources.search import SearchFilters


def prepared(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    result = service.register_bytes("project", "report.txt", b"# Market\nRevenue reached 42 million in 2025.\nCosts remained stable.")
    service.parse_source("project", result.source.source_id)
    service.index_source("project", result.source.source_id)
    service.activate("project", result.source.source_id)
    return repository, service, result.source


def test_hybrid_search_returns_keyword_and_semantic_scores(tmp_path: Path) -> None:
    repository, service, source = prepared(tmp_path)
    results = service.search("project", "revenue 42", limit=5)
    assert results
    assert results[0].source.source_id == source.source_id
    assert results[0].keyword_score > 0
    assert results[0].semantic_score > 0
    repository.close()


def test_filter_and_project_isolation(tmp_path: Path) -> None:
    repository, service, source = prepared(tmp_path)
    other = service.register_bytes("other", "report.txt", b"Revenue 999")
    service.parse_source("other", other.source.source_id)
    service.index_source("other", other.source.source_id)
    service.activate("other", other.source.source_id)
    assert all(item.source.project_id == "project" for item in service.search("project", "revenue"))
    assert service.search("project", "revenue", filters=SearchFilters(source_ids=frozenset({other.source.source_id}))) == []
    repository.close()


def test_evidence_requires_exact_excerpt_and_version(tmp_path: Path) -> None:
    repository, service, source = prepared(tmp_path)
    result = service.search("project", "revenue", limit=1)[0]
    evidence = EvidenceRecord(evidence_id="ev_1", project_id="project", research_question_id="q1", claim="Revenue was 42 million",
                              source_id=source.source_id, source_version=source.version, chunk_id=result.chunk.chunk_id,
                              locator=SourceLocator(locator_type=LocatorType.OFFSET), excerpt="Revenue reached 42 million", confidence=0.95)
    service.record_evidence(evidence)
    assert repository.list_evidence("project")[0].evidence_id == "ev_1"
    evidence.excerpt = "invented text"
    try:
        service.record_evidence(evidence)
    except ValueError as exc:
        assert "excerpt" in str(exc)
    else:
        raise AssertionError("invented evidence was accepted")
    repository.close()
