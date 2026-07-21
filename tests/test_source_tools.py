import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.models import SourceLocator
from research_agent.sources.enums import LocatorType
from research_agent.sources.quality import ResearchRequirement
from research_agent.tools import default_registry
from research_agent.tools.builtins import project_sources
from research_agent.tools.builtins.web_fetch import WebResource


@pytest.mark.anyio
async def test_four_source_tools_are_project_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    project_sources._service.cache_clear()
    repository = SQLiteRepository(tmp_path / "sources" / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "sources" / "objects"))
    source = service.register_bytes("project", "facts.txt", b"Revenue was 42 million").source
    service.parse_source("project", source.source_id)
    service.index_source("project", source.source_id)
    service.activate("project", source.source_id)
    service2 = project_sources._service()
    assert service2.get_source("project", source.source_id).source_id == source.source_id
    listed = json.loads(await project_sources.list_project_sources("project"))
    assert listed["items"][0]["source_id"] == source.source_id
    searched = json.loads(await project_sources.search_project_sources("project", "revenue"))
    chunk_id = searched["items"][0]["chunk_id"]
    read = json.loads(await project_sources.read_project_source("project", source.source_id, chunk_id))
    assert read["ok"] is True
    assert read["untrusted_evidence"] is True
    chunks = json.loads(await project_sources.list_project_source_chunks("project", source.source_id))
    assert chunks["items"][0]["chunk_id"] == chunk_id
    invalid = json.loads(await project_sources.read_project_source("project", source.source_id, source.source_id))
    assert invalid["error"] == "chunk_not_found"
    assert chunk_id in invalid["available_chunk_ids"]
    denied = await project_sources.list_project_sources("other-project")
    assert json.loads(denied)["items"] == []
    assert {"ListProjectSources", "SearchProjectSources", "ListProjectSourceChunks", "ReadProjectSource", "RecordProjectEvidence", "InspectSourceEvidence", "CaptureProjectWebSource"}.issubset(default_registry._schemas)
    assert default_registry._schemas["RecordProjectEvidence"]["parameters"]["properties"]["verification_status"]["enum"] == [
        "unverified", "supported", "partially_supported", "contradicted", "stale",
    ]
    repository.close()


@pytest.mark.anyio
async def test_public_web_source_can_create_evidence_without_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    project_sources._service.cache_clear()
    html = b"""<!doctype html><html><head><title>Official Results</title></head>
    <body><h1>Quarterly update</h1><p>Revenue reached 42 million in Q2.</p></body></html>"""

    async def fake_fetch(url: str) -> WebResource:
        return WebResource(
            requested_url=url,
            final_url=url,
            content_type="text/html",
            content=html,
            text="Official Results\nQuarterly update\nRevenue reached 42 million in Q2.",
        )

    monkeypatch.setattr(project_sources, "fetch_web_resource", fake_fetch)
    captured = json.loads(await project_sources.capture_project_web_source(
        "web-project",
        "https://example.com/results",
        source_tier="A",
    ))

    source_id = captured["source"]["source_id"]
    assert captured["source"]["origin_url"] == "https://example.com/results"
    assert captured["source"]["status"] == "active"
    assert captured["chunk_count"] >= 1
    service = project_sources._service()
    source = service.get_source("web-project", source_id)
    assert source.confidentiality == "public"
    assert source.origin_url == "https://example.com/results"
    result = service.search("web-project", "42 million", limit=1)[0]
    locator = result.chunk.locators[-1]
    assert locator.locator_type == LocatorType.PARAGRAPH

    recorded = json.loads(await project_sources.record_project_evidence(
        "web-project",
        "q1",
        "Revenue reached 42 million in Q2",
        source_id,
        source.version,
        result.chunk.chunk_id,
        "Revenue reached 42 million in Q2.",
        json.dumps([locator.model_dump(mode="json")]),
    ))
    gate = service.quality_gate("web-project", [ResearchRequirement(question_id="q1")])
    assert recorded["source_id"] == source_id
    assert gate.passed is True
    project_sources._service.cache_clear()


def test_media_url_cannot_self_declare_as_s_tier() -> None:
    assert project_sources._effective_web_tier("https://finance.example.com/report", "S")[0] == "B"
    assert project_sources._effective_web_tier("https://www.hkexnews.hk/listedco/report.pdf", "S")[0] == "S"
