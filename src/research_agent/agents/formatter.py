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
from ..agent_skills import load_project_skill
from ..llm import LLMClient
from ..report_charts import load_chart_manifest
from ..report_layout import generate_typeset_artifacts
from ..research_plan import ResearchPlanError, plan_prompt_context, require_plan
from ..sources.runtime import get_service
from ..sources.citations import (
    render_citation,
    validate_report_citations,
    validate_report_text_citations,
)
from ..sources.enums import VerificationStatus
from ..sources.models import EvidenceRecord, SourceAsset
from ..tools import default_registry
from .source_context import source_context

if TYPE_CHECKING:
    from ..state import ProjectState

console = Console()

_PROMPT_FORMATTER = Path(__file__).parent / "prompts" / "formatter.md"


def _require_delivery_evidence(
    state: "ProjectState",
) -> tuple[list[EvidenceRecord], dict[str, SourceAsset]]:
    service = get_service(config.SOURCE_DATA_DIR)
    project_id = state.project_dir.name
    evidence = service.repository.list_evidence(project_id)
    supported = [
        item
        for item in evidence
        if item.verification_status == VerificationStatus.SUPPORTED
    ]
    # R1：要求集合来自研究开始阶段固化的清单，不是从 supported 证据反推。
    # 缺失清单一律阻断，绝不按空集合放行。
    try:
        requirements = require_plan(state).as_requirements()
    except ResearchPlanError as exc:
        state.save()
        raise RuntimeError(f"quality gate blocked final delivery: {exc}") from exc
    gate = service.quality_gate(project_id, requirements)
    sources = {
        source.source_id: source
        for source in service.list_sources(project_id, include_superseded=True)
    }
    state.notes["quality_gate"] = gate.status.value
    state.notes["quality_gate_reasons"] = gate.reasons
    state.notes["research_question_coverage"] = gate.coverage
    valid, errors = validate_report_citations(supported, sources)
    if not gate.passed:
        state.save()
        raise RuntimeError(f"quality gate blocked final delivery: {gate.status.value}: {gate.reasons}")
    if not valid:
        raise RuntimeError(f"citation audit failed: {errors}")
    return supported, sources


def _finalize_evidence_appendix(state: "ProjectState", report_path: Path) -> None:
    supported, sources = _require_delivery_evidence(state)
    report = report_path.read_text(encoding="utf-8")
    appendix = ["", "---", "", "## 可追溯证据索引", ""]
    for item in supported:
        source = sources[item.source_id]
        citation = render_citation(item, source)
        origin = f"（[原文]({source.origin_url})）" if source.origin_url else ""
        appendix.append(f"- {citation} **{item.claim}** — {item.excerpt}{origin}")
    marker = "## 可追溯证据索引"
    if marker in report:
        report = report.split(marker, 1)[0].rstrip()
    report_path.write_text(report.rstrip() + "\n" + "\n".join(appendix) + "\n", encoding="utf-8")


def _audit_final_report_citations(
    report_path: Path,
    supported: list[EvidenceRecord],
    sources: dict[str, SourceAsset],
) -> None:
    valid, errors = validate_report_text_citations(
        report_path.read_text(encoding="utf-8"), supported, sources
    )
    if not valid:
        details = "; ".join(errors[:10])
        if len(errors) > 10:
            details += f"; 另有 {len(errors) - 10} 项"
        raise RuntimeError(f"Agent5 引用审计失败：{details}")


def _load_formatter_prompt() -> str:
    return _PROMPT_FORMATTER.read_text(encoding="utf-8")


def _can_reuse_generated(
    final_report_path: Path,
    chart_manifest_path: Path,
    analysis_path: Path,
) -> bool:
    """Reuse Agent5 output only when both files are current and valid."""
    if not (
        final_report_path.is_file()
        and chart_manifest_path.is_file()
        and final_report_path.stat().st_mtime >= analysis_path.stat().st_mtime
        and chart_manifest_path.stat().st_mtime >= analysis_path.stat().st_mtime
    ):
        return False
    try:
        load_chart_manifest(chart_manifest_path, max_charts=config.REPORT_MAX_CHARTS)
    except (ValueError, OSError):
        return False
    return True


async def run_formatting(state: "ProjectState") -> Path:
    """排版生成最终报告。"""
    if not state.outline_path:
        raise RuntimeError("需要 outline.md")
    if not state.analysis_path:
        raise RuntimeError("需要 analysis.md（Agent4 产出）")
    if not state.sources_final_path:
        raise RuntimeError("需要 sources_final.md")

    # Fail before invoking the LLM. A formatter cannot repair missing source
    # provenance, and retrying it only regenerates the same blocked artifacts.
    supported, sources = _require_delivery_evidence(state)

    outline_path = Path(state.outline_path)
    analysis_path = Path(state.analysis_path)
    sources_final_path = Path(state.sources_final_path)
    validation_report_path = (
        Path(state.validation_report_path)
        if state.validation_report_path
        else None
    )
    final_report_path = state.project_dir / config.FILE_FINAL_REPORT
    chart_manifest_path = state.project_dir / config.FILE_CHART_MANIFEST

    skill = load_project_skill(config.REPORT_FORMATTING_SKILL)
    system_prompt = _load_formatter_prompt() + skill.prompt_context()
    system_prompt += plan_prompt_context(state)
    system_prompt += source_context(state)
    replacements = {
        "{outline_path}": str(outline_path),
        "{analysis_path}": str(analysis_path),
        "{sources_final_path}": str(sources_final_path),
        "{validation_report_path}": str(validation_report_path or "（无）"),
        "{final_report_path}": str(final_report_path),
        "{chart_manifest_path}": str(chart_manifest_path),
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
        f"- 图表清单输出路径：`{chart_manifest_path}`\n"
        f"- 已加载排版 Skill：`{skill.name}`\n"
    )
    output_preference = state.notes.get("output_preference", config.OUTPUT_PREFERENCE)
    preference_instructions = {
        "fast": "精简优先：突出摘要、关键证据和可执行结论，避免重复展开。",
        "balanced": "平衡优先：兼顾阅读效率、分析深度与证据完整性。",
        "deep": "深度优先：充分展开论证、反方证据、限制条件和来源细节。",
    }
    system_prompt += (
        f"- 输出偏好：{output_preference}\n"
        f"- 写作要求：{preference_instructions.get(output_preference, preference_instructions['balanced'])}\n"
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

    reuse_generated = _can_reuse_generated(
        final_report_path,
        chart_manifest_path,
        analysis_path,
    )
    if not reuse_generated:
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
                    f"产出完整可独立阅读的行业调研报告到 `{final_report_path}`，"
                    f"并把真实图表清单写到 `{chart_manifest_path}`。"
                ),
                options=options,
                llm_client=client,
                tool_registry=default_registry,
                on_assistant_text=_on_text,
            )
        console.print()
    else:
        console.print("[dim]复用已生成的 Markdown 和图表清单，继续排版交付。[/dim]")

    if not final_report_path.exists():
        raise RuntimeError(f"Agent5 未能生成最终报告：{final_report_path}")
    if not chart_manifest_path.exists():
        raise RuntimeError(f"Agent5 未能生成图表清单：{chart_manifest_path}")
    load_chart_manifest(chart_manifest_path, max_charts=config.REPORT_MAX_CHARTS)

    # Audit the authored report before adding the generated evidence appendix;
    # otherwise appendix citations could mask a report body with no valid citations.
    _audit_final_report_citations(final_report_path, supported, sources)
    _finalize_evidence_appendix(state, final_report_path)
    state.final_report_path = str(final_report_path)
    state.chart_manifest_path = str(chart_manifest_path)
    state.save()

    console.print(f"\n[green]✓ 最终报告已生成：{final_report_path.name}[/green]")

    try:
        console.print("[cyan]正在生成券商研报图表、HTML 与正式 PDF...[/cyan]")
        artifacts = await generate_typeset_artifacts(
            topic=state.topic,
            project_dir=state.project_dir,
            final_report_path=final_report_path,
        )
        state.final_report_tex_path = str(artifacts["tex_path"])
        state.final_report_html_path = str(artifacts["html_path"])
        state.final_report_pdf_path = (
            str(artifacts["pdf_path"]) if artifacts["pdf_path"] else None
        )
        if artifacts["pdf_path"]:
            state.final_report_typeset_pdf_path = str(artifacts["pdf_path"])
            console.print(
                f"[green]✓ 正式 PDF 已生成：{artifacts['pdf_path'].name}[/green]"
            )
        else:
            console.print(
                "[yellow]已生成 HTML 与 LaTeX 源文件；本机未检测到配置的 LaTeX 引擎，"
                "暂未自动编译 PDF。[/yellow]"
            )
        state.notes.pop("latex_typeset_error", None)
        state.save()
    except Exception as e:
        state.notes["latex_typeset_error"] = str(e)
        state.save()
        raise RuntimeError(f"Agent5 排版交付物生成失败：{e}") from e

    return final_report_path
