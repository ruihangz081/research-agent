"""Agent3 · 信息验证。

对 Agent2 本轮的原始数据做交叉验证，淘汰低质源，产出结构化反馈。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError
from rich.console import Console

from .. import config
from ..agent_loop import AgentOptions, run_agent
from ..llm import LLMClient
from ..research_plan import plan_prompt_context
from ..sources.models import ResearchTaskUpdate
from ..sources.tasks import (
    apply_feedback_tasks,
    config_tasks_path,
    load_task_execution_report,
    load_tasks_file,
    task_results_path,
)
from ..tools import default_registry
from .source_context import source_context

if TYPE_CHECKING:
    from ..state import ProjectState

console = Console()

_PROMPT_VALIDATOR = Path(__file__).parent / "prompts" / "validator.md"


def _load_validator_prompt() -> str:
    return _PROMPT_VALIDATOR.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# 反馈 Schema
# ═══════════════════════════════════════════════════════════════


class ConflictItem(BaseModel):
    topic: str
    values: list[dict]
    resolution: str | None = None


class ValidationFeedback(BaseModel):
    """Agent3 → Agent2 的结构化反馈。"""
    round: int
    converged: bool
    summary: str = ""
    drop_sources: list[str] = Field(default_factory=list)
    retain_sources: list[str] = Field(default_factory=list)
    gap_list: list[str] = Field(default_factory=list)
    need_rework_topics: list[str] = Field(default_factory=list)
    conflicts: list[ConflictItem] = Field(default_factory=list)
    next_round_focus: list[str] = Field(default_factory=list)
    tasks: list[ResearchTaskUpdate] = Field(default_factory=list)


def load_feedback(path: Path) -> ValidationFeedback:
    """从磁盘读取并校验反馈 JSON。

    Agent 输出偶尔会把纯分析任务的独立来源数写成 0。任务台账不允许绕过
    证据门槛，因此在模型输出边界保守提升为 1；严格的持久化模型仍保持
    ``ge=1``，其他结构错误继续 fail-closed。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    normalized = []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if isinstance(tasks, list):
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            value = task.get("required_independent_sources")
            if isinstance(value, bool):
                continue
            try:
                source_count = int(value)
            except (TypeError, ValueError):
                continue
            if source_count < 1:
                task["required_independent_sources"] = 1
                normalized.append(index)

    feedback = ValidationFeedback(**data)
    if normalized:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        locations = ", ".join(f"tasks.{index}" for index in normalized)
        console.print(
            "[yellow]反馈任务的 required_independent_sources 小于 1，"
            f"已按最低证据门槛纠正为 1：{locations}[/yellow]"
        )
    return feedback


def _previous_tasks_context(previous_tasks, round_idx: int) -> str:
    """把上一轮的任务台账注入 prompt，供 Agent3 验收并重写。

    上一轮没有任务文件时返回空串（首轮或旧项目）。有历史任务时，Agent3 必须
    逐条验收：已补齐的标 completed（回填 evidence_id），未补齐的保持 pending
    或标 blocked，然后连同新任务一起输出为完整清单。
    """
    if not previous_tasks.tasks:
        return ""
    lines = [
        "\n\n## 上一轮的结构化补研任务（必须验收并更新状态）",
        f"这是第 {round_idx - 1} 轮留下的任务台账，请逐条验收：",
        "",
        "| task_id | question_id | 优先级 | 描述 | 状态 |",
        "|---|---|---|---|---|",
    ]
    for item in previous_tasks.tasks:
        lines.append(
            f"| `{item.task_id}` | `{item.question_id}` | {item.priority} "
            f"| {item.description} | {item.status} |"
        )
    lines += [
        "",
        "验收规则：",
        "- 本轮已补齐该任务要求的证据 → 状态改 `completed`，并在 `completed_evidence_ids` 回填证据 ID",
        "- 本轮仍无法补齐 → 保持 `pending`（继续下轮）或改 `blocked` 并在 `blocked_reason` 写明原因",
        "- 不得自行改为 `waived`；豁免必须由显式人工操作写入台账",
        "- 需要变更状态的历史任务必须输出；未输出的任务由程序原样保留。",
    ]
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════
# 执行器
# ═══════════════════════════════════════════════════════════════


async def run_validation(
    state: "ProjectState",
    round_idx: int,
    raw_round_path: Path,
) -> tuple[Path, ValidationFeedback]:
    """对本轮 raw data 做验证。"""
    if not state.outline_path:
        raise RuntimeError("outline 未就绪")
    if not state.sources_draft_path:
        raise RuntimeError("源清单未就绪")
    if not raw_round_path.exists():
        raise FileNotFoundError(f"本轮 raw data 不存在：{raw_round_path}")

    raw_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    feedback_path = raw_dir / config.FILE_FEEDBACK_ROUND.format(n=round_idx)
    validation_report_path = state.project_dir / config.FILE_VALIDATION
    task_execution_path = task_results_path(state, round_idx)
    load_task_execution_report(state, round_idx, task_execution_path)

    sources_list_path = (
        Path(state.sources_final_path)
        if state.sources_final_path
        else Path(state.sources_draft_path)
    )

    previous_rounds = [
        raw_dir / config.FILE_RAW_ROUND.format(n=index)
        for index in range(1, round_idx)
        if (raw_dir / config.FILE_RAW_ROUND.format(n=index)).exists()
    ]
    previous_rounds_str = (
        "\n".join(f"  - {p}" for p in previous_rounds if p != raw_round_path)
        or "  （首轮，无其他历史）"
    )
    previous_feedback_path = (
        raw_dir / config.FILE_FEEDBACK_ROUND.format(n=round_idx - 1)
        if round_idx > 1
        else None
    )
    previous_feedback_str = (
        str(previous_feedback_path)
        if previous_feedback_path and previous_feedback_path.exists()
        else "（首轮，无历史反馈）"
    )

    tasks_path = config_tasks_path(state.project_dir)
    previous_tasks = load_tasks_file(tasks_path)

    system_prompt = _load_validator_prompt()
    system_prompt += plan_prompt_context(state)
    system_prompt += source_context(state)
    replacements = {
        "{outline_path}": str(Path(state.outline_path)),
        "{sources_list_path}": str(sources_list_path),
        "{current_round_raw}": str(raw_round_path),
        "{previous_rounds}": previous_rounds_str,
        "{previous_feedback}": previous_feedback_str,
        "{feedback_path}": str(feedback_path),
        "{task_results_path}": str(task_execution_path),
        "{validation_report_path}": str(validation_report_path),
        "{tasks_path}": str(tasks_path),
        "{N}": str(round_idx),
    }
    for k, v in replacements.items():
        system_prompt = system_prompt.replace(k, v)
    system_prompt += _previous_tasks_context(previous_tasks, round_idx)

    system_prompt += (
        f"\n\n## 当前项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 本轮序号：第 {round_idx} 轮\n"
        f"- 最大循环轮次：{state.max_collect_rounds}\n"
        f"- 反馈 JSON 写入：`{feedback_path}`\n"
        f"- 验证报告写入（累加）：`{validation_report_path}`\n"
        f"- 本轮 raw：`{raw_round_path}`\n"
        f"- 研究需求清单：`{state.project_dir / config.FILE_RESEARCH_REQUIREMENTS}`\n"
    )

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write", "SearchProjectSources", "ListProjectSourceChunks", "ReadProjectSource", "RecordProjectEvidence", "InspectSourceEvidence"],
        cwd=str(state.project_dir),
        max_turns=30,
    )

    console.print(
        f"\n[bold magenta]═══ Agent3 · 验证第 {round_idx} 轮 ═══[/bold magenta]\n"
        f"[dim]校验对象：{raw_round_path.name}[/dim]\n"
    )

    async def _on_text(text: str) -> None:
        console.print(text, style="bright_yellow", end="")

    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        await run_agent(
            user_prompt=(
                f"请对第 {round_idx} 轮采集（`{raw_round_path}`）做交叉验证。"
                f"输出反馈 JSON 到 `{feedback_path}`，追加验证报告到 `{validation_report_path}`。"
            ),
            options=options,
            llm_client=client,
            tool_registry=default_registry,
            on_assistant_text=_on_text,
        )
    console.print()

    # 校验产物
    if not feedback_path.exists():
        raise RuntimeError(f"Agent3 未生成反馈 JSON：{feedback_path}")
    try:
        feedback = load_feedback(feedback_path)
    except (json.JSONDecodeError, ValidationError) as e:
        console.print(
            f"[red]反馈 JSON 不合法：{e}\n"
            f"文件内容：\n{feedback_path.read_text(encoding='utf-8')[:500]}[/red]"
        )
        raise

    if feedback.round != round_idx:
        feedback.round = round_idx

    apply_feedback_tasks(state, feedback)

    console.print(
        f"\n[green]✓ 第 {round_idx} 轮验证完成 | converged="
        f"{'[bold]true[/bold]' if feedback.converged else 'false'} | "
        f"drop={len(feedback.drop_sources)}, gap={len(feedback.gap_list)}, "
        f"conflicts={len(feedback.conflicts)}, tasks={len(feedback.tasks)}[/green]"
    )
    return feedback_path, feedback


# ═══════════════════════════════════════════════════════════════
# 生成最终源清单
# ═══════════════════════════════════════════════════════════════


async def finalize_sources(
    state: "ProjectState",
    final_feedback: ValidationFeedback,
) -> Path:
    """基于最后一轮反馈的 retain_sources，生成 sources_final.md。"""
    draft_path = Path(state.sources_draft_path)
    draft_text = draft_path.read_text(encoding="utf-8")
    final_path = state.project_dir / config.FILE_SOURCES_FINAL

    retained = set(final_feedback.retain_sources)
    dropped_cumulative: set[str] = set()
    raw_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    feedback_files = [
        raw_dir / config.FILE_FEEDBACK_ROUND.format(n=index)
        for index in range(1, state.collect_round + 1)
    ]
    for fb_file in feedback_files:
        try:
            fb = load_feedback(fb_file)
            dropped_cumulative.update(fb.drop_sources)
        except Exception:
            continue

    lines = [
        f"# 《{state.topic}》最终数据源清单",
        "",
        "> 由 Agent3 验证后自动生成。",
        "",
        f"- 总轮次：{state.collect_round}",
        f"- 收敛判定：{'✓ 已收敛' if final_feedback.converged else '✗ 达到轮次上限'}",
        f"- 保留源数量：{len(retained)}",
        f"- 累计淘汰源：{len(dropped_cumulative)}",
        "",
        "## 摘要",
        final_feedback.summary or "（无）",
        "",
        "## 保留的信息源（按 ID 排序）",
    ]
    for sid in sorted(retained):
        lines.append(f"- `{sid}`")
    lines += ["", "## 已淘汰的信息源"]
    for sid in sorted(dropped_cumulative):
        lines.append(f"- `{sid}`")
    lines += ["", "## 已知遗留 gap"]
    if final_feedback.gap_list:
        for g in final_feedback.gap_list:
            lines.append(f"- {g}")
    else:
        lines.append("- （无）")
    lines += ["", "## 冲突数据处理记录"]
    if final_feedback.conflicts:
        for c in final_feedback.conflicts:
            lines.append(f"- **{c.topic}**：{c.resolution or '（未标注）'}")
            for v in c.values:
                lines.append(f"  - `{v.get('src')}`：{v.get('value')} {v.get('note', '')}")
    else:
        lines.append("- （无冲突）")
    lines += ["", "---", "", "## 附：源清单草稿全文", "", draft_text]

    final_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]✓ 最终源清单已生成：{final_path.name}[/green]")
    return final_path
