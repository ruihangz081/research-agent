"""Agent5 · 排版交付。

将 Agent4 的分析报告 + 全部产物排版为最终交付物。
先产出 Markdown 终稿，必要时标注格式转换建议。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from .. import config
from ..agent_loop import AgentOptions, run_agent
from ..agent_skills import load_project_skill
from ..llm import LLMClient
from ..report_charts import ChartManifest, load_chart_manifest
from ..report_layout import generate_typeset_artifacts
from ..research_plan import ResearchPlanError, require_plan
from ..sources.runtime import get_service
from ..sources.citations import validate_report_citations, validate_report_text_citations
from ..sources.claims import ClaimsError, load_claims_file
from ..sources.enums import VerificationStatus
from ..sources.models import EvidenceRecord, SourceAsset
from ..tools import default_registry

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


def _audit_final_report_claims(
    state: "ProjectState",
    report_path: Path,
    *,
    analysis_path: Path | None = None,
) -> None:
    """兼容旧调用方；生产交付使用更强的正文逐字保真审计。"""
    try:
        claims = load_claims_file(state.project_dir / config.FILE_CLAIMS)
    except ClaimsError as exc:
        raise RuntimeError(f"Agent5 结论保留审计失败：{exc}") from exc

    report_text = report_path.read_text(encoding="utf-8")
    baseline_text = (
        analysis_path.read_text(encoding="utf-8")
        if analysis_path is not None
        else None
    )
    missing = [
        item.claim_id
        for item in claims.claims
        if item.importance == "critical"
        and (baseline_text is None or item.text.strip() in baseline_text)
        and item.text.strip() not in report_text
    ]
    if missing:
        raise RuntimeError(
            "Agent5 终稿缺少 Agent4 的 critical 结论（结论在排版阶段丢失）："
            + ", ".join(missing)
        )


def _audit_composed_report(
    analysis_path: Path,
    report_path: Path,
    manifest: ChartManifest,
) -> None:
    """确认终稿确实是 Agent4 正文逐字加上图表占位符，没有别的改动。

    生产交付以此取代原来的"终稿必须保留 critical 结论"审计。那条审计在新架构下已
    结构上无法触发：终稿由 `_compose_final_report_from_analysis` 逐字复制正文、
    只在整行之间插入占位符，因此"在正文里"必然推出"在终稿里"，判定条件恒为空集。

    真正需要防的是 compose 本身被改坏——一旦它开始丢行、改行或重排，交付物就会
    悄悄偏离已通过 Agent4 门禁的正文，而没有任何检查会发现。所以改为反向校验：
    把终稿里的占位符行剔除后，必须与正文**逐字节相同**。

    结构化结论台账继续由 Agent4 阶段单独校验；Agent5 不再把台账内容作为
    补丁注入阅读版报告。
    """
    analysis = analysis_path.read_text(encoding="utf-8")
    report = report_path.read_text(encoding="utf-8")
    expected_placeholders = {f"{{{{chart:{chart.id}}}}}" for chart in manifest.charts}

    surplus: list[str] = []
    residual: list[str] = []
    for line in report.splitlines(keepends=True):
        stripped = line.strip()
        if stripped in expected_placeholders:
            expected_placeholders.discard(stripped)
            continue
        if not stripped and not analysis:
            continue
        residual.append(line)

    # 占位符插入时前后各带一个空行，剔除后会多出成对空行：按正文原样重建做整体比对，
    # 避免逐行做启发式判断。
    rebuilt = "".join(residual)
    if rebuilt != analysis:
        # 去掉占位符自带的空行后再比一次，仍不一致才算真的偏离。
        collapsed = re.sub(r"\n{3,}", "\n\n", rebuilt)
        if collapsed != re.sub(r"\n{3,}", "\n\n", analysis):
            raise RuntimeError(
                "Agent5 终稿与 Agent4 正文不一致：终稿必须是正文逐字复制加图表占位符，"
                "不得摘要、改写、重排或删行。"
            )
    if expected_placeholders:
        surplus = sorted(expected_placeholders)
        raise RuntimeError(
            "Agent5 终稿缺少图表清单声明的占位符：" + ", ".join(surplus)
        )


def _load_formatter_prompt() -> str:
    return _PROMPT_FORMATTER.read_text(encoding="utf-8")


def _can_reuse_chart_manifest(
    chart_manifest_path: Path,
    analysis_path: Path,
) -> bool:
    """Reuse only a current manifest whose charts have deterministic anchors."""
    if not (
        chart_manifest_path.is_file()
        and chart_manifest_path.stat().st_mtime >= analysis_path.stat().st_mtime
    ):
        return False
    try:
        manifest = load_chart_manifest(
            chart_manifest_path, max_charts=config.REPORT_MAX_CHARTS
        )
    except (ValueError, OSError):
        return False
    return all(chart.placement_after for chart in manifest.charts)


def _compose_final_report_from_analysis(
    analysis_path: Path,
    final_report_path: Path,
    manifest: ChartManifest,
) -> None:
    """Copy Agent4 verbatim and insert only deterministic chart placeholders."""
    analysis = analysis_path.read_text(encoding="utf-8")
    lines = analysis.splitlines(keepends=True)
    insertions: dict[int, list[str]] = {}

    for chart in manifest.charts:
        anchor = chart.placement_after
        if not anchor:
            raise ValueError(f"chart {chart.id} 缺少 placement_after")
        matches = [
            index
            for index, line in enumerate(lines)
            if line.rstrip("\r\n") == anchor
        ]
        if len(matches) != 1:
            raise ValueError(
                f"chart {chart.id} 的 placement_after 必须逐字匹配 Agent4 中唯一一行："
                f"当前匹配 {len(matches)} 行"
            )
        insertions.setdefault(matches[0], []).append(chart.id)

    newline = "\r\n" if "\r\n" in analysis else "\n"
    output: list[str] = []
    for index, line in enumerate(lines):
        output.append(line)
        for chart_id in insertions.get(index, []):
            output.append(f"{newline}{{{{chart:{chart_id}}}}}{newline}")
    final_report_path.write_text("".join(output), encoding="utf-8")


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

    analysis_path = Path(state.analysis_path)
    final_report_path = state.project_dir / config.FILE_FINAL_REPORT
    chart_manifest_path = state.project_dir / config.FILE_CHART_MANIFEST

    skill = load_project_skill(config.REPORT_FORMATTING_SKILL)
    system_prompt = _load_formatter_prompt() + skill.prompt_context()
    replacements = {
        "{analysis_path}": str(analysis_path),
        "{chart_manifest_path}": str(chart_manifest_path),
    }
    for k, v in replacements.items():
        system_prompt = system_prompt.replace(k, v)

    system_prompt += (
        f"\n\n## 当前项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 分析报告：`{analysis_path}`\n"
        f"- 图表清单输出路径：`{chart_manifest_path}`\n"
        f"- 已加载排版 Skill：`{skill.name}`\n"
    )
    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write"],
        cwd=str(state.project_dir),
        max_turns=12,
    )

    console.print(
        f"\n[bold magenta]═══ Agent5 · 排版交付 ═══[/bold magenta]\n"
        f"[dim]主题：{state.topic}[/dim]\n"
    )

    async def _on_text(text: str) -> None:
        console.print(text, style="bright_white", end="")

    reuse_generated = _can_reuse_chart_manifest(chart_manifest_path, analysis_path)
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
                    f"请读取 Agent4 深度分析并设计能增强其表达的真实图表。"
                    f"不要重写、摘要或压缩报告正文；只把图表清单写到 "
                    f"`{chart_manifest_path}`。"
                ),
                options=options,
                llm_client=client,
                tool_registry=default_registry,
                on_assistant_text=_on_text,
            )
        console.print()
    else:
        console.print("[dim]复用已生成的图表清单，重新从 Agent4 正文排版交付。[/dim]")

    if not chart_manifest_path.exists():
        raise RuntimeError(f"Agent5 未能生成图表清单：{chart_manifest_path}")
    manifest = load_chart_manifest(
        chart_manifest_path, max_charts=config.REPORT_MAX_CHARTS
    )
    _compose_final_report_from_analysis(analysis_path, final_report_path, manifest)

    _audit_final_report_citations(final_report_path, supported, sources)
    _audit_composed_report(analysis_path, final_report_path, manifest)
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
