import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.agents import formatter
from research_agent.agents.formatter import (
    _can_reuse_chart_manifest,
    _compose_final_report_from_analysis,
    run_formatting,
)
from research_agent.agents.validator import ValidationFeedback
from research_agent.orchestrator import (
    PipelineError,
    ResearchPlanBlockedError,
    _assert_delivery_ready,
    _deterministic_convergence,
    _safe_run,
    migrate_research_plan,
    recover_blocked_delivery,
)
from research_agent.pipeline_errors import DeterministicContentError
from research_agent.research_plan import derive_plan_from_outline, save_plan
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.citations import render_citation
from research_agent.sources.enums import VerificationStatus
from research_agent.sources.models import EvidenceRecord
from research_agent.sources.runtime import reset_runtime
from research_agent.state import ProjectState, Stage
from research_agent.tools.builtins import project_sources


def fix_plan(state: ProjectState, *question_ids: str) -> None:
    """写入研究开始阶段固定的需求清单。

    R1 之后所有门禁都读取这份清单，因此夹具必须显式声明"这次研究要回答哪些问题"，
    而不是让系统从已有证据反推。
    """
    ids = question_ids or ("q1",)
    outline_text = (
        f"# 《{state.topic}》调研提纲\n\n## 二、核心研究问题\n"
        + "\n".join(
            f"{index}. 研究问题 {value}" for index, value in enumerate(ids, 1)
        )
        + "\n"
    )
    outline = state.project_dir / config.FILE_OUTLINE
    outline.parent.mkdir(parents=True, exist_ok=True)
    outline.write_text(outline_text, encoding="utf-8")
    state.outline_path = str(outline)
    plan, _ = derive_plan_from_outline(state.topic, outline_text)
    for item, question_id in zip(plan.requirements, ids, strict=True):
        item.question_id = question_id
    save_plan(state, plan)


def prepared_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    reset_runtime()
    state = ProjectState(topic="evidence", date_str="20260717")
    state.project_dir.mkdir(parents=True)
    state.save()
    fix_plan(state, "q1")
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


def test_deterministic_convergence_keeps_non_material_gaps_as_limitations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, repository, service, source, chunk = prepared_state(tmp_path, monkeypatch)
    service.record_evidence(EvidenceRecord(
        evidence_id="ev", project_id=state.project_dir.name, research_question_id="q1",
        claim="Revenue reached 42 million", source_id=source.source_id, source_version=source.version,
        chunk_id=chunk.chunk_id, locator=chunk.locators[0], excerpt="Revenue reached 42 million",
        source_tier="S", verification_status=VerificationStatus.SUPPORTED, confidence=1,
    ))
    feedback = ValidationFeedback(
        round=1,
        converged=True,
        gap_list=["A paid report is unavailable; public evidence covers the claim"],
    )

    assert _deterministic_convergence(state, feedback) is True
    assert state.notes["quality_gate_reasons"] == [
        "validation gaps remain: 1",
    ]
    repository.close()


def test_delivery_gate_explains_recovery_without_starting_agent5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    state = ProjectState(topic="blocked", date_str="20260720", stage=Stage.FORMATTING)
    state.save()
    # 需求清单先于证据检查阻断，所以这里显式固定一份，才能测到"证据不足"这条路径
    fix_plan(state, "q1")

    with pytest.raises(PipelineError, match="Agent5 未启动，本错误不会重试") as exc_info:
        _assert_delivery_ready(state)

    assert f"/materials?project={state.project_dir.name}" in str(exc_info.value)
    assert state.notes["delivery_blocked_stage"] == "evidence"


def test_delivery_gate_blocks_legacy_project_missing_research_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧项目没有需求清单时：阻断在需求清单，而不是伪装成"证据不足"。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    state = ProjectState(topic="legacy", date_str="20260720", stage=Stage.FORMATTING)
    state.save()

    with pytest.raises(ResearchPlanBlockedError, match="migrate-plan"):
        _assert_delivery_ready(state)

    assert state.notes["delivery_blocked_stage"] == "research_plan"
    assert state.notes["research_plan_migration_required"] is True
    assert state.failed_stage == "研究需求清单"


def test_migration_rebuilds_plan_from_existing_outline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移只做一件事：从既有提纲重建需求清单，不按已有证据反推。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    state = ProjectState(topic="legacy", date_str="20260720", stage=Stage.FORMATTING)
    state.project_dir.mkdir(parents=True, exist_ok=True)
    outline = state.project_dir / config.FILE_OUTLINE
    outline.write_text(
        "# 《X》调研提纲\n\n"
        "## 二、核心研究问题（Key Questions）\n"
        "1. **市场规模**有多大？\n"
        "2. 竞争格局如何？\n"
        "\n## 三、调研章节规划\n### 1. 市场概况\n",
        encoding="utf-8",
    )
    state.outline_path = str(outline)
    state.save()

    message = migrate_research_plan(state)

    assert "2 个研究问题" in message
    assert (state.project_dir / config.FILE_RESEARCH_REQUIREMENTS).is_file()
    assert state.notes.get("research_plan_migration_required") is None

    # 迁移只固定问题，证据仍然缺失，所以交付必须继续被阻断
    with pytest.raises(PipelineError, match="交付前证据门槛阻断"):
        _assert_delivery_ready(state)


@pytest.mark.anyio
async def test_safe_run_does_not_retry_deterministic_pipeline_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="blocked", date_str="20260720")
    attempts = 0

    async def blocked() -> None:
        nonlocal attempts
        attempts += 1
        raise PipelineError("blocked")

    with pytest.raises(PipelineError, match="blocked"):
        await _safe_run("Agent5", state, blocked)

    assert attempts == 1


def test_blocked_delivery_rewinds_after_materials_are_uploaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, repository, _service, _source, _chunk = prepared_state(tmp_path, monkeypatch)
    state.stage = Stage.FORMATTING
    state.collect_round = 3
    state.converged = False
    state.notes["quality_gate"] = "blocked"
    state.save()

    assert recover_blocked_delivery(state) is True
    assert state.stage.value == "collecting_and_validating"
    assert state.collect_round == 0
    assert state.sources_final_path is None
    repository.close()


@pytest.mark.anyio
async def test_formatter_blocks_before_generation_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    state = ProjectState(topic="blocked", date_str="20260720")
    state.project_dir.mkdir(parents=True)
    fix_plan(state, "q1")
    for attribute, filename in (
        ("analysis_path", config.FILE_ANALYSIS),
        ("sources_final_path", config.FILE_SOURCES_FINAL),
    ):
        path = state.project_dir / filename
        path.write_text("# placeholder\n", encoding="utf-8")
        setattr(state, attribute, str(path))

    with pytest.raises(RuntimeError, match="quality gate blocked final delivery"):
        await run_formatting(state)

    assert not (state.project_dir / config.FILE_FINAL_REPORT).exists()
    assert not (state.project_dir / config.FILE_CHART_MANIFEST).exists()


@pytest.mark.anyio
async def test_formatter_reuses_report_and_surfaces_typeset_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, repository, service, source, chunk = prepared_state(tmp_path, monkeypatch)
    evidence = EvidenceRecord(
        evidence_id="ev-typeset", project_id=state.project_dir.name,
        research_question_id="q1", claim="Revenue reached 42 million",
        source_id=source.source_id, source_version=source.version,
        chunk_id=chunk.chunk_id, locator=chunk.locators[0],
        excerpt="Revenue reached 42 million", source_tier="S",
        verification_status=VerificationStatus.SUPPORTED, confidence=1,
    )
    service.record_evidence(evidence)
    analysis = state.project_dir / config.FILE_ANALYSIS
    analysis_text = (
        f"# Report\n\nRevenue reached 42 million "
        f"{render_citation(evidence, source)}\n"
    )
    analysis.write_text(analysis_text, encoding="utf-8")
    state.analysis_path = str(analysis)
    sources_final = state.project_dir / config.FILE_SOURCES_FINAL
    sources_final.write_text("# input\n", encoding="utf-8")
    state.sources_final_path = str(sources_final)
    report = state.project_dir / config.FILE_FINAL_REPORT
    report.write_text(
        f"# Report\n\nRevenue reached 42 million {render_citation(evidence, source)}\n",
        encoding="utf-8",
    )
    claims = state.project_dir / config.FILE_CLAIMS
    claims.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "claims": [
                    {
                        "claim_id": "c1",
                        "question_id": "q1",
                        "kind": "fact",
                        "importance": "critical",
                        "text": "Revenue reached 42 million",
                        "supporting_evidence_ids": ["ev-typeset"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = state.project_dir / config.FILE_CHART_MANIFEST
    manifest.write_text('{"version": 1, "charts": []}', encoding="utf-8")

    async def fail_typeset(**_kwargs):
        raise RuntimeError("layout failed")

    async def fail_if_agent_runs(*_args, **_kwargs):
        raise AssertionError("formatter LLM should not rerun")

    monkeypatch.setattr(formatter, "generate_typeset_artifacts", fail_typeset)
    monkeypatch.setattr(formatter, "run_agent", fail_if_agent_runs)

    # 排版交付物生成失败不再阻断正文交付：Markdown 已就绪，降级为仅 Markdown。
    result = await run_formatting(state)

    assert result == report
    assert state.final_report_path == str(report)
    assert state.notes["latex_typeset_error"] == "layout failed"
    assert state.notes["delivery_status"] == "done_degraded"
    assert "排版交付物生成失败" in state.notes["delivery_degradation"][0]
    assert report.read_text(encoding="utf-8") == analysis_text
    assert "可追溯证据索引" not in report.read_text(encoding="utf-8")
    repository.close()


def test_formatter_does_not_reuse_invalid_chart_manifest(tmp_path: Path) -> None:
    analysis = tmp_path / "analysis.md"
    manifest = tmp_path / "manifest.json"
    analysis.write_text("# Analysis\n", encoding="utf-8")
    manifest.write_text('{"version": 1, "charts": [{"note": "bad "quote""}]}', encoding="utf-8")

    assert _can_reuse_chart_manifest(manifest, analysis) is False


def test_formatter_does_not_reuse_legacy_manifest_without_anchor(
    tmp_path: Path,
) -> None:
    analysis = tmp_path / "analysis.md"
    manifest = tmp_path / "manifest.json"
    analysis.write_text("# Analysis\n", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "charts": [
                    {
                        "id": "trend",
                        "type": "line",
                        "title": "趋势",
                        "unit": "%",
                        "as_of_date": "2026-08-17",
                        "source": "Research Agent 整理",
                        "labels": ["2026"],
                        "series": [
                            {
                                "name": "增速",
                                "values": [5],
                                "value_kind": ["actual"],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert _can_reuse_chart_manifest(manifest, analysis) is False


@pytest.mark.anyio
async def test_record_tool_uses_exact_evidence(tmp_path: Path, monkeypatch) -> None:
    state, repository, service, source, chunk = prepared_state(tmp_path, monkeypatch)
    result = json.loads(await project_sources.record_project_evidence(
        state.project_dir.name, "q1", "Revenue reached 42 million", source.source_id,
        source.version, chunk.chunk_id, "Revenue reached 42 million",
        chunk.locators[0].model_dump_json(),
    ))
    assert result["source_id"] == source.source_id
    repository.close()


def test_formatter_preserves_agent4_text_and_only_inserts_charts(tmp_path: Path) -> None:
    analysis = tmp_path / "04_analysis.md"
    report = tmp_path / "05_final_report.md"
    analysis_text = "# 行业分析\n\n开篇结论。\n\n## 供需格局\n\n供给仍然偏紧。\n"
    analysis.write_text(analysis_text, encoding="utf-8")
    manifest = formatter.ChartManifest.model_validate(
        {
            "version": 1,
            "charts": [
                {
                    "id": "supply_trend",
                    "type": "line",
                    "title": "供给增速保持低位",
                    "unit": "%",
                    "as_of_date": "2026-08-17",
                    "source": "公开资料，Research Agent 整理",
                    "placement_after": "## 供需格局",
                    "labels": ["2025", "2026E"],
                    "series": [
                        {
                            "name": "增速",
                            "values": [8, 5],
                            "value_kind": ["actual", "forecast"],
                        }
                    ],
                }
            ],
        }
    )

    _compose_final_report_from_analysis(analysis, report, manifest)
    formatter._audit_composed_report(analysis, report, manifest)

    assert report.read_text(encoding="utf-8") == (
        "# 行业分析\n\n开篇结论。\n\n## 供需格局\n\n"
        "{{chart:supply_trend}}\n\n供给仍然偏紧。\n"
    )

    report.write_text(
        report.read_text(encoding="utf-8").replace("供给仍然偏紧", "供给已经宽松"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Agent4 正文不一致"):
        formatter._audit_composed_report(analysis, report, manifest)


def test_formatter_degrades_chart_without_anchor(tmp_path: Path) -> None:
    analysis = tmp_path / "04_analysis.md"
    report = tmp_path / "05_final_report.md"
    analysis.write_text("# 行业分析\n", encoding="utf-8")
    manifest = formatter.ChartManifest.model_validate(
        {
            "version": 1,
            "charts": [
                {
                    "id": "supply_trend",
                    "type": "line",
                    "title": "供给增速保持低位",
                    "unit": "%",
                    "as_of_date": "2026-08-17",
                    "source": "公开资料，Research Agent 整理",
                    "labels": ["2025"],
                    "series": [
                        {
                            "name": "增速",
                            "values": [8],
                            "value_kind": ["actual"],
                        }
                    ],
                }
            ],
        }
    )

    # 缺 placement_after 不再作废整轮：降级到文末插入，报告仍可交付。
    _compose_final_report_from_analysis(analysis, report, manifest)
    assert "{{chart:supply_trend}}" in report.read_text(encoding="utf-8")
