"""Agent2 · 数据搜集。

两阶段：
- 阶段 2-A  `run_source_tiering()`：识别候选源 → 分层 → 写 sources_draft.md
- 阶段 2-B  `run_collection_round()`：按用户确认的源采集数据，接收 Agent3 反馈迭代
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from .. import config
from ..agent_loop import AgentOptions, run_agent
from ..llm import LLMClient
from ..research_plan import plan_prompt_context
from ..sources.tasks import load_tasks_file, config_tasks_path, tasks_prompt_context
from ..tools import default_registry
from .source_context import source_context

if TYPE_CHECKING:
    from ..state import ProjectState

console = Console()

_PROMPT_TIERING = Path(__file__).parent / "prompts" / "collector.md"
_PROMPT_ROUND = Path(__file__).parent / "prompts" / "collector_round.md"


def _load_tiering_prompt() -> str:
    return _PROMPT_TIERING.read_text(encoding="utf-8")


def _load_round_prompt() -> str:
    return _PROMPT_ROUND.read_text(encoding="utf-8")


async def _run_and_print(prompt: str, options: AgentOptions, style: str) -> None:
    async def _on_text(text: str) -> None:
        console.print(text, style=style, end="")

    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=options.model or config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        await run_agent(
            user_prompt=prompt,
            options=options,
            llm_client=client,
            tool_registry=default_registry,
            on_assistant_text=_on_text,
        )
    console.print()


# ═══════════════════════════════════════════════════════════════
# 阶段 2-A：信息源识别与分层
# ═══════════════════════════════════════════════════════════════


async def run_source_tiering(
    state: "ProjectState", feedback: str | None = None
) -> Path:
    """识别候选信息源，分层后写入 sources_draft.md。"""
    if not state.outline_path:
        raise RuntimeError("Agent2 需要先运行 Agent1 生成 outline.md")

    outline_path = Path(state.outline_path)
    if not outline_path.exists():
        raise FileNotFoundError(f"outline 文件不存在：{outline_path}")

    sources_draft_path = state.project_dir / config.FILE_SOURCES_DRAFT

    system_prompt = _load_tiering_prompt()
    system_prompt += plan_prompt_context(state)
    system_prompt += source_context(state)
    system_prompt = system_prompt.replace("{outline_path}", str(outline_path))
    system_prompt = system_prompt.replace("{sources_draft_path}", str(sources_draft_path))
    system_prompt += (
        f"\n\n## 当前项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 提纲路径（请 Read 读取）：`{outline_path}`\n"
        f"- 源清单写入路径：`{sources_draft_path}`\n"
    )
    if feedback:
        system_prompt += (
            f"\n## 重要：用户驳回了上一版源清单\n"
            f"修改意见：{feedback}\n"
            f"请针对该意见重新梳理信息源，再次写入同一路径。\n"
        )

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write", "WebSearch", "ListProjectSources", "SearchProjectSources", "ReadProjectSource"],
        cwd=str(state.project_dir),
        max_turns=25,
    )

    console.print(
        f"\n[bold magenta]═══ Agent2 · 数据搜集（阶段 2-A：信息源分层）═══[/bold magenta]\n"
        f"[dim]主题：{state.topic}[/dim]\n"
        f"[dim]基于：{outline_path.name}[/dim]\n"
    )

    user_prompt = (
        f"请基于《调研提纲》完成信息源识别与 S/A/B/D 分层，"
        f"并将结果写入 `{sources_draft_path}`。"
    )
    if feedback:
        user_prompt += f"\n\n【用户对上一版的意见】{feedback}"

    await _run_and_print(user_prompt, options, "bright_green")

    if not sources_draft_path.exists():
        raise RuntimeError("Agent2 未能生成信息源清单文件。")

    console.print(f"\n[green]✓ 信息源清单已生成：{sources_draft_path.name}[/green]")
    return sources_draft_path


# ═══════════════════════════════════════════════════════════════
# 阶段 2-B：按级采集
# ═══════════════════════════════════════════════════════════════


async def run_collection_round(
    state: "ProjectState",
    round_idx: int,
    feedback_path: Path | None = None,
) -> Path:
    """执行一轮数据采集。"""
    if not state.outline_path:
        raise RuntimeError("需要先运行 Agent1 生成 outline.md")
    if not state.sources_draft_path:
        raise RuntimeError("需要先运行 Agent2 阶段 2-A 生成源清单")

    outline_path = Path(state.outline_path)
    sources_list_path = (
        Path(state.sources_final_path)
        if state.sources_final_path
        else Path(state.sources_draft_path)
    )

    raw_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    round_output = raw_dir / config.FILE_RAW_ROUND.format(n=round_idx)

    previous_rounds = [
        raw_dir / config.FILE_RAW_ROUND.format(n=index)
        for index in range(1, round_idx)
        if (raw_dir / config.FILE_RAW_ROUND.format(n=index)).exists()
    ]
    previous_rounds_str = "\n".join(f"  - {p}" for p in previous_rounds) or "  （首轮，无历史）"

    system_prompt = _load_round_prompt()
    replacements = {
        "{outline_path}": str(outline_path),
        "{sources_final_list}": str(sources_list_path),
        "{previous_rounds}": previous_rounds_str,
        "{feedback_json}": str(feedback_path) if feedback_path else "（首轮，无反馈）",
        "{round_output_path}": str(round_output),
        "{N}": str(round_idx),
        "{project_id}": state.project_dir.name,
    }
    for k, v in replacements.items():
        system_prompt = system_prompt.replace(k, v)
    system_prompt += plan_prompt_context(state)
    analysis_gaps = state.notes.get("analysis_gap_requests", [])
    if analysis_gaps:
        gap_lines = "\n".join(
            f"- question_id={item['question_id']} | reason={item['reason']} | "
            f"needed_evidence={item['needed_evidence']}"
            for item in analysis_gaps
        )
        system_prompt += (
            "\n\n## Agent4 显式补研请求\n"
            "这些请求不是新任务协议；本轮仍须按现有流程采集，随后交给 Agent3 "
            "验证并通过 QualityGate。\n"
            f"{gap_lines}\n"
        )

    system_prompt += (
        f"\n\n## 当前项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 本轮序号：第 {round_idx} 轮\n"
        f"- 提纲：`{outline_path}`\n"
        f"- 源清单：`{sources_list_path}`\n"
        f"- 历史轮次文件：\n{previous_rounds_str}\n"
        f"- Agent3 反馈 JSON：`{feedback_path}`\n"
        f"- 本轮输出路径：`{round_output}`\n"
        f"- 研究需求清单：`{state.project_dir / config.FILE_RESEARCH_REQUIREMENTS}`\n"
    )
    # R3：注入结构化补研任务，取代 Agent2 从自由文本 gap 中重新理解缺口
    previous_tasks = load_tasks_file(config_tasks_path(state.project_dir))
    system_prompt += tasks_prompt_context(previous_tasks)

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write", "WebSearch", "WebFetch", "CaptureProjectWebSource", "ListProjectSources", "SearchProjectSources", "ReadProjectSource"],
        cwd=str(state.project_dir),
        max_turns=40,
    )

    console.print(
        f"\n[bold magenta]═══ Agent2 · 采集第 {round_idx} 轮 ═══[/bold magenta]\n"
        f"[dim]反馈：{feedback_path or '（首轮）'}[/dim]\n"
    )

    user_prompt = (
        f"请执行第 {round_idx} 轮数据采集，"
        f"读取提纲与源清单，"
        + (f"消费反馈 `{feedback_path}`，" if feedback_path else "")
        + f"将结果写入 `{round_output}`。"
    )
    await _run_and_print(user_prompt, options, "bright_green")

    if not round_output.exists():
        raise RuntimeError(f"Agent2 第 {round_idx} 轮未能生成 raw data 文件：{round_output}")

    console.print(f"[green]✓ 第 {round_idx} 轮采集完成：{round_output.name}[/green]")
    return round_output
