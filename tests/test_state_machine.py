"""Project state persistence tests using real dependencies."""
import json
from pathlib import Path

import pytest

from research_agent import config, orchestrator
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.citations import render_citation
from research_agent.sources.enums import VerificationStatus
from research_agent.sources.models import EvidenceRecord
from research_agent.sources.runtime import reset_runtime
from research_agent.state import ProjectState, Stage


@pytest.fixture
def projects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    return tmp_path


def test_create_save_and_load(projects_dir: Path) -> None:
    state = ProjectState(topic="测试行业", date_str="20260423")
    assert state.stage == Stage.INIT
    state.save()
    loaded = ProjectState.load(state.project_dir)
    assert loaded.topic == state.topic
    assert loaded.stage == Stage.INIT


def test_stage_advances_and_persists(projects_dir: Path) -> None:
    state = ProjectState(topic="状态测试", date_str="20260423")
    state.advance_to(Stage.PLANNING)
    assert ProjectState.load(state.project_dir).stage == Stage.PLANNING


def test_project_directory_stays_under_configured_root(projects_dir: Path) -> None:
    state = ProjectState(topic="路径测试", date_str="20260423")
    assert state.project_dir.resolve().is_relative_to(projects_dir.resolve())


def test_failure_is_persisted_and_cleared(projects_dir: Path) -> None:
    state = ProjectState(topic="失败标记", date_str="20260728")
    state.mark_failure("Agent2·采集第1轮", "network error")
    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.failed_stage == "Agent2·采集第1轮"
    assert reloaded.last_error == "network error"
    assert orchestrator.can_retry(reloaded) is True

    reloaded.clear_failure()
    assert ProjectState.load(state.project_dir).failed_stage is None
    assert orchestrator.can_retry(ProjectState.load(state.project_dir)) is False


def test_completed_project_is_not_retryable(projects_dir: Path) -> None:
    state = ProjectState(topic="已完成", date_str="20260728", stage=Stage.DONE)
    state.mark_failure("Agent5·排版交付", "stale failure")
    assert orchestrator.can_retry(state) is False


def test_prepare_retry_adds_round_budget_and_keeps_progress(projects_dir: Path) -> None:
    state = ProjectState(
        topic="轮次补采",
        date_str="20260728",
        stage=Stage.COLLECTING_AND_VALIDATING,
    )
    state.collect_round = 3
    state.max_collect_rounds = 3
    state.last_feedback_path = "feedback_round_3.json"
    state.mark_failure("Agent3·证据审查（第3轮）", "quality gate failed")

    message = orchestrator.prepare_retry(state)

    assert "追加采集验证轮次" in message
    assert state.max_collect_rounds == 4
    assert state.collect_round == 3
    assert state.last_feedback_path == "feedback_round_3.json"
    assert state.stage == Stage.COLLECTING_AND_VALIDATING
    assert state.retry_count == 1
    assert state.failed_stage is None


def test_prepare_retry_warns_about_cost_past_soft_limit(projects_dir: Path) -> None:
    state = ProjectState(
        topic="轮次上限",
        date_str="20260728",
        stage=Stage.COLLECTING_AND_VALIDATING,
    )
    state.collect_round = orchestrator.RETRY_ROUND_SOFT_LIMIT
    state.max_collect_rounds = orchestrator.RETRY_ROUND_SOFT_LIMIT
    state.mark_failure("Agent3·证据审查", "quality gate failed")

    message = orchestrator.prepare_retry(state, extra_rounds=2)

    # 重试不设硬上限，避免项目彻底卡死；仅提示继续重试的开销
    assert state.max_collect_rounds == orchestrator.RETRY_ROUND_SOFT_LIMIT + 2
    assert "开销" in message

    # 再次失败后依然允许重试
    state.mark_failure("Agent3·证据审查", "quality gate failed again")
    assert orchestrator.retry_blocked_reason(state) is None


def test_prepare_retry_reruns_failed_agent_stage_in_place(projects_dir: Path) -> None:
    state = ProjectState(topic="原地重跑", date_str="20260728", stage=Stage.ANALYZING)
    state.mark_failure("Agent4·深度分析", "LLM timeout")

    message = orchestrator.prepare_retry(state)

    assert state.stage == Stage.ANALYZING
    assert "Agent4·深度分析" in message
    assert state.failed_stage is None


def test_completed_project_can_rerun_from_analysis_with_backup(
    projects_dir: Path,
) -> None:
    state = ProjectState(topic="回退分析", date_str="20260731", stage=Stage.DONE)
    state.save()
    outline = state.project_dir / config.FILE_OUTLINE
    sources_final = state.project_dir / config.FILE_SOURCES_FINAL
    analysis = state.project_dir / config.FILE_ANALYSIS
    final_report = state.project_dir / config.FILE_FINAL_REPORT
    for path, content in (
        (outline, "# outline"),
        (sources_final, "# sources"),
        (analysis, "# old analysis"),
        (final_report, "# old report"),
    ):
        path.write_text(content, encoding="utf-8")
    state.outline_path = str(outline)
    state.sources_final_path = str(sources_final)
    state.analysis_path = str(analysis)
    state.final_report_path = str(final_report)
    state.save()

    message = orchestrator.prepare_stage_rerun(state, Stage.ANALYZING)

    reloaded = ProjectState.load(state.project_dir)
    backup_dir = Path(reloaded.notes["last_rerun_backup"])
    assert reloaded.stage == Stage.ANALYZING
    assert reloaded.analysis_path is None
    assert reloaded.final_report_path is None
    assert outline.exists()
    assert sources_final.exists()
    assert not analysis.exists()
    assert not final_report.exists()
    assert (backup_dir / config.FILE_ANALYSIS).read_text(encoding="utf-8") == "# old analysis"
    assert (backup_dir / config.FILE_FINAL_REPORT).read_text(encoding="utf-8") == "# old report"
    assert "旧产物已备份" in message
    assert orchestrator.available_rerun_stages(reloaded) == (
        Stage.PLANNING,
        Stage.SOURCING,
        Stage.COLLECTING_AND_VALIDATING,
        Stage.ANALYZING,
    )


def test_rerun_collection_archives_old_rounds_and_resets_progress(
    projects_dir: Path,
) -> None:
    state = ProjectState(topic="回退采集", date_str="20260731", stage=Stage.DONE)
    state.save()
    outline = state.project_dir / config.FILE_OUTLINE
    sources_draft = state.project_dir / config.FILE_SOURCES_DRAFT
    raw_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    outline.write_text("# outline", encoding="utf-8")
    sources_draft.write_text("# draft", encoding="utf-8")
    raw_dir.mkdir()
    (raw_dir / "round_1.md").write_text("old round", encoding="utf-8")
    state.outline_path = str(outline)
    state.sources_draft_path = str(sources_draft)
    state.collect_round = 3
    state.converged = True
    state.last_feedback_path = str(raw_dir / "feedback_round_3.json")
    state.save()

    orchestrator.prepare_stage_rerun(state, Stage.COLLECTING_AND_VALIDATING)

    reloaded = ProjectState.load(state.project_dir)
    backup_dir = Path(reloaded.notes["last_rerun_backup"])
    assert reloaded.stage == Stage.COLLECTING_AND_VALIDATING
    assert reloaded.collect_round == 0
    assert reloaded.converged is False
    assert reloaded.last_feedback_path is None
    assert not raw_dir.exists()
    assert (backup_dir / config.FILE_RAW_DATA_DIR / "round_1.md").exists()


def test_rerun_rejects_stage_the_project_has_not_reached(projects_dir: Path) -> None:
    state = ProjectState(
        topic="禁止前跳",
        date_str="20260731",
        stage=Stage.AWAIT_OUTLINE_APPROVAL,
    )
    state.save()

    with pytest.raises(ValueError, match="不能前跳"):
        orchestrator.prepare_stage_rerun(state, Stage.ANALYZING)


def test_cli_delete_requires_confirmation_then_removes_project(projects_dir: Path) -> None:
    from research_agent.__main__ import main

    state = ProjectState(topic="CLI删除", date_str="20260728")
    state.save()

    assert main(["delete", str(state.project_dir)]) == 1
    assert state.project_dir.exists()

    assert main(["delete", str(state.project_dir), "-y"]) == 0
    assert not state.project_dir.exists()


def test_cli_delete_refuses_paths_outside_projects_dir(projects_dir: Path, tmp_path: Path) -> None:
    from research_agent.__main__ import main

    outside = tmp_path.parent / "outside-project"
    outside.mkdir(exist_ok=True)
    (outside / "state.json").write_text("{}", encoding="utf-8")

    assert main(["delete", str(outside), "-y"]) == 2
    assert outside.exists()


def test_cli_retry_rejects_project_without_failure(projects_dir: Path) -> None:
    from research_agent.__main__ import main

    state = ProjectState(topic="CLI重试", date_str="20260728", stage=Stage.SOURCING)
    state.save()

    assert main(["retry", str(state.project_dir)]) == 2


# ═══════════════════════════════════════════════════════════════
# 状态机单一实现（CLI / Web 共用同一份 run_state_machine）
# ═══════════════════════════════════════════════════════════════


class _RecordingHost:
    """测试宿主：记录阶段日志，检查点按预设脚本处置。"""

    def __init__(
        self,
        decisions=None,
        outline_body: str = (
            "# 调研提纲\n\n## 二、核心研究问题\n1. 市场规模是多少？\n"
        ),
    ) -> None:
        self.decisions = list(decisions or [])
        self.outline_body = outline_body
        self.logs: list[str] = []
        self.checkpoints_seen: list[str] = []
        self.strategist_calls: list[str | None] = []
        self.done_announced = False

    async def run_strategist(self, state: ProjectState, feedback):
        self.strategist_calls.append(feedback)
        path = state.project_dir / config.FILE_OUTLINE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.outline_body, encoding="utf-8")
        return path

    async def resolve_checkpoint(self, state: ProjectState, spec):
        self.checkpoints_seen.append(spec.key)
        if self.decisions:
            return self.decisions.pop(0)
        return orchestrator.CheckpointResult(
            decision=orchestrator.CheckpointDecision.PAUSE
        )

    def log(self, message: str) -> None:
        self.logs.append(message)

    def announce_done(self, state: ProjectState) -> None:
        self.done_announced = True


def test_checkpoint_specs_cover_every_checkpoint_stage() -> None:
    """检查点规格必须与 Stage.is_checkpoint 完全一致，避免两处定义漂移。"""
    spec_stages = {spec.stage for spec in orchestrator.CHECKPOINT_SPECS}
    stage_flagged = {stage for stage in Stage if stage.is_checkpoint}
    assert spec_stages == stage_flagged

    keys = [spec.key for spec in orchestrator.CHECKPOINT_SPECS]
    assert len(keys) == len(set(keys))
    for spec in orchestrator.CHECKPOINT_SPECS:
        assert orchestrator.checkpoint_for(spec.stage) is spec


def test_checkpoint_for_returns_none_on_non_checkpoint_stage() -> None:
    assert orchestrator.checkpoint_for(Stage.PLANNING) is None
    assert orchestrator.checkpoint_for(Stage.DONE) is None


@pytest.mark.anyio
async def test_pause_host_stops_at_first_checkpoint(projects_dir: Path) -> None:
    """Web 风格宿主：状态机在检查点挂起，阶段停在等待审批。"""
    state = ProjectState(topic="挂起", date_str="20260728")
    host = _RecordingHost()

    await orchestrator.run_state_machine(state, host)

    assert state.stage == Stage.AWAIT_OUTLINE_APPROVAL
    assert host.checkpoints_seen == ["outline"]
    assert host.done_announced is False
    assert Path(state.outline_path).exists()


@pytest.mark.anyio
async def test_rejected_outline_reruns_strategist_with_feedback(projects_dir: Path) -> None:
    """驳回提纲后回到 PLANNING，反馈透传给 Agent1，再次到检查点挂起。"""
    state = ProjectState(topic="驳回", date_str="20260728")
    host = _RecordingHost(decisions=[
        orchestrator.CheckpointResult(
            decision=orchestrator.CheckpointDecision.REJECTED,
            feedback="请补充竞争格局",
        ),
    ])

    await orchestrator.run_state_machine(state, host)

    assert host.strategist_calls == [None, "请补充竞争格局"]
    assert state.stage == Stage.AWAIT_OUTLINE_APPROVAL
    assert host.checkpoints_seen == ["outline", "outline"]


@pytest.mark.anyio
async def test_approved_outline_advances_to_sourcing(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通过提纲后进入 Agent2，并在信息源检查点挂起。"""
    state = ProjectState(topic="通过", date_str="20260728")
    host = _RecordingHost(decisions=[
        orchestrator.CheckpointResult(
            decision=orchestrator.CheckpointDecision.APPROVED
        ),
    ])

    async def fake_tiering(target: ProjectState, feedback=None) -> Path:
        path = target.project_dir / config.FILE_SOURCES_DRAFT
        path.write_text("# sources", encoding="utf-8")
        return path

    monkeypatch.setattr(orchestrator.collector, "run_source_tiering", fake_tiering)

    await orchestrator.run_state_machine(state, host)

    assert state.stage == Stage.AWAIT_SOURCE_APPROVAL
    assert host.checkpoints_seen == ["outline", "sources_draft"]
    assert Path(state.sources_draft_path).exists()


@pytest.mark.anyio
async def test_cli_host_drives_same_state_machine_to_done(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 风格宿主（全部检查点直接通过）能把同一状态机推到 DONE。"""
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", projects_dir / "sources")
    reset_runtime()
    state = ProjectState(topic="全通", date_str="20260728")
    repository = SQLiteRepository(config.SOURCE_DATA_DIR / "catalog.sqlite3")
    service = SourceService(
        repository,
        LocalObjectStore(config.SOURCE_DATA_DIR / "objects"),
    )
    source = service.register_bytes(
        state.project_dir.name,
        "facts.txt",
        b"Revenue reached 42 million",
    ).source
    service.parse_source(state.project_dir.name, source.source_id)
    chunk = service.index_source(state.project_dir.name, source.source_id)[0]
    service.activate(state.project_dir.name, source.source_id)
    evidence = EvidenceRecord(
        evidence_id="ev-state-machine",
        project_id=state.project_dir.name,
        research_question_id="q1",
        claim="Revenue reached 42 million",
        source_id=source.source_id,
        source_version=source.version,
        chunk_id=chunk.chunk_id,
        locator=chunk.locators[0],
        excerpt="Revenue reached 42 million",
        verification_status=VerificationStatus.SUPPORTED,
        confidence=1,
    )
    service.record_evidence(evidence)
    citation = render_citation(evidence, source)
    approve = orchestrator.CheckpointResult(
        decision=orchestrator.CheckpointDecision.APPROVED
    )
    host = _RecordingHost(decisions=[approve, approve, approve])

    async def fake_tiering(target: ProjectState, feedback=None) -> Path:
        path = target.project_dir / config.FILE_SOURCES_DRAFT
        path.write_text("# sources", encoding="utf-8")
        return path

    async def fake_loop(target: ProjectState, _host) -> None:
        target.converged = True
        path = target.project_dir / config.FILE_SOURCES_FINAL
        path.write_text("# final", encoding="utf-8")
        target.sources_final_path = str(path)

    async def fake_analysis(target: ProjectState) -> Path:
        path = target.project_dir / config.FILE_ANALYSIS
        path.write_text(
            f"# analysis\n\nRevenue reached 42 million {citation}",
            encoding="utf-8",
        )
        outcome_path = target.project_dir / config.FILE_ANALYSIS_OUTCOME
        outcome_path.write_text(
            '{"schema_version":"1.0","status":"completed","gap_requests":[]}',
            encoding="utf-8",
        )
        claims_path = target.project_dir / config.FILE_CLAIMS
        claims_path.write_text(
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
                            "supporting_evidence_ids": ["ev-state-machine"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    async def fake_formatting(target: ProjectState) -> Path:
        path = target.project_dir / config.FILE_FINAL_REPORT
        path.write_text("# report", encoding="utf-8")
        return path

    monkeypatch.setattr(orchestrator.collector, "run_source_tiering", fake_tiering)
    monkeypatch.setattr(orchestrator, "_run_collect_validate_loop", fake_loop)
    monkeypatch.setattr(orchestrator, "_assert_delivery_ready", lambda target: None)
    monkeypatch.setattr(orchestrator.analyst, "run_analysis", fake_analysis)
    monkeypatch.setattr(orchestrator.formatter, "run_formatting", fake_formatting)

    await orchestrator.run_state_machine(state, host)

    assert state.stage == Stage.DONE
    assert host.done_announced is True
    assert host.checkpoints_seen == ["outline", "sources_draft", "sources_final"]
    assert Path(state.final_report_path).exists()
    repository.close()


@pytest.mark.anyio
async def test_rejected_final_sources_resets_collect_progress(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """驳回最终源清单时清空采集进度并回到采集验证阶段。"""
    state = ProjectState(
        topic="驳回终稿",
        date_str="20260728",
        stage=Stage.AWAIT_FINAL_SOURCE_APPROVAL,
    )
    state.collect_round = 2
    state.converged = True
    state.last_feedback_path = "stale.json"
    state.save()

    host = _RecordingHost(decisions=[
        orchestrator.CheckpointResult(
            decision=orchestrator.CheckpointDecision.REJECTED,
            feedback="来源权威性不足",
        ),
    ])

    captured: dict[str, object] = {}

    async def fake_loop(target: ProjectState, _host) -> None:
        captured["collect_round"] = target.collect_round
        captured["converged"] = target.converged
        captured["feedback"] = target.notes.get("sources_feedback")
        raise orchestrator.PipelineError("stop here")

    monkeypatch.setattr(orchestrator, "_run_collect_validate_loop", fake_loop)

    with pytest.raises(orchestrator.PipelineError):
        await orchestrator.run_state_machine(state, host)

    assert captured == {
        "collect_round": 0,
        "converged": False,
        "feedback": "来源权威性不足",
    }


@pytest.mark.anyio
async def test_run_pipeline_defaults_to_cli_host(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pipeline 未传 host 时使用 CliPipelineHost。"""
    state = ProjectState(topic="默认宿主", date_str="20260728")
    seen: list[object] = []

    async def capture(target: ProjectState, host) -> None:
        seen.append(host)

    monkeypatch.setattr(orchestrator, "run_state_machine", capture)

    await orchestrator.run_pipeline(state)

    assert len(seen) == 1
    assert isinstance(seen[0], orchestrator.CliPipelineHost)


@pytest.mark.anyio
async def test_cli_host_maps_approval_prompt_to_decisions(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CliPipelineHost 把 checkpoints.ask_approval 的返回映射为决策。"""
    state = ProjectState(topic="CLI宿主", date_str="20260728")
    state.save()
    host = orchestrator.CliPipelineHost()
    spec = orchestrator.checkpoint_for(Stage.AWAIT_OUTLINE_APPROVAL)

    monkeypatch.setattr(
        orchestrator.checkpoints, "ask_approval", lambda path, title: (True, "")
    )
    approved = await host.resolve_checkpoint(state, spec)
    assert approved.decision is orchestrator.CheckpointDecision.APPROVED

    monkeypatch.setattr(
        orchestrator.checkpoints, "ask_approval", lambda path, title: (False, "换来源")
    )
    rejected = await host.resolve_checkpoint(state, spec)
    assert rejected.decision is orchestrator.CheckpointDecision.REJECTED
    assert rejected.feedback == "换来源"


# ═══════════════════════════════════════════════════════════════
# Agent1 需求澄清（backlog 第 11 项）
# ═══════════════════════════════════════════════════════════════


class _ClarifyingHost(_RecordingHost):
    """先提出澄清问题，收到回答后再产出提纲。"""

    def __init__(self, questions, answers=None, **kwargs):
        super().__init__(**kwargs)
        self.pending_questions = list(questions)
        self.answers = answers
        self.clarify_calls: list[tuple[str, ...]] = []
        self.strategist_seen_history: list[int] = []

    async def run_strategist(self, state: ProjectState, feedback):
        self.strategist_calls.append(feedback)
        self.strategist_seen_history.append(len(state.clarification))
        if self.pending_questions:
            return orchestrator.StrategistOutcome(
                questions=tuple(self.pending_questions.pop(0))
            )
        path = state.project_dir / config.FILE_OUTLINE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.outline_body, encoding="utf-8")
        return orchestrator.StrategistOutcome(outline_path=path)

    async def resolve_clarification(self, state: ProjectState, questions):
        self.clarify_calls.append(questions)
        return self.answers


@pytest.mark.anyio
async def test_strategist_questions_pause_at_await_clarification(
    projects_dir: Path,
) -> None:
    """Web 风格宿主返回 None 时挂起在 AWAIT_CLARIFICATION。"""
    state = ProjectState(topic="澄清挂起", date_str="20260728")
    host = _ClarifyingHost([["调研地域范围？建议国内", "时间窗口？建议近 3 年"]], answers=None)

    await orchestrator.run_state_machine(state, host)

    assert state.stage == Stage.AWAIT_CLARIFICATION
    assert state.notes["clarification_questions"] == [
        "调研地域范围？建议国内",
        "时间窗口？建议近 3 年",
    ]
    assert host.checkpoints_seen == []


@pytest.mark.anyio
async def test_answers_feed_back_into_next_strategist_run(projects_dir: Path) -> None:
    """回答后回到 PLANNING，Agent1 能看到问答历史。"""
    state = ProjectState(topic="澄清回灌", date_str="20260728")
    host = _ClarifyingHost(
        [["地域？", "时间窗口？"]], answers=["全球", "近 5 年"]
    )

    await orchestrator.run_state_machine(state, host)

    assert state.stage == Stage.AWAIT_OUTLINE_APPROVAL
    assert [item["answer"] for item in state.clarification] == ["全球", "近 5 年"]
    # 第二次执行 Agent1 时已能看到 2 条问答
    assert host.strategist_seen_history == [0, 2]
    assert "clarification_questions" not in state.notes


@pytest.mark.anyio
async def test_blank_answers_become_explicit_default_marker(projects_dir: Path) -> None:
    """留空的回答要显式标注，否则 Agent1 无法区分"未回答"和"空字符串"。"""
    state = ProjectState(topic="留空", date_str="20260728")
    host = _ClarifyingHost([["地域？", "受众？"]], answers=["  ", ""])

    await orchestrator.run_state_machine(state, host)

    answers = [item["answer"] for item in state.clarification]
    assert all("用户未回答" in value for value in answers)


@pytest.mark.anyio
async def test_missing_answers_are_padded(projects_dir: Path) -> None:
    """答案数量少于问题数量时补默认值，不应抛 IndexError。"""
    state = ProjectState(topic="答案不足", date_str="20260728")
    host = _ClarifyingHost([["Q1", "Q2", "Q3"]], answers=["只答第一个"])

    await orchestrator.run_state_machine(state, host)

    assert len(state.clarification) == 3
    assert state.clarification[0]["answer"] == "只答第一个"
    assert "用户未回答" in state.clarification[2]["answer"]


@pytest.mark.anyio
async def test_cli_host_never_enters_clarification_stage(projects_dir: Path) -> None:
    """CLI 的澄清对话在 AgentSession 内完成，不应走 AWAIT_CLARIFICATION。"""
    state = ProjectState(topic="CLI澄清", date_str="20260728")
    host = orchestrator.CliPipelineHost()
    outline = state.project_dir / config.FILE_OUTLINE

    async def fake_strategist(target, feedback=None):
        outline.parent.mkdir(parents=True, exist_ok=True)
        outline.write_text("# outline", encoding="utf-8")
        return outline

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(orchestrator.strategist, "run_strategist", fake_strategist)
        patch.setattr(
            orchestrator.checkpoints, "ask_approval", lambda path, title: (False, "停")
        )
        outcome = await host.run_strategist(state, None)

    assert isinstance(outcome, orchestrator.StrategistOutcome)
    assert outcome.needs_clarification is False
    assert outcome.outline_path == outline


def test_plain_path_return_is_accepted(tmp_path: Path) -> None:
    """兼容直接返回 Path 的宿主实现。"""
    outcome = orchestrator._as_strategist_outcome(tmp_path / "01_outline.md")
    assert outcome.outline_path == tmp_path / "01_outline.md"
    assert outcome.needs_clarification is False


def test_clarification_stage_is_not_treated_as_agent_running() -> None:
    """AWAIT_CLARIFICATION 是等用户，不是 Agent 在跑；重启不应标记为中断。"""
    assert Stage.AWAIT_CLARIFICATION.is_agent_running is False
    assert Stage.AWAIT_CLARIFICATION.is_checkpoint is False
    assert Stage.PLANNING.is_agent_running is True


def test_clarification_stage_has_no_artifact_checkpoint_spec() -> None:
    """澄清不是产物审批检查点，不应出现在 CHECKPOINT_SPECS 中。"""
    assert orchestrator.checkpoint_for(Stage.AWAIT_CLARIFICATION) is None


def test_paused_notes_persist_and_serialize(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """额度耗尽暂停时，paused 标记写入 notes 并可序列化暴露，且不落 failed。"""
    from research_agent import web_app

    monkeypatch.setattr(config, "PROJECTS_DIR", projects_dir)
    state = ProjectState(topic="暂停测试", date_str="20260423")
    state.notes["paused"] = True
    state.notes["pause_reason"] = "quota exhausted"
    state.notes["delivery_status"] = "done_degraded"
    state.notes["delivery_degradation"] = ["PDF 生成失败"]
    state.save()

    payload = web_app._serialize_state(state)
    assert payload["paused"] is True
    assert payload["pause_reason"] == "quota exhausted"
    assert payload["delivery_status"] == "done_degraded"
    assert payload["delivery_degradation"] == ["PDF 生成失败"]
    # paused 不是 failed：不应有失败阶段
    assert payload["failed"] is False
