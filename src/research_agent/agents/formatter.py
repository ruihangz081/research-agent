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
from ..pipeline_errors import DETERMINISTIC_CONTENT_HINT, DeterministicContentError
from ..report_charts import ChartManifest, load_chart_manifest
from ..report_formatting import canonicalize_report_text
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
    *,
    repository=None,
    project_id: str | None = None,
) -> None:
    # 终稿引用审计必须先归一化：拆分 [事实｜src:...]，并（若传了证据库）把 Agent4
    # 只写的 [ev=ev_xxx] 展开为标准引用——否则会被误判为「非精确 EvidenceRecord」。
    report_text = canonicalize_report_text(
        report_path.read_text(encoding="utf-8"),
        repository=repository,
        project_id=project_id,
    )
    valid, errors = validate_report_text_citations(report_text, supported, sources)
    if not valid:
        details = "; ".join(errors[:10])
        if len(errors) > 10:
            details += f"; 另有 {len(errors) - 10} 项"
        raise DeterministicContentError(
            f"Agent5 引用审计失败：{details}（{DETERMINISTIC_CONTENT_HINT}）"
        )


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
    """复用当前有效的图表清单，避免重复调用 LLM 生成。

    锚点位置漂移（多打/少打 #、行首缩进、匹配多行）不再构成复用障碍——``_resolve_anchor_index``
    会降级插入到文末，不会作废整轮。因此这里只校验清单可解析、时间戳不早于正文，
    以及每条图都有非空 placement_after（空锚点会降级到文末，虽可用但提示 LLM 重新生成更合适）。
    """
    if not (
        chart_manifest_path.is_file()
        and chart_manifest_path.stat().st_mtime >= analysis_path.stat().st_mtime
    ):
        return False
    try:
        manifest = load_chart_manifest(
            chart_manifest_path, max_charts=config.REPORT_MAX_CHARTS
        )
    except (ValueError, OSError, DeterministicContentError):
        return False
    try:
        analysis_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for chart in manifest.charts:
        if not chart.placement_after:
            return False
    return True


def _anchor_matches(anchor: str, lines: list[str]) -> list[int]:
    """返回 anchor 逐字匹配的行索引（忽略行首/行尾空白）。

    placement_after 是模型从 Agent4 正文复制的一整行。正文行首常有 Markdown 缩进
    （如 ``  **加粗行**``），模型复制锚点时偶发丢掉行首空格，导致 ``line.rstrip()``
    匹配 0 行。因此两侧 strip 后再比对。
    """
    target = anchor.strip()
    return [
        index
        for index, line in enumerate(lines)
        if line.strip() == target
    ]


def _resolve_anchor_index(anchor: str | None, lines: list[str]) -> int:
    """把图表锚点解析成正文中的插入行索引；解析不到时降级到文末，绝不阻断。

    锚点只是「图表放哪」的排版提示，不是事实正确性门禁。模型偶发多打/少打一个
    Markdown ``#``、或复制到行首缩进，就把锚点判成「不唯一/不存在」并作废整轮、
    重跑 LLM，代价远大于「图表位置略有偏移」。因此这里按宽松优先级解析：

    1. 两侧 strip 后逐字唯一匹配 → 精确插入该行后；
    2. 唯一匹配失败（0 行或多行）→ 忽略 Markdown 标题 ``#`` 前缀再匹配一次；
    3. 仍失败 → 降级到文末（最后一行之后）。

    任何情况下都返回一个合法行索引，调用方不再因锚点问题抛异常。
    """
    if anchor is None or not anchor.strip():
        return len(lines) - 1
    target = anchor.strip()

    exact = _anchor_matches(target, lines)
    if len(exact) == 1:
        return exact[0]

    # 忽略 Markdown 标题符号（#）与两侧空白后再匹配，容忍模型多打/少打 # 层级。
    def _norm(value: str) -> str:
        return value.strip().lstrip("#").strip()

    normalized = _norm(target)
    fuzzy = [
        index
        for index, line in enumerate(lines)
        if _norm(line) == normalized
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        return fuzzy[0]

    # 多匹配或完全匹配不到：降级到文末，保证图表仍能插入、报告仍能交付。
    return len(lines) - 1


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
        index = _resolve_anchor_index(anchor, lines)
        insertions.setdefault(index, []).append(chart.id)

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

    degradation: list[str] = []
    if not chart_manifest_path.exists():
        # 图表清单缺失只影响图表，不影响正文交付：降级为空清单 → 无图版报告。
        degradation.append(f"图表清单缺失，已交付无图版报告：{chart_manifest_path}")
        manifest = ChartManifest(version=1, charts=[])
    else:
        try:
            manifest = load_chart_manifest(
                chart_manifest_path, max_charts=config.REPORT_MAX_CHARTS
            )
        except Exception as exc:
            degradation.append(f"图表清单无法解析，已交付无图版报告：{exc}")
            manifest = ChartManifest(version=1, charts=[])
    _compose_final_report_from_analysis(analysis_path, final_report_path, manifest)

    _audit_final_report_citations(
        final_report_path,
        supported,
        sources,
        repository=get_service(config.SOURCE_DATA_DIR).repository,
        project_id=state.project_dir.name,
    )
    _audit_composed_report(analysis_path, final_report_path, manifest)
    state.final_report_path = str(final_report_path)
    if chart_manifest_path.exists():
        state.chart_manifest_path = str(chart_manifest_path)
    state.save()

    console.print(f"\n[green]✓ 最终报告已生成：{final_report_path.name}[/green]")

    # 多格式独立交付：Markdown 已就绪，HTML/PDF 各自独立降级，任一失败都不阻断。
    # generate_report_artifacts 内部已把 HTML/PDF 拆成独立 try，失败只进 degradation
    # 列表，不再抛异常打挂整个 Agent5。
    console.print("[cyan]正在生成券商研报图表、HTML 与正式 PDF...[/cyan]")
    try:
        artifacts = await generate_typeset_artifacts(
            topic=state.topic,
            project_dir=state.project_dir,
            final_report_path=final_report_path,
        )
    except Exception as exc:
        # 排版交付物生成失败不阻断正文交付：Markdown 已经就绪，降级为仅 Markdown。
        state.notes["latex_typeset_error"] = str(exc)
        degradation.append(f"排版交付物生成失败，已交付 Markdown：{exc}")
        artifacts = {
            "tex_path": None,
            "html_path": None,
            "pdf_path": None,
            "degradation": [],
        }
    artifacts_degradation = list(artifacts.get("degradation", []))
    degradation.extend(artifacts_degradation)
    # chrome 引擎不产出 .tex：artifacts["tex_path"] 为 None，不得写 state 兜底路径
    # （否则前端会给出一个 404 的下载链接）。
    state.final_report_tex_path = (
        str(artifacts["tex_path"]) if artifacts.get("tex_path") else None
    )
    state.final_report_html_path = (
        str(artifacts["html_path"]) if artifacts.get("html_path") else None
    )
    state.final_report_pdf_path = (
        str(artifacts["pdf_path"]) if artifacts.get("pdf_path") else None
    )
    if artifacts.get("pdf_path"):
        state.final_report_typeset_pdf_path = str(artifacts["pdf_path"])
        console.print(
            f"[green]✓ 正式 PDF 已生成：{artifacts['pdf_path'].name}[/green]"
        )
    else:
        console.print(
            "[yellow]PDF 未生成（HTML/Markdown 仍可用），交付已降级。[/yellow]"
        )

    # 落降级详情到状态：供 done_degraded 状态与前端展示。
    if degradation:
        state.notes["delivery_degradation"] = degradation
        state.notes["delivery_status"] = "done_degraded"
    else:
        state.notes.pop("delivery_degradation", None)
        state.notes["delivery_status"] = "done"
        # 无降级时才清除排版错误标记；有降级时保留供诊断。
        state.notes.pop("latex_typeset_error", None)
    # 图表降级（溯源失败 → 表格）也在 generate_report_artifacts 内发生，
    # 通过 provenance report 落到 05_chart_provenance.json，这里只保留 manifest 级降级。
    state.save()

    return final_report_path
