"""Orchestrator 主控：状态机 + 阶段调度 + 检查点拦截 + 异常兜底。

完整流水线：
  Agent1(提纲) → 检查点1
  Agent2(源分层) → 检查点2
  Agent2↔3(采集-验证循环) → 检查点3
  Agent4(深度分析)
  Agent5(排版交付) → DONE

状态机只有一份实现（`run_state_machine`）。CLI 与 Web 的差异通过注入
`PipelineHost` 消除：
  - CLI（`CliPipelineHost`）：Agent1 走多轮对话，检查点阻塞等用户输入
  - Web（`web_app.WebPipelineHost`）：Agent1 单次生成，检查点保存状态后退出，
    由 HTTP 审批接口推进阶段并重新调度

异常策略：
  - Agent 执行失败时，失败阶段与原因写入 state.json（不丢数据）
  - 用户可通过 `resume` 从断点续跑，或 `retry` 复位失败阶段后重跑
  - 每个阶段最多自动重试 MAX_RETRIES 次（默认 2）
"""
from __future__ import annotations

import shutil
import traceback
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from rich.console import Console

from . import checkpoints, config, research_plan, token_usage
from .agent_loop import AgentLoopStuckError
from .agents import analyst, collector, formatter, strategist, validator
from .research_plan import ResearchPlanError
from .sources.citations import audit_analysis_citations
from .sources.runtime import get_service
from .state import ProjectState, Stage

console = Console()

MAX_RETRIES: int = 2

# 用户显式重试时，为采集验证循环追加的轮次预算，以及提示成本的软阈值
RETRY_EXTRA_ROUNDS: int = 1
RETRY_ROUND_SOFT_LIMIT: int = 10

RERUNNABLE_STAGES: tuple[Stage, ...] = (
    Stage.PLANNING,
    Stage.SOURCING,
    Stage.COLLECTING_AND_VALIDATING,
    Stage.ANALYZING,
    Stage.FORMATTING,
)
RERUN_STAGE_LABELS: dict[Stage, str] = {
    Stage.PLANNING: "战略规划",
    Stage.SOURCING: "信息源分层",
    Stage.COLLECTING_AND_VALIDATING: "采集验证",
    Stage.ANALYZING: "深度分析",
    Stage.FORMATTING: "排版交付",
}
_STAGE_PROGRESS: dict[Stage, int] = {
    Stage.INIT: 0,
    Stage.PLANNING: 1,
    Stage.AWAIT_CLARIFICATION: 1,
    Stage.AWAIT_OUTLINE_APPROVAL: 1,
    Stage.SOURCING: 2,
    Stage.AWAIT_SOURCE_APPROVAL: 2,
    Stage.COLLECTING_AND_VALIDATING: 3,
    Stage.AWAIT_FINAL_SOURCE_APPROVAL: 3,
    Stage.ANALYZING: 4,
    Stage.FORMATTING: 5,
    Stage.DONE: 6,
}


# ═════════════════════════════════════════════════════════════════
# 阶段执行 wrapper（统一异常兜底）
# ═════════════════════════════════════════════════════════════════


async def _safe_run(
    stage_name: str,
    state: ProjectState,
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """执行一个阶段的 agent 调用，附带重试 + 异常兜底。

    - 成功：返回 agent 函数的返回值
    - 确定性门槛阻断：保存状态后直接抛出，不重试
    - 失败且重试耗尽：记录失败阶段后 raise PipelineError（用户可显式重试）
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with token_usage.collect_stage(state, stage_name):
                result = await fn(*args, **kwargs)
            state.clear_failure()
            return result
        except KeyboardInterrupt:
            # 用户中断，直接保存状态抛出
            state.save()
            raise
        except AgentLoopStuckError as e:
            state.mark_failure(stage_name, str(e))
            raise PipelineError(
                f"{stage_name} 因重复工具错误提前停止：{e}。"
                "请修正工具参数后从当前项目断点续跑。"
            ) from e
        except PipelineError as e:
            # Deterministic gates are not transient agent failures. Retrying would
            # waste model calls and mislabel the blocked stage as an agent crash.
            state.mark_failure(stage_name, str(e))
            raise
        except Exception as e:
            last_err = e
            state.mark_failure(stage_name, str(e))  # 每次失败都保存状态，确保不丢数据
            if attempt < MAX_RETRIES:
                console.print(
                    f"\n[yellow]⚠ {stage_name} 第 {attempt} 次执行失败，"
                    f"正在重试（{attempt}/{MAX_RETRIES}）...[/yellow]\n"
                    f"[dim]错误：{e}[/dim]\n"
                )
            else:
                console.print(
                    f"\n[red]✗ {stage_name} 在 {MAX_RETRIES} 次尝试后仍失败[/red]\n"
                    f"[red]错误：{last_err}[/red]\n"
                    f"[dim]{traceback.format_exc()}[/dim]"
                )

    # 所有重试都失败
    raise PipelineError(
        f"{stage_name} 执行失败（已重试 {MAX_RETRIES} 次）。\n"
        f"状态已保存到：{state.state_file}\n"
        f"可在工作台点击“重试当前阶段”，或运行 "
        f"`python -m research_agent retry {state.project_dir}`。\n"
        f"原始错误：{last_err}"
    ) from last_err


class PipelineError(RuntimeError):
    """流水线阶段不可恢复的错误。"""

    pass


class DeliveryBlockedError(PipelineError):
    """Delivery is paused until deterministic evidence requirements are met."""

    pass


class QualityGateError(PipelineError):
    """采集验证轮次用尽但证据质量门槛未通过；用户可显式重试补采。"""

    pass


class ResearchPlanBlockedError(PipelineError):
    """研究需求清单缺失、为空或损坏；迁移完成前不得交付。

    单独成类，是为了让 Web 与 CLI 都能把它与"证据不足"区分开：证据不足靠补采解决，
    需求清单缺失需要用户先重新确认研究计划。
    """

    pass


class AnalysisBoundaryError(PipelineError):
    """Agent4 outcome or citations failed deterministic validation."""


class AnalysisResearchRequiredError(PipelineError):
    """Agent4 requested explicit additional research before Agent5."""


def _quality_gate_error(state: ProjectState, max_rounds: int) -> QualityGateError:
    """构造审查未通过的错误，并标记为可重试。"""
    reasons = "; ".join(state.notes.get("quality_gate_reasons", [])) or "未给出具体原因"
    stage_name = f"Agent3·证据审查（第{state.collect_round}轮）"
    error = QualityGateError(
        f"达到最大轮次 {max_rounds}，但证据质量门槛未通过：{reasons}。"
        f"可在工作台点击“重试”，系统会追加轮次预算并从第 {state.collect_round + 1} 轮继续补采验证。"
    )
    state.mark_failure(stage_name, str(error))
    return error


def _fixed_requirements(state: ProjectState) -> list[Any]:
    """读取研究开始阶段固定下来的完整需求集合。

    这是 R1 的核心：需求集合与"已经找到了什么证据"完全无关。缺失或损坏时抛
    `ResearchPlanBlockedError`，绝不退化为空集合。
    """
    try:
        plan = research_plan.require_plan(state)
    except ResearchPlanError as exc:
        raise ResearchPlanBlockedError(str(exc)) from exc
    return plan.as_requirements()


def _deterministic_convergence(state: ProjectState, feedback: validator.ValidationFeedback) -> bool:
    """A model convergence claim is advisory; the fixed requirement set decides readiness."""
    service = get_service(config.SOURCE_DATA_DIR)
    project_id = state.project_dir.name
    try:
        requirements = _fixed_requirements(state)
    except ResearchPlanBlockedError as exc:
        # 需求清单缺失时不能宣布收敛；把原因留在 notes 里供工作台与 CLI 展示。
        state.notes["quality_gate"] = "blocked"
        state.notes["quality_gate_reasons"] = [str(exc)]
        state.save()
        return False
    gate = service.quality_gate(project_id, requirements)
    unresolved_conflicts = [item.topic for item in feedback.conflicts if not item.resolution]
    reasons = list(gate.reasons)
    if feedback.gap_list:
        reasons.append(f"validation gaps remain: {len(feedback.gap_list)}")
    if unresolved_conflicts:
        reasons.append(f"unresolved feedback conflicts: {', '.join(unresolved_conflicts)}")
    if not feedback.converged:
        reasons.append("validator did not declare convergence")
    # The validator may retain known, non-material limitations (for example a
    # paywalled industry report) in gap_list while still declaring convergence.
    # Keep those limitations visible, but do not turn every residual gap into a
    # hard stop. Persisted evidence and unresolved conflicts remain deterministic
    # blockers; the validator is responsible for deciding whether a gap is major.
    ready = feedback.converged and not unresolved_conflicts and gate.passed
    state.notes["quality_gate"] = gate.status.value
    state.notes["quality_gate_reasons"] = sorted(set(reasons))
    state.notes["research_question_coverage"] = gate.coverage
    return ready


def _assert_delivery_ready(state: ProjectState) -> None:
    service = get_service(config.SOURCE_DATA_DIR)
    project_id = state.project_dir.name
    sources = service.list_sources(project_id, include_superseded=True)
    # 需求清单缺失先于证据检查阻断：不知道要回答什么问题时，"证据够不够"没有意义。
    try:
        requirements = _fixed_requirements(state)
    except ResearchPlanBlockedError as exc:
        state.notes["delivery_blocked_stage"] = "research_plan"
        state.save()
        state.mark_failure("研究需求清单", str(exc))
        raise
    gate = service.quality_gate(project_id, requirements)
    state.notes["research_question_coverage"] = gate.coverage
    if not gate.passed:
        materials_url = f"/materials?project={project_id}"
        state.notes["quality_gate"] = gate.status.value
        state.notes["quality_gate_reasons"] = gate.reasons
        state.notes["delivery_blocked_stage"] = "evidence"
        state.notes["delivery_materials_url"] = materials_url
        state.save()
        if not sources:
            recovery = (
                "项目尚未形成可验证来源。再次点击“继续”或运行 resume，系统会回到 Agent2↔Agent3，"
                "自动捕获公开网页并生成 EvidenceRecord；"
                f"如有私有材料，也可选地通过 `{materials_url}` 补充。"
            )
        else:
            recovery = (
                f"当前项目已有 {len(sources)} 份材料，但没有形成合格证据。"
                "再次点击“继续”或运行 resume，系统会回到 Agent2↔Agent3 重新验证。"
            )
        error = DeliveryBlockedError(
            "交付前证据门槛阻断："
            f"{gate.status.value}: {'; '.join(gate.reasons)}。"
            f"{recovery} Agent5 未启动，本错误不会重试。"
        )
        state.mark_failure("交付证据门槛", str(error))
        raise error
    state.notes["quality_gate"] = gate.status.value
    state.notes["quality_gate_reasons"] = gate.reasons
    state.notes.pop("delivery_blocked_stage", None)
    state.save()


def _validate_analysis_transition(
    state: ProjectState,
    analysis_path: Path,
) -> analyst.AnalysisOutcome:
    """Fail closed before Agent4 can transition to Agent5."""
    outcome_path = state.project_dir / config.FILE_ANALYSIS_OUTCOME
    try:
        outcome = analyst.load_analysis_outcome(state, outcome_path)
    except (analyst.AnalysisOutcomeError, ResearchPlanError) as exc:
        error = AnalysisBoundaryError(str(exc))
        state.notes["analysis_outcome_status"] = "invalid"
        state.mark_failure("Agent4·AnalysisOutcome 门禁", str(error))
        raise error from exc

    state.analysis_path = str(analysis_path)
    state.analysis_outcome_path = str(outcome_path)
    state.notes["analysis_outcome_status"] = outcome.status.value

    if outcome.status == analyst.AnalysisStatus.NEEDS_MORE_RESEARCH:
        gaps = [request.model_dump() for request in outcome.gap_requests]
        state.notes["analysis_gap_requests"] = gaps
        summary = "; ".join(
            f"{item['question_id']}: {item['reason']}（需要：{item['needed_evidence']}）"
            for item in gaps
        )
        error = AnalysisResearchRequiredError(
            f"Agent4 判定需要补研：{summary}。Agent5 未启动；"
            "请显式重试以回到 Agent2↔Agent3 采集验证阶段。"
        )
        state.mark_failure("Agent4·补研请求", str(error))
        raise error

    analysis_text = analysis_path.read_text(encoding="utf-8")
    service = get_service(config.SOURCE_DATA_DIR)
    citation_errors = audit_analysis_citations(
        analysis_text,
        state.project_dir.name,
        service.repository,
    )
    if citation_errors:
        state.notes["analysis_citation_audit_errors"] = citation_errors
        error = AnalysisBoundaryError(
            "Agent4 来源引用审计失败：" + "; ".join(citation_errors)
        )
        state.mark_failure("Agent4·来源引用审计", str(error))
        raise error

    state.notes.pop("analysis_gap_requests", None)
    state.notes.pop("analysis_citation_audit_errors", None)
    state.save()
    return outcome


def recover_blocked_delivery(state: ProjectState) -> bool:
    """Rewind an invalid legacy delivery state for web or uploaded evidence."""
    if state.stage not in {Stage.ANALYZING, Stage.FORMATTING}:
        return False
    if state.notes.get("research_plan_migration_required"):
        # 需求清单缺失不是"证据不够"，回退到采集验证只会再次撞上同一道门。
        return False
    if state.notes.get("delivery_blocked_stage") != "evidence" and state.notes.get("quality_gate") != "blocked":
        return False

    state.collect_round = 0
    state.converged = False
    state.last_feedback_path = None
    state.sources_final_path = None
    state.notes.pop("delivery_blocked_stage", None)
    state.notes["quality_gate"] = "revalidating"
    state.notes["quality_gate_reasons"] = []
    state.notes["delivery_recovery"] = "rewound_to_collecting_and_validating"
    state.advance_to(Stage.COLLECTING_AND_VALIDATING)
    return True


# ═════════════════════════════════════════════════════════════════
# 旧项目迁移：从现有提纲重建研究需求清单
# ═════════════════════════════════════════════════════════════════


def research_plan_migration_required(state: ProjectState) -> bool:
    return bool(state.notes.get("research_plan_migration_required"))


def migrate_research_plan(state: ProjectState) -> str:
    """显式迁移：从项目现有提纲重建 `research_requirements.json`。

    这是唯一的兼容路径——不使用空需求集合，也不按已有证据反推。用户必须显式触发
    （CLI `migrate-plan` 或工作台按钮），因为重建后的清单需要人工确认是否符合预期。
    """
    try:
        research_plan.load_plan(state)
    except ResearchPlanError:
        pass
    else:
        state.notes.pop("research_plan_migration_required", None)
        state.notes.pop("research_plan_error", None)
        state.save()
        raise ResearchPlanError(
            f"项目已经存在与当前提纲匹配的有效 {config.FILE_RESEARCH_REQUIREMENTS}，"
            "迁移仅用于清单缺失、损坏或与提纲不一致的旧项目；现有清单未被覆盖。"
        )

    plan, warning = research_plan.rebuild_plan(state)
    state.notes.pop("delivery_blocked_stage", None)
    state.notes["quality_gate"] = "revalidating"
    state.notes["quality_gate_reasons"] = []
    state.clear_failure()
    state.save()
    message = (
        f"已从现有提纲重建研究需求清单，共 {len(plan.requirements)} 个研究问题："
        f"{', '.join(plan.question_ids)}。"
        "请查阅 research_requirements.json 确认无误后继续推进；"
        "必答问题缺证据时仍会被质量门阻断。"
    )
    if warning:
        message += f" 注意：{warning}"
    return message


# ═════════════════════════════════════════════════════════════════
# 失败重试
# ═════════════════════════════════════════════════════════════════


def can_retry(state: ProjectState) -> bool:
    """项目是否处于可重试状态（有失败记录且未完成）。"""
    return state.stage != Stage.DONE and bool(state.failed_stage or state.last_error)


def retry_blocked_reason(state: ProjectState) -> str | None:
    """返回阻止重试的原因；None 表示可以重试。"""
    if state.stage == Stage.DONE:
        return "项目已完成，无需重试"
    if research_plan_migration_required(state):
        # 需求清单缺失时重试只会再次撞上同一道门；必须先显式迁移。
        return (
            "该项目缺少研究需求清单，重试无法解决。请先重建需求清单："
            "工作台点击“生成研究需求清单”，或运行 "
            f"`python -m research_agent migrate-plan {state.project_dir}`。"
        )
    if not can_retry(state):
        return "项目当前没有失败记录，可直接点击继续运行"
    return None


def prepare_retry(state: ProjectState, *, extra_rounds: int = RETRY_EXTRA_ROUNDS) -> str:
    """把失败的项目复位到可重跑的状态，返回本次重试的处理说明。

    重试保留全部既有产物（提纲、源清单、历史采集轮次、证据），只回退失败的阶段：
    - 采集验证阶段因证据门槛未通过而失败：追加轮次预算，从下一轮继续采集验证
    - 分析/排版阶段因交付证据门槛阻断：回退到采集验证阶段补证据
    - 其他阶段失败：原地重跑该阶段
    """
    failed_stage = state.failed_stage or state.stage.value
    state.retry_count += 1
    state.failed_stage = None
    state.last_error = None
    state.notes["last_retry_at"] = datetime.now().isoformat()
    state.notes["last_retry_stage"] = failed_stage

    if (
        state.stage == Stage.ANALYZING
        and state.notes.get("analysis_outcome_status") == "needs_more_research"
        and state.notes.get("analysis_gap_requests")
    ):
        budget = state.max_collect_rounds or config.MAX_COLLECT_ROUNDS
        if state.collect_round >= budget:
            state.max_collect_rounds = max(budget, state.collect_round) + max(
                1, extra_rounds
            )
        state.converged = False
        state.notes["analysis_outcome_status"] = "retrying_research"
        state.notes["quality_gate"] = "revalidating"
        state.notes["quality_gate_reasons"] = []
        state.advance_to(Stage.COLLECTING_AND_VALIDATING)
        return (
            "Agent4 请求补研，已保留现有材料、EvidenceRecord 和历史轮次，"
            f"将从第 {state.collect_round + 1} 轮继续采集验证"
        )

    if state.stage in {Stage.ANALYZING, Stage.FORMATTING, Stage.AWAIT_FINAL_SOURCE_APPROVAL} and (
        state.notes.get("delivery_blocked_stage") == "evidence"
        or state.notes.get("quality_gate") == "blocked"
    ):
        state.notes.pop("delivery_blocked_stage", None)
        state.notes["quality_gate"] = "revalidating"
        state.notes["quality_gate_reasons"] = []
        state.collect_round = 0
        state.converged = False
        state.last_feedback_path = None
        state.sources_final_path = None
        state.advance_to(Stage.COLLECTING_AND_VALIDATING)
        return "交付证据门槛未通过，已回退到采集验证阶段重新补充证据"

    if state.stage == Stage.COLLECTING_AND_VALIDATING and not state.converged:
        budget = state.max_collect_rounds or config.MAX_COLLECT_ROUNDS
        state.notes["quality_gate_reasons"] = []
        if state.collect_round >= budget:
            state.max_collect_rounds = max(budget, state.collect_round) + max(1, extra_rounds)
            state.save()
            hint = (
                f"审查未通过，已追加采集验证轮次预算至 {state.max_collect_rounds} 轮，"
                f"从第 {state.collect_round + 1} 轮继续"
            )
            if state.max_collect_rounds > RETRY_ROUND_SOFT_LIMIT:
                hint += (
                    f"（已超过 {RETRY_ROUND_SOFT_LIMIT} 轮建议值，"
                    "继续重试的模型开销较高，建议在材料中心补充权威材料）"
                )
            return hint
        state.save()
        return f"将从第 {state.collect_round + 1} 轮继续采集验证（预算 {budget} 轮）"

    state.save()
    return f"已复位「{failed_stage}」，将重跑该阶段"


def available_rerun_stages(state: ProjectState) -> tuple[Stage, ...]:
    """返回当前项目可以回退重跑的 Agent 阶段。"""
    progress = _STAGE_PROGRESS[state.stage]
    return tuple(
        stage for stage in RERUNNABLE_STAGES
        if _STAGE_PROGRESS[stage] <= progress
    )


def _require_rerun_input(state: ProjectState, stage: Stage) -> None:
    required: dict[Stage, tuple[tuple[str, str | None], ...]] = {
        Stage.SOURCING: (("调研提纲", state.outline_path),),
        Stage.COLLECTING_AND_VALIDATING: (
            ("调研提纲", state.outline_path),
            ("信息源草案", state.sources_draft_path),
        ),
        Stage.ANALYZING: (
            ("调研提纲", state.outline_path),
            ("最终源清单", state.sources_final_path),
        ),
        Stage.FORMATTING: (
            ("深度分析", state.analysis_path),
            ("最终源清单", state.sources_final_path),
        ),
    }
    for label, value in required.get(stage, ()):
        if not value or not Path(value).is_file():
            raise ValueError(f"无法从「{RERUN_STAGE_LABELS[stage]}」重跑：缺少{label}")


def _rerun_artifacts(state: ProjectState, stage: Stage) -> tuple[Path, ...]:
    project_dir = state.project_dir
    by_stage: dict[Stage, tuple[Path, ...]] = {
        Stage.PLANNING: (project_dir / config.FILE_OUTLINE,),
        Stage.SOURCING: (project_dir / config.FILE_SOURCES_DRAFT,),
        Stage.COLLECTING_AND_VALIDATING: (
            project_dir / config.FILE_RAW_DATA_DIR,
            project_dir / config.FILE_SOURCES_FINAL,
            project_dir / config.FILE_VALIDATION,
        ),
        Stage.ANALYZING: (project_dir / config.FILE_ANALYSIS,),
        Stage.FORMATTING: (
            project_dir / config.FILE_FINAL_REPORT,
            project_dir / config.FILE_CHART_MANIFEST,
            project_dir / config.FILE_FINAL_REPORT_HTML,
            project_dir / config.FILE_FINAL_REPORT_TEX,
            project_dir / config.FILE_FINAL_REPORT_PDF,
        ),
    }
    target_progress = _STAGE_PROGRESS[stage]
    return tuple(
        path
        for candidate in RERUNNABLE_STAGES
        if _STAGE_PROGRESS[candidate] >= target_progress
        for path in by_stage[candidate]
    )


def _archive_rerun_artifacts(state: ProjectState, stage: Stage) -> Path | None:
    existing = [path for path in _rerun_artifacts(state, stage) if path.exists()]
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = state.project_dir / "rerun_backups" / f"{stamp}_{stage.value}"
    backup_dir.mkdir(parents=True)
    for path in existing:
        shutil.move(str(path), str(backup_dir / path.name))
    return backup_dir


def prepare_stage_rerun(state: ProjectState, stage: Stage) -> str:
    """回退到指定 Agent 阶段，并归档该阶段及其下游旧产物。"""
    if stage not in RERUNNABLE_STAGES:
        raise ValueError(f"不支持回退到阶段：{stage.value}")
    if stage not in available_rerun_stages(state):
        raise ValueError(
            f"项目当前处于「{state.stage.value}」，不能前跳到「{stage.value}」"
        )
    _require_rerun_input(state, stage)
    backup_dir = _archive_rerun_artifacts(state, stage)

    target_progress = _STAGE_PROGRESS[stage]
    if target_progress <= _STAGE_PROGRESS[Stage.PLANNING]:
        state.outline_path = None
        state.clarification = []
        state.notes.pop("clarification_questions", None)
        state.notes.pop("outline_feedback", None)
    if target_progress <= _STAGE_PROGRESS[Stage.SOURCING]:
        state.sources_draft_path = None
        state.notes.pop("sources_feedback", None)
    if target_progress <= _STAGE_PROGRESS[Stage.COLLECTING_AND_VALIDATING]:
        state.sources_final_path = None
        state.validation_report_path = None
        state.collect_round = 0
        state.converged = False
        state.last_feedback_path = None
        state.notes.pop("quality_gate", None)
        state.notes.pop("quality_gate_reasons", None)
        state.notes.pop("delivery_blocked_stage", None)
        state.notes.pop("delivery_materials_url", None)
    if target_progress <= _STAGE_PROGRESS[Stage.ANALYZING]:
        state.analysis_path = None
    if target_progress <= _STAGE_PROGRESS[Stage.FORMATTING]:
        state.final_report_path = None
        state.chart_manifest_path = None
        state.final_report_html_path = None
        state.final_report_tex_path = None
        state.final_report_pdf_path = None
        state.final_report_typeset_pdf_path = None
        state.notes.pop("latex_typeset_error", None)

    state.failed_stage = None
    state.last_error = None
    state.notes["last_rerun_at"] = datetime.now().isoformat()
    state.notes["last_rerun_stage"] = stage.value
    state.notes["rerun_count"] = int(state.notes.get("rerun_count", 0)) + 1
    if backup_dir:
        state.notes["last_rerun_backup"] = str(backup_dir)
    else:
        state.notes.pop("last_rerun_backup", None)
    state.advance_to(stage)

    message = f"已回退到「{RERUN_STAGE_LABELS[stage]}」，将从该阶段重新运行"
    if backup_dir:
        message += "；旧产物已备份"
    return message


# ═════════════════════════════════════════════════════════════════
# 检查点规格 + 宿主协议（消除 CLI / Web 两份状态机）
# ═════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CheckpointSpec:
    """一个人工确认检查点的静态描述。"""

    stage: Stage
    key: str  # 产物 key，Web 前端用它定位待审产物
    title: str  # 简短标题，Web 用
    cli_title: str  # CLI 面板标题
    filename: str  # 待审文件名（相对项目目录）

    def file_path(self, state: ProjectState) -> Path:
        return state.project_dir / self.filename


CHECKPOINT_SPECS: tuple[CheckpointSpec, ...] = (
    CheckpointSpec(
        stage=Stage.AWAIT_OUTLINE_APPROVAL,
        key="outline",
        title="调研提纲",
        cli_title="《调研提纲》— 请查验",
        filename=config.FILE_OUTLINE,
    ),
    CheckpointSpec(
        stage=Stage.AWAIT_SOURCE_APPROVAL,
        key="sources_draft",
        title="信息源草案",
        cli_title="《信息源分层清单》— 请查验（S/A/B/D）",
        filename=config.FILE_SOURCES_DRAFT,
    ),
    CheckpointSpec(
        stage=Stage.AWAIT_FINAL_SOURCE_APPROVAL,
        key="sources_final",
        title="最终源清单",
        cli_title="《最终数据源清单》— 请查验",
        filename=config.FILE_SOURCES_FINAL,
    ),
)

_CHECKPOINTS_BY_STAGE: dict[Stage, CheckpointSpec] = {
    spec.stage: spec for spec in CHECKPOINT_SPECS
}


def checkpoint_for(stage: Stage) -> CheckpointSpec | None:
    """返回该阶段对应的检查点规格；非检查点阶段返回 None。"""
    return _CHECKPOINTS_BY_STAGE.get(stage)


class CheckpointDecision(str, Enum):
    """宿主对检查点的处置方式。"""

    APPROVED = "approved"  # 通过，进入下一阶段
    REJECTED = "rejected"  # 驳回，回退重做
    PAUSE = "pause"  # 挂起，交由宿主稍后推进（Web 走 HTTP 审批）


@dataclass(frozen=True)
class CheckpointResult:
    decision: CheckpointDecision
    feedback: str = ""


@dataclass(frozen=True)
class StrategistOutcome:
    """Agent1 本次执行的结果。

    要么产出提纲（`outline_path`），要么需要用户先回答澄清问题（`questions`）。
    CLI 在 `AgentSession` 内部完成多轮对话，因此总是直接返回提纲；Web 无法阻塞
    等待输入，于是把问题交回状态机，挂起到 `AWAIT_CLARIFICATION`。
    """

    outline_path: Path | None = None
    questions: tuple[str, ...] = ()

    @property
    def needs_clarification(self) -> bool:
        return self.outline_path is None and bool(self.questions)


def _as_strategist_outcome(value: Any) -> StrategistOutcome:
    """兼容直接返回 Path 的宿主实现。"""
    if isinstance(value, StrategistOutcome):
        return value
    return StrategistOutcome(outline_path=Path(value))


class PipelineHost(Protocol):
    """状态机运行宿主。CLI 与 Web 的全部行为差异都收敛到这里。"""

    async def run_strategist(
        self, state: ProjectState, feedback: str | None
    ) -> StrategistOutcome | Path:
        """执行 Agent1：返回提纲路径，或需要用户回答的澄清问题。"""
        ...

    async def resolve_clarification(
        self, state: ProjectState, questions: tuple[str, ...]
    ) -> list[str] | None:
        """收集澄清问题的回答；返回 None 表示挂起，等宿主稍后推进。"""
        ...

    async def resolve_checkpoint(
        self, state: ProjectState, spec: CheckpointSpec
    ) -> CheckpointResult:
        """处置一个人工确认检查点。"""
        ...

    def log(self, message: str) -> None:
        """记录一条阶段进展。"""
        ...

    def announce_done(self, state: ProjectState) -> None:
        """流水线完成时的收尾输出。"""
        ...


class CliPipelineHost:
    """CLI 宿主：Agent1 多轮对话，检查点阻塞等待用户输入。"""

    async def run_strategist(
        self, state: ProjectState, feedback: str | None
    ) -> StrategistOutcome:
        # CLI 的澄清对话在 AgentSession 内部通过 Prompt.ask 完成，
        # 因此这里总是直接拿到提纲，不会走 AWAIT_CLARIFICATION。
        path = await strategist.run_strategist(state, feedback=feedback)
        return StrategistOutcome(outline_path=path)

    async def resolve_clarification(
        self, state: ProjectState, questions: tuple[str, ...]
    ) -> list[str] | None:  # pragma: no cover - CLI 不会进入该状态
        return []

    async def resolve_checkpoint(
        self, state: ProjectState, spec: CheckpointSpec
    ) -> CheckpointResult:
        if spec.stage is Stage.AWAIT_FINAL_SOURCE_APPROVAL:
            console.print(
                "\n[bold cyan]──────────────────────────────────────[/bold cyan]\n"
                "[bold]最终数据源清单 + 验证报告[/bold]\n"
            )
            if state.validation_report_path:
                report = Path(state.validation_report_path)
                if report.exists():
                    console.print("[dim]（附：验证报告供参考）[/dim]")
                    checkpoints._render_file(report, "验证报告")

        approved, feedback = checkpoints.ask_approval(
            spec.file_path(state), title=spec.cli_title
        )
        return CheckpointResult(
            decision=CheckpointDecision.APPROVED
            if approved
            else CheckpointDecision.REJECTED,
            feedback=feedback,
        )

    def log(self, message: str) -> None:
        console.print(f"[dim]{message}[/dim]")

    def announce_done(self, state: ProjectState) -> None:
        console.print(
            f"\n[bold green]{'═' * 50}[/bold green]\n"
            f"[bold green]  ✓ 调研完成！[/bold green]\n"
            f"[bold green]{'═' * 50}[/bold green]\n\n"
            f"  最终报告：{state.final_report_path}\n"
            f"  项目目录：{state.project_dir}\n\n"
            f"  全部产物：\n"
            f"    - 调研提纲：{state.outline_path}\n"
            f"    - 信息源清单（草案）：{state.sources_draft_path}\n"
            f"    - 原始数据：{state.project_dir / config.FILE_RAW_DATA_DIR}/\n"
            f"    - 验证报告：{state.validation_report_path}\n"
            f"    - 最终源清单：{state.sources_final_path}\n"
            f"    - 深度分析：{state.analysis_path}\n"
            f"    - 最终报告：{state.final_report_path}\n"
        )


# ═════════════════════════════════════════════════════════════════
# 主流水线
# ═════════════════════════════════════════════════════════════════


async def run_pipeline(state: ProjectState, host: PipelineHost | None = None) -> None:
    """驱动整条调研流水线，按状态机推进到 DONE（CLI 入口）。

    任何阶段失败时，失败阶段与原因写入 state.json：
    - `resume` 从当前阶段断点重跑
    - `retry` 复位失败阶段后重跑（审查未通过时追加轮次预算）
    """

    try:
        await run_state_machine(state, host or CliPipelineHost())
    except PipelineError as e:
        # PipelineError 已在 _safe_run 里打印了详细信息，这里只确保失败被记录
        if not state.failed_stage:
            state.mark_failure(state.stage.value, str(e))
        else:
            state.save()
        raise
    except KeyboardInterrupt:
        state.save()
        console.print(
            f"\n[yellow]已中断。状态已保存，可用 resume 续跑。[/yellow]\n"
            f"[dim]{state.state_file}[/dim]"
        )
        raise
    except Exception as e:
        if not state.failed_stage:
            state.mark_failure(state.stage.value, str(e))
        else:
            state.save()
        console.print(
            f"\n[red]未预期的错误：{e}[/red]\n"
            f"[dim]{traceback.format_exc()}[/dim]\n"
            f"[yellow]状态已保存到：{state.state_file}[/yellow]\n"
            f"可用 `python -m research_agent retry {state.project_dir}` 重试当前阶段。"
        )
        raise


async def run_state_machine(state: ProjectState, host: PipelineHost) -> None:
    """唯一的状态机实现。CLI 与 Web 共用，差异由 host 注入。

    检查点的处置由 host 决定：CLI 阻塞询问用户，Web 返回 PAUSE 后退出，
    由 HTTP 审批接口推进阶段并重新调度。
    """

    while True:
        # 初次进入
        if state.stage == Stage.INIT:
            host.log("进入战略规划阶段")
            state.advance_to(Stage.PLANNING)

        # ================== 阶段 1: Agent1 战略规划 ==================
        if state.stage == Stage.PLANNING:
            host.log("Agent1 正在生成调研提纲")
            feedback = state.notes.pop("outline_feedback", None)
            outcome = _as_strategist_outcome(
                await _safe_run(
                    "Agent1·战略规划", state, host.run_strategist, state, feedback
                )
            )
            if outcome.needs_clarification:
                state.notes["clarification_questions"] = list(outcome.questions)
                state.advance_to(Stage.AWAIT_CLARIFICATION)
                host.log(f"Agent1 需要澄清 {len(outcome.questions)} 个问题")
            else:
                state.outline_path = str(outcome.outline_path)
                state.notes.pop("clarification_questions", None)
                # R1：提纲与需求清单在同一阶段固化，保证两者天然一致；
                # Agent1 重新生成提纲时这里会重新派生，不存在漂移窗口。
                plan, warning = research_plan.rebuild_plan(state)
                host.log(
                    f"已固定研究需求清单：{len(plan.requirements)} 个研究问题"
                    f"（{', '.join(plan.question_ids)}）"
                )
                if warning:
                    host.log(f"需求清单提示：{warning}")
                state.advance_to(Stage.AWAIT_OUTLINE_APPROVAL)
                host.log("调研提纲已生成，等待审批")

        # ---------- Agent1 澄清追问：等用户回答 ----------
        if state.stage == Stage.AWAIT_CLARIFICATION:
            questions = tuple(state.notes.get("clarification_questions", []))
            answers = await host.resolve_clarification(state, questions)
            if answers is None:
                return  # 宿主挂起，等 HTTP 接口提交回答后重新调度
            _append_clarification(state, questions, answers)
            state.advance_to(Stage.PLANNING)
            continue

        # ---------- 检查点 1: 调研提纲确认 ----------
        if state.stage == Stage.AWAIT_OUTLINE_APPROVAL:
            result = await _resolve_checkpoint(state, host)
            if result.decision is CheckpointDecision.PAUSE:
                return
            if result.decision is CheckpointDecision.APPROVED:
                state.advance_to(Stage.SOURCING)
            else:
                state.notes["outline_feedback"] = result.feedback
                state.advance_to(Stage.PLANNING)
                continue

        # ================== 阶段 2: Agent2 信息源分层 ==================
        if state.stage == Stage.SOURCING:
            host.log("Agent2 正在生成信息源草案")
            feedback = state.notes.pop("sources_feedback", None)
            sources_draft_path = await _safe_run(
                "Agent2·信息源分层", state,
                collector.run_source_tiering, state, feedback=feedback,
            )
            state.sources_draft_path = str(sources_draft_path)
            state.advance_to(Stage.AWAIT_SOURCE_APPROVAL)
            host.log("信息源草案已生成，等待审批")

        # ---------- 检查点 2: 信息源分层清单确认 ----------
        if state.stage == Stage.AWAIT_SOURCE_APPROVAL:
            result = await _resolve_checkpoint(state, host)
            if result.decision is CheckpointDecision.PAUSE:
                return
            if result.decision is CheckpointDecision.APPROVED:
                state.advance_to(Stage.COLLECTING_AND_VALIDATING)
            else:
                state.notes["sources_feedback"] = result.feedback
                state.advance_to(Stage.SOURCING)
                continue

        # ================== 阶段 3: Agent2↔3 迭代循环 ==================
        if state.stage == Stage.COLLECTING_AND_VALIDATING:
            await _run_collect_validate_loop(state, host)
            state.advance_to(Stage.AWAIT_FINAL_SOURCE_APPROVAL)
            host.log("最终源清单已生成，等待审批")

        # ---------- 检查点 3: 最终源清单确认 ----------
        if state.stage == Stage.AWAIT_FINAL_SOURCE_APPROVAL:
            result = await _resolve_checkpoint(state, host)
            if result.decision is CheckpointDecision.PAUSE:
                return
            if result.decision is CheckpointDecision.APPROVED:
                _assert_delivery_ready(state)
                state.advance_to(Stage.ANALYZING)
            else:
                state.notes["sources_feedback"] = result.feedback
                state.collect_round = 0
                state.converged = False
                state.last_feedback_path = None
                state.advance_to(Stage.COLLECTING_AND_VALIDATING)
                continue

        # ================== 阶段 4: Agent4 深度分析 ==================
        if state.stage == Stage.ANALYZING:
            host.log("Agent4 正在生成深度分析")
            analysis_path = await _safe_run(
                "Agent4·深度分析", state, analyst.run_analysis, state,
            )
            _validate_analysis_transition(state, analysis_path)
            state.advance_to(Stage.FORMATTING)

        # ================== 阶段 5: Agent5 排版交付 ==================
        if state.stage == Stage.FORMATTING:
            host.log("正在检查交付证据门槛")
            _assert_delivery_ready(state)
            host.log("Agent5 正在排版最终报告")
            final_report_path = await _safe_run(
                "Agent5·排版交付", state, formatter.run_formatting, state,
            )
            state.final_report_path = str(final_report_path)
            state.advance_to(Stage.DONE)

        # ================== 完成 ==================
        if state.stage == Stage.DONE:
            state.clear_failure()
            host.log("调研完成")
            host.announce_done(state)
            return


def _append_clarification(
    state: ProjectState, questions: tuple[str, ...], answers: list[str]
) -> None:
    """把一轮问答写入 state.clarification，供 Agent1 下次执行时读取。"""
    for index, question in enumerate(questions):
        answer = answers[index].strip() if index < len(answers) else ""
        state.clarification.append(
            {"question": question, "answer": answer or "（用户未回答，请用合理默认值）"}
        )
    state.notes.pop("clarification_questions", None)
    state.save()


async def _resolve_checkpoint(
    state: ProjectState, host: PipelineHost
) -> CheckpointResult:
    """把当前检查点交给宿主处置。"""
    spec = checkpoint_for(state.stage)
    if spec is None:  # pragma: no cover - 调用点已保证是检查点阶段
        raise PipelineError(f"阶段 {state.stage.value} 不是检查点")
    host.log(f"等待{spec.title}审批")
    return await host.resolve_checkpoint(state, spec)


async def _run_collect_validate_loop(state: ProjectState, host: PipelineHost) -> None:
    """Agent2↔Agent3 采集-验证循环，收敛后写出终稿源清单。"""
    max_rounds = state.max_collect_rounds or config.MAX_COLLECT_ROUNDS
    host.log(f"进入采集验证循环：{state.collect_round}/{max_rounds}")

    while state.collect_round < max_rounds and not state.converged:
        round_idx = state.collect_round + 1

        # —— Agent2: 本轮采集 ——
        feedback_path = (
            Path(state.last_feedback_path) if state.last_feedback_path else None
        )
        host.log(f"Agent2 正在执行第 {round_idx} 轮采集")
        raw_round_path = await _safe_run(
            f"Agent2·采集第{round_idx}轮", state,
            collector.run_collection_round,
            state, round_idx, feedback_path=feedback_path,
        )

        # —— Agent3: 本轮验证 ——
        host.log(f"Agent3 正在验证第 {round_idx} 轮数据")
        fb_path, fb_obj = await _safe_run(
            f"Agent3·验证第{round_idx}轮", state,
            validator.run_validation,
            state, round_idx, raw_round_path,
        )

        # 更新状态
        state.collect_round = round_idx
        state.last_feedback_path = str(fb_path)
        state.converged = _deterministic_convergence(state, fb_obj)
        state.save()

        host.log(
            f"第 {round_idx} 轮完成，"
            f"模型收敛={fb_obj.converged}，证据收敛={state.converged}"
        )

        if state.converged:
            host.log("确定性证据门槛判定数据已收敛")
            break

    if not state.converged:
        raise _quality_gate_error(state, max_rounds)

    # 读取最后一轮反馈生成终稿源清单
    final_fb = validator.load_feedback(Path(state.last_feedback_path))
    sources_final_path = await validator.finalize_sources(state, final_fb)
    state.sources_final_path = str(sources_final_path)
    state.validation_report_path = str(state.project_dir / config.FILE_VALIDATION)
