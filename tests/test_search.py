from pathlib import Path

from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.enums import LocatorType
from research_agent.sources.models import EvidenceRecord, SourceLocator
from research_agent.sources.search import SearchFilters


class FakeEmbeddingProvider:
    model_name = "fake-test-embedding"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "revenue" in text.casefold() else [0.0, 1.0] for text in texts]


def prepared(tmp_path: Path, embedding_provider=None):
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"), embedding_provider=embedding_provider)
    result = service.register_bytes("project", "report.txt", b"# Market\nRevenue reached 42 million in 2025.\nCosts remained stable.")
    service.parse_source("project", result.source.source_id)
    service.index_source("project", result.source.source_id)
    service.activate("project", result.source.source_id)
    return repository, service, result.source


def test_hybrid_search_returns_keyword_and_semantic_scores(tmp_path: Path) -> None:
    repository, service, source = prepared(tmp_path, FakeEmbeddingProvider())
    results = service.search("project", "revenue 42", limit=5)
    assert results
    assert results[0].source.source_id == source.source_id
    assert results[0].keyword_score > 0
    assert results[0].semantic_score > 0
    assert results[0].chunk.embedding_model == "fake-test-embedding"
    assert repository.get_chunk_embeddings([results[0].chunk.chunk_id], "fake-test-embedding")
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
                              locator=result.chunk.locators[0], excerpt="Revenue reached 42 million", confidence=0.95)
    service.record_evidence(evidence)
    assert repository.list_evidence("project")[0].evidence_id == "ev_1"
    evidence.excerpt = "invented text"
    try:
        service.record_evidence(evidence)
    except ValueError as exc:
        assert result.chunk.chunk_id in str(exc)
        assert "ReadProjectSource.text" in str(exc)
    else:
        raise AssertionError("invented evidence was accepted")
    repository.close()


def test_cross_language_financial_number_normalization(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    correct = service.register_bytes("project", "correct.txt", "公司销售额达到四千二百万美元。".encode()).source
    irrelevant = service.register_bytes("project", "irrelevant.txt", b"Annual revenue policy and reporting calendar, no financial value provided.").source
    for source in (correct, irrelevant):
        service.parse_source("project", source.source_id)
        service.index_source("project", source.source_id)
        service.activate("project", source.source_id)
    results = service.search("project", "annual revenue 42m USD", limit=2, adjacent=0)
    assert results[0].source.source_id == correct.source_id
    repository.close()
