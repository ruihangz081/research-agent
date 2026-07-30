"""Agent4 · 深度分析。

基于验证后的数据做全方位分析，产出 04_analysis.md。
必要时通过 WebSearch 搜索可用的分析类 skill（不阻塞主流程）。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from .. import config
from ..agent_loop import AgentOptions, run_agent
from ..llm import LLMClient
from ..research_plan import plan_prompt_context
from ..tools import default_registry
from .source_context import source_context

if TYPE_CHECKING:
    from ..state import ProjectState

console = Console()

_PROMPT_ANALYST = Path(__file__).parent / "prompts" / "analyst.md"


def _load_analyst_prompt() -> str:
    return _PROMPT_ANALYST.read_text(encoding="utf-8")


async def run_analysis(state: "ProjectState") -> Path:
    """运行深度分析，输出 04_analysis.md。"""
    if not state.outline_path:
        raise RuntimeError("需要 outline.md")
    if not state.sources_final_path:
        raise RuntimeError("需要 sources_final.md")

    outline_path = Path(state.outline_path)
    sources_final_path = Path(state.sources_final_path)
    validation_report_path = (
        Path(state.validation_report_path)
        if state.validation_report_path
        else None
    )
    raw_data_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    analysis_path = state.project_dir / config.FILE_ANALYSIS

    raw_rounds = sorted(raw_data_dir.glob("round_*.md")) if raw_data_dir.exists() else []
    raw_rounds_str = "\n".join(f"  - {p}" for p in raw_rounds) or "  （无）"

    system_prompt = _load_analyst_prompt()
    system_prompt += plan_prompt_context(state)
    system_prompt += source_context(state)
    replacements = {
        "{outline_path}": str(outline_path),
        "{sources_final_path}": str(sources_final_path),
        "{validation_report_path}": str(validation_report_path or "（无）"),
        "{raw_data_dir}": str(raw_data_dir),
        "{analysis_path}": str(analysis_path),
        "{N}": str(state.collect_round),
    }
    for k, v in replacements.items():
        system_prompt = system_prompt.replace(k, v)

    system_prompt += (
        f"\n\n## 当前项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 提纲：`{outline_path}`\n"
        f"- 最终源清单：`{sources_final_path}`\n"
        f"- 验证报告：`{validation_report_path}`\n"
        f"- 原始数据文件：\n{raw_rounds_str}\n"
        f"- 分析报告输出路径：`{analysis_path}`\n"
        f"- 研究需求清单：`{state.project_dir / config.FILE_RESEARCH_REQUIREMENTS}`\n"
    )

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write", "WebSearch", "WebFetch", "ListProjectSources", "SearchProjectSources", "ReadProjectSource", "InspectSourceEvidence"],
        cwd=str(state.project_dir),
        max_turns=50,
    )

    console.print(
        f"\n[bold magenta]═══ Agent4 · 深度分析 ═══[/bold magenta]\n"
        f"[dim]主题：{state.topic}[/dim]\n"
        f"[dim]数据轮次：{state.collect_round} 轮[/dim]\n"
    )

    async def _on_text(text: str) -> None:
        console.print(text, style="bright_blue", end="")

    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        await run_agent(
            user_prompt=(
                f"请基于所有已验证的数据，对「{state.topic}」做全方位深度分析，"
                f"读取提纲、源清单、验证报告和原始数据，"
                f"将分析报告写入 `{analysis_path}`。"
            ),
            options=options,
            llm_client=client,
            tool_registry=default_registry,
            on_assistant_text=_on_text,
        )
    console.print()

    if not analysis_path.exists():
        raise RuntimeError(f"Agent4 未能生成分析报告：{analysis_path}")

    console.print(f"\n[green]✓ 深度分析完成：{analysis_path.name}[/green]")
    return analysis_path
