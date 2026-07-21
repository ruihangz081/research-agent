"""Orchestrator 主控：状态机 + 阶段调度 + 检查点拦截 + 异常兜底。

完整流水线：
  Agent1(提纲) → 检查点1
  Agent2(源分层) → 检查点2
  Agent2↔3(采集-验证循环) → 检查点3
  Agent4(深度分析)
  Agent5(排版交付) → DONE

异常策略：
  - Agent 执行失败时，自动保存当前状态到 state.json（不丢数据）
  - 用户可通过 `resume` 从断点重跑
  - 每个阶段最多重试 MAX_RETRIES 次（默认 2）
"""
from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.console import Console

from . import checkpoints, config
from .agent_loop import AgentLoopStuckError
from .agents import analyst, collector, formatter, strategist, validator
from .sources.api import build_runtime
from .sources.enums import VerificationStatus
from .sources.quality import ResearchRequirement
from .state import ProjectState, Stage

console = Console()

MAX_RETRIES: int = 2


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
    - 失败且重试耗尽：保存状态后 raise PipelineError
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await fn(*args, **kwargs)
        except KeyboardInterrupt:
            # 用户中断，直接保存状态抛出
            state.save()
            raise
        except AgentLoopStuckError as e:
            state.save()
            raise PipelineError(
                f"{stage_name} 因重复工具错误提前停止：{e}。"
                "请修正工具参数后从当前项目断点续跑。"
            ) from e
        except PipelineError:
            # Deterministic gates are not transient agent failures. Retrying would
            # waste model calls and mislabel the blocked stage as an agent crash.
            state.save()
            raise
        except Exception as e:
            last_err = e
            state.save()  # 每次失败都保存状态，确保不丢数据
            if attempt < MAX_RETRIES:
                console.print(
                    f"\n[yellow]⚠ {stage_name} 第 {attempt} 次执行失败，"
                    f"正在重试（{attempt}/{MAX_RETRIES}）...[/yellow]\n"
                    f"[dim]错误：{e}[/dim]\n"
                )
            else:
                console.print(
                    f"\n[red]✗ {stage_name} 在 {MAX_RETRIES} 次尝试后仍失败[/red]\n"
                    f"[red]错误：{e}[/red]\n"
                    f"[dim]{traceback.format_exc()}[/dim]"
                )

    # 所有重试都失败
    raise PipelineError(
        f"{stage_name} 执行失败（已重试 {MAX_RETRIES} 次）。\n"
        f"状态已保存到：{state.state_file}\n"
        f"可用 `python -m research_agent resume {state.project_dir}` 从断点重跑。\n"
        f"原始错误：{last_err}"
    ) from last_err


class PipelineError(RuntimeError):
    """流水线阶段不可恢复的错误。"""

    pass


class DeliveryBlockedError(PipelineError):
    """Delivery is paused until deterministic evidence requirements are met."""

    pass


def _deterministic_convergence(state: ProjectState, feedback: validator.ValidationFeedback) -> bool:
    """A model convergence claim is advisory; persisted evidence decides readiness."""
    service, _ = build_runtime(config.SOURCE_DATA_DIR)
    project_id = state.project_dir.name
    evidence = service.repository.list_evidence(project_id)
    supported_questions = sorted({
        item.research_question_id
        for item in evidence
        if item.verification_status == VerificationStatus.SUPPORTED
    })
    requirements = [ResearchRequirement(question_id=value) for value in supported_questions]
    gate = service.quality_gate(project_id, requirements)
    service.repository.close()
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
    return ready


def _assert_delivery_ready(state: ProjectState) -> None:
    service, _ = build_runtime(config.SOURCE_DATA_DIR)
    project_id = state.project_dir.name
    try:
        sources = service.list_sources(project_id, include_superseded=True)
        evidence = service.repository.list_evidence(project_id)
        questions = sorted({item.research_question_id for item in evidence if item.verification_status == VerificationStatus.SUPPORTED})
        gate = service.quality_gate(project_id, [ResearchRequirement(question_id=value) for value in questions])
    finally:
        service.repository.close()
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
        raise DeliveryBlockedError(
            "交付前证据门槛阻断："
            f"{gate.status.value}: {'; '.join(gate.reasons)}。"
            f"{recovery} Agent5 未启动，本错误不会重试。"
        )


def recover_blocked_delivery(state: ProjectState) -> bool:
    """Rewind an invalid legacy delivery state for web or uploaded evidence."""
    if state.stage not in {Stage.ANALYZING, Stage.FORMATTING}:
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
# 主流水线
# ═════════════════════════════════════════════════════════════════


async def run_pipeline(state: ProjectState) -> None:
    """驱动整条调研流水线，按状态机推进到 DONE。

    任何阶段失败时，状态已自动保存到 state.json，
    可通过 `resume` 命令从当前阶段断点重跑。
    """

    try:
        await _run_pipeline_inner(state)
    except PipelineError:
        # PipelineError 已在 _safe_run 里打印了详细信息，这里只确保状态保存
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
        state.save()
        console.print(
            f"\n[red]未预期的错误：{e}[/red]\n"
            f"[dim]{traceback.format_exc()}[/dim]\n"
            f"[yellow]状态已保存到：{state.state_file}[/yellow]\n"
            f"可用 `python -m research_agent resume {state.project_dir}` 续跑。"
        )
        raise


async def _run_pipeline_inner(state: ProjectState) -> None:
    """内部实现，不做顶层异常捕获。"""

    # 初次进入
    if state.stage == Stage.INIT:
        state.advance_to(Stage.PLANNING)

    # ================== 阶段 1: Agent1 战略规划 ==================
    if state.stage == Stage.PLANNING:
        feedback = state.notes.pop("outline_feedback", None)
        outline_path = await _safe_run(
            "Agent1·战略规划", state,
            strategist.run_strategist, state, feedback=feedback,
        )
        state.outline_path = str(outline_path)
        state.advance_to(Stage.AWAIT_OUTLINE_APPROVAL)

    # ---------- 检查点 1: 调研提纲确认 ----------
    if state.stage == Stage.AWAIT_OUTLINE_APPROVAL:
        approved, feedback = checkpoints.ask_approval(
            state.project_dir / config.FILE_OUTLINE,
            title="《调研提纲》— 请查验",
        )
        if approved:
            state.advance_to(Stage.SOURCING)
        else:
            state.notes["outline_feedback"] = feedback
            state.advance_to(Stage.PLANNING)
            return await _run_pipeline_inner(state)

    # ================== 阶段 2: Agent2 信息源分层 ==================
    if state.stage == Stage.SOURCING:
        feedback = state.notes.pop("sources_feedback", None)
        sources_draft_path = await _safe_run(
            "Agent2·信息源分层", state,
            collector.run_source_tiering, state, feedback=feedback,
        )
        state.sources_draft_path = str(sources_draft_path)
        state.advance_to(Stage.AWAIT_SOURCE_APPROVAL)

    # ---------- 检查点 2: 信息源分层清单确认 ----------
    if state.stage == Stage.AWAIT_SOURCE_APPROVAL:
        approved, feedback = checkpoints.ask_approval(
            state.project_dir / config.FILE_SOURCES_DRAFT,
            title="《信息源分层清单》— 请查验（S/A/B/D）",
        )
        if approved:
            state.advance_to(Stage.COLLECTING_AND_VALIDATING)
        else:
            state.notes["sources_feedback"] = feedback
            state.advance_to(Stage.SOURCING)
            return await _run_pipeline_inner(state)

    # ================== 阶段 3: Agent2↔3 迭代循环 ==================
    if state.stage == Stage.COLLECTING_AND_VALIDATING:
        max_rounds = state.max_collect_rounds or config.MAX_COLLECT_ROUNDS

        console.print(
            f"\n[bold magenta]═══ 采集-验证循环 ═══[/bold magenta]\n"
            f"[dim]已完成轮次：{state.collect_round} / 上限：{max_rounds}[/dim]\n"
        )

        while state.collect_round < max_rounds and not state.converged:
            round_idx = state.collect_round + 1

            # —— Agent2: 本轮采集 ——
            feedback_path = (
                Path(state.last_feedback_path)
                if state.last_feedback_path
                else None
            )
            raw_round_path = await _safe_run(
                f"Agent2·采集第{round_idx}轮", state,
                collector.run_collection_round,
                state, round_idx, feedback_path=feedback_path,
            )

            # —— Agent3: 本轮验证 ——
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

            console.print(
                f"\n[dim]── 第 {round_idx} 轮结束 | model_converged={fb_obj.converged} "
                f"| deterministic_converged={state.converged} "
                f"| drop={len(fb_obj.drop_sources)} gap={len(fb_obj.gap_list)} ──[/dim]\n"
            )

            if state.converged:
                console.print("[green]✓ 确定性证据门槛判定数据已收敛[/green]")
                break

        if not state.converged:
            raise PipelineError(
                f"达到最大轮次 {max_rounds}，但证据质量门槛未通过："
                f"{'; '.join(state.notes.get('quality_gate_reasons', []))}"
            )

        # 读取最后一轮反馈生成终稿源清单
        final_fb_path = Path(state.last_feedback_path)
        final_fb = validator.load_feedback(final_fb_path)
        sources_final_path = await validator.finalize_sources(state, final_fb)
        state.sources_final_path = str(sources_final_path)
        state.validation_report_path = str(
            state.project_dir / config.FILE_VALIDATION
        )
        state.advance_to(Stage.AWAIT_FINAL_SOURCE_APPROVAL)

    # ---------- 检查点 3: 最终源清单确认 ----------
    if state.stage == Stage.AWAIT_FINAL_SOURCE_APPROVAL:
        console.print(
            "\n[bold cyan]──────────────────────────────────────[/bold cyan]\n"
            "[bold]最终数据源清单 + 验证报告[/bold]\n"
        )
        if state.validation_report_path:
            vr = Path(state.validation_report_path)
            if vr.exists():
                console.print("[dim]（附：验证报告供参考）[/dim]")
                checkpoints._render_file(vr, "验证报告")

        approved, feedback = checkpoints.ask_approval(
            state.project_dir / config.FILE_SOURCES_FINAL,
            title="《最终数据源清单》— 请查验",
        )
        if approved:
            _assert_delivery_ready(state)
            state.advance_to(Stage.ANALYZING)
        else:
            state.notes["sources_feedback"] = feedback
            state.collect_round = 0
            state.converged = False
            state.last_feedback_path = None
            state.advance_to(Stage.COLLECTING_AND_VALIDATING)
            return await _run_pipeline_inner(state)

    # ================== 阶段 4: Agent4 深度分析 ==================
    if state.stage == Stage.ANALYZING:
        analysis_path = await _safe_run(
            "Agent4·深度分析", state,
            analyst.run_analysis, state,
        )
        state.analysis_path = str(analysis_path)
        state.advance_to(Stage.FORMATTING)

    # ================== 阶段 5: Agent5 排版交付 ==================
    if state.stage == Stage.FORMATTING:
        _assert_delivery_ready(state)
        final_report_path = await _safe_run(
            "Agent5·排版交付", state,
            formatter.run_formatting, state,
        )
        state.final_report_path = str(final_report_path)
        state.advance_to(Stage.DONE)

    # ================== 完成 ==================
    if state.stage == Stage.DONE:
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
