import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.agents.formatter import _finalize_evidence_appendix
from research_agent.agents.validator import ValidationFeedback
from research_agent.orchestrator import _deterministic_convergence
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.enums import VerificationStatus
from research_agent.sources.models import EvidenceRecord
from research_agent.state import ProjectState
from research_agent.tools.builtins import project_sources


def prepared_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    project_sources._service.cache_clear()
    state = ProjectState(topic="evidence", date_str="20260717")
    state.project_dir.mkdir(parents=True)
    state.save()
    repository = SQLiteRepository(config.SOURCE_DATA_DIR / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(config.SOURCE_DATA_DIR / "objects"))
    source = service.register_bytes(state.project_dir.name, "facts.txt", b"Revenue reached 42 million").source
    source.source_tier = "S"
    repository.update_source(source)
    service.parse_source(state.project_dir.name, source.source_id)
    chunks = service.index_source(state.project_dir.name, source.source_id)
    service.activate(state.project_dir.name, source.source_id)
    return state, repository, service, source, chunks[0]


def test_deterministic_convergence_requires_persisted_evidence(tmp_path: Path, monkeypatch) -> None:
    state, repository, service, source, chunk = prepared_state(tmp_path, monkeypatch)
    feedback = ValidationFeedback(round=1, converged=True)
    assert _deterministic_convergence(state, feedback) is False
    evidence = EvidenceRecord(
        evidence_id="ev", project_id=state.project_dir.name, research_question_id="q1",
        claim="Revenue reached 42 million", source_id=source.source_id, source_version=source.version,
        chunk_id=chunk.chunk_id, locator=chunk.locators[0], excerpt="Revenue reached 42 million",
        source_tier="S", verification_status=VerificationStatus.SUPPORTED, confidence=1,
    )
    service.record_evidence(evidence)
    assert _deterministic_convergence(state, feedback) is True
    evidence.evidence_id = "contradiction"
    evidence.verification_status = VerificationStatus.CONTRADICTED
    repository.put_evidence(evidence)
    assert _deterministic_convergence(state, feedback) is False
    repository.close()


@pytest.mark.anyio
async def test_record_tool_and_formatter_use_exact_evidence(tmp_path: Path, monkeypatch) -> None:
    state, repository, service, source, chunk = prepared_state(tmp_path, monkeypatch)
    result = json.loads(await project_sources.record_project_evidence(
        state.project_dir.name, "q1", "Revenue reached 42 million", source.source_id,
        source.version, chunk.chunk_id, "Revenue reached 42 million",
        chunk.locators[0].model_dump_json(),
    ))
    assert result["source_id"] == source.source_id
    report = state.project_dir / config.FILE_FINAL_REPORT
    report.write_text("# Report\n\nRevenue reached 42 million.", encoding="utf-8")
    _finalize_evidence_appendix(state, report)
    rendered = report.read_text(encoding="utf-8")
    assert "## 可追溯证据索引" in rendered
    assert f"[src:{source.source_id}:v1" in rendered
    repository.close()
