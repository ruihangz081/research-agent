"""Agent5 · 排版交付。

将 Agent4 的分析报告 + 全部产物排版为最终交付物。
先产出 Markdown 终稿，必要时标注格式转换建议。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from .. import config
from ..agent_loop import AgentOptions, run_agent
from ..llm import LLMClient
from ..report_layout import generate_typeset_artifacts
from ..tools import default_registry
from .source_context import source_context

if TYPE_CHECKING:
    from ..state import ProjectState

console = Console()

_PROMPT_FORMATTER = Path(__file__).parent / "prompts" / "formatter.md"


def _load_formatter_prompt() -> str:
    return _PROMPT_FORMATTER.read_text(encoding="utf-8")


async def run_formatting(state: "ProjectState") -> Path:
    """排版生成最终报告。"""
    if not state.outline_path:
        raise RuntimeError("需要 outline.md")
    if not state.analysis_path:
        raise RuntimeError("需要 analysis.md（Agent4 产出）")
    if not state.sources_final_path:
        raise RuntimeError("需要 sources_final.md")

    outline_path = Path(state.outline_path)
    analysis_path = Path(state.analysis_path)
    sources_final_path = Path(state.sources_final_path)
    validation_report_path = (
        Path(state.validation_report_path)
        if state.validation_report_path
        else None
    )
    final_report_path = state.project_dir / config.FILE_FINAL_REPORT

    system_prompt = _load_formatter_prompt()
    system_prompt += source_context(state)
    replacements = {
        "{outline_path}": str(outline_path),
        "{analysis_path}": str(analysis_path),
        "{sources_final_path}": str(sources_final_path),
        "{validation_report_path}": str(validation_report_path or "（无）"),
        "{final_report_path}": str(final_report_path),
    }
    for k, v in replacements.items():
        system_prompt = system_prompt.replace(k, v)

    system_prompt += (
        f"\n\n## 当前项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 提纲：`{outline_path}`\n"
        f"- 分析报告：`{analysis_path}`\n"
        f"- 最终源清单：`{sources_final_path}`\n"
        f"- 验证报告：`{validation_report_path}`\n"
        f"- 最终报告输出路径：`{final_report_path}`\n"
    )

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write", "ReadProjectSource", "InspectSourceEvidence"],
        cwd=str(state.project_dir),
        max_turns=40,
    )

    console.print(
        f"\n[bold magenta]═══ Agent5 · 排版交付 ═══[/bold magenta]\n"
        f"[dim]主题：{state.topic}[/dim]\n"
    )

    async def _on_text(text: str) -> None:
        console.print(text, style="bright_white", end="")

    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        await run_agent(
            user_prompt=(
                f"请将深度分析排版为最终报告。"
                f"读取提纲、分析报告、源清单，"
                f"产出完整可独立阅读的行业调研报告到 `{final_report_path}`。"
            ),
            options=options,
            llm_client=client,
            tool_registry=default_registry,
            on_assistant_text=_on_text,
        )
    console.print()

    if not final_report_path.exists():
        raise RuntimeError(f"Agent5 未能生成最终报告：{final_report_path}")

    console.print(f"\n[green]✓ 最终报告已生成：{final_report_path.name}[/green]")

    try:
        console.print("[cyan]正在生成 LaTeX 排版交付物...[/cyan]")
        artifacts = await generate_typeset_artifacts(
            topic=state.topic,
            project_dir=state.project_dir,
            final_report_path=final_report_path,
        )
        state.final_report_tex_path = str(artifacts["tex_path"])
        if artifacts["pdf_path"]:
            state.final_report_typeset_pdf_path = str(artifacts["pdf_path"])
            console.print(
                f"[green]✓ LaTeX 高级 PDF 已生成：{artifacts['pdf_path'].name}[/green]"
            )
        else:
            console.print(
                "[yellow]已生成 LaTeX 源文件；本机未检测到 xelatex/lualatex，"
                "暂未自动编译 PDF。[/yellow]"
            )
        state.save()
    except Exception as e:
        state.notes["latex_typeset_error"] = str(e)
        state.save()
        console.print(f"[yellow]LaTeX 排版交付物生成失败：{e}[/yellow]")

    return final_report_path
