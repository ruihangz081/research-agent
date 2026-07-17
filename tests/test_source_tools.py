import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.models import SourceLocator
from research_agent.sources.enums import LocatorType
from research_agent.tools import default_registry
from research_agent.tools.builtins import project_sources


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
    assert read["untrusted_evidence"] is True
    denied = await project_sources.list_project_sources("other-project")
    assert json.loads(denied)["items"] == []
    assert {"ListProjectSources", "SearchProjectSources", "ReadProjectSource", "InspectSourceEvidence"}.issubset(default_registry._schemas)
    repository.close()
