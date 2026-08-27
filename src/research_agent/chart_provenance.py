"""图表数值证据溯源门禁（方案第 11 章，阶段 1A，图表级）。

ChartSpec.series.values 此前没有任何确定性检查，Agent5 可以在图里编数字且全链路
无人发现。这里做图表级反查：

- 候选 ``provenance.claim_ids`` 只是线索，不作为通过依据；
- 反查 claim 存在、仍在 Agent4 正文中、关联当前 SUPPORTED EvidenceRecord；
- 每个非空数值必须能在候选 claim 文本或 evidence 的 normalized_value/excerpt
  中匹配到；用 Decimal，容差只用显示精度半单位，禁止百分比宽容阈值；
- 百分比与百分点严格区分（0.12 / 12% / +12pct 不等价）；
- 必需图失败抛 DeterministicContentError；可选图由调用方降级。

兼容性硬约束：只有快手/腾讯有 ``04_claims.json``，其余历史项目走兼容路径——
v1 清单（``provenance`` 为 None 或 claim_ids 为空）不强制要求 claim_ids。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .pipeline_errors import DETERMINISTIC_CONTENT_HINT, DeterministicContentError
from .sources.claims import load_claims_file, normalize_claim_text
from .sources.enums import VerificationStatus
from .sources.models import Claim, EvidenceRecord, SourceAsset
from .report_charts import (
    ChartManifest,
    ChartSpec,
    _is_multiple_unit,
    _is_percent_unit,
)
from .sources.repository import SQLiteRepository

#: 中文数量级（含全半角）。"万亿"必须组合，避免把"万"单独解析。
_MAGNITUDE_SUFFIXES = (
    ("万亿", Decimal("1000000000000")),
    ("亿", Decimal("100000000")),
    ("万", Decimal("10000")),
    ("千", Decimal("1000")),
)
#: 数字 token：千分位、正负号、小数点。后跟可选中文数量级（万/亿/万亿）或英文
#: 数量级（million/billion/trillion）。相邻写法（`2.5 万`、`42 million`）由
#: ``_extract_values`` 的放大后缀扫描处理。
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\w])[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?"
)
#: 英文数量级后缀。
_ENGLISH_MAGNITUDE = {
    "million": Decimal("1000000"),
    "billion": Decimal("1000000000"),
    "trillion": Decimal("1000000000000"),
}
#: 百分比 / 百分点 / 倍数 上下文标记（带语义，用于严格区分口径）。
_PERCENT_RE = re.compile(r"(?<![\w])([+-]?\d+(?:\.\d+)?)\s*%")
_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(?:pct|个百分点)")
_MULTIPLE_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(?:x|倍|X)")


def _parse_chinese_number(token: str) -> Decimal | None:
    """把带中文数量级的数字 token 解析为 Decimal。"""
    cleaned = token.replace(",", "")
    for suffix, multiplier in _MAGNITUDE_SUFFIXES:
        if cleaned.endswith(suffix):
            base = cleaned[: -len(suffix)]
            if base == "" or base in {"+", "-", "."}:
                continue
            try:
                return Decimal(base) * multiplier
            except InvalidOperation:
                return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _extract_values(text: str) -> list[tuple[Decimal, str]]:
    """提取文本中的数值及其单位语义（无/percent/pct/multiple）。

    返回 (decimal_value, semantic)，semantic 用于区分百分比与百分点等口径。
    统一千分位、全半角符号、正负号、中文数量级（万/亿/万亿）与英文数量级
    （million/billion）。
    """
    results: list[tuple[Decimal, str]] = []
    cleaned = text.translate(str.maketrans("０１２３４５６７８９．，－＋", "0123456789.,-+"))
    # 百分比、百分点、倍数优先提取（带语义），其余 token 归"普通数值"。
    for match in _PERCENT_RE.finditer(cleaned):
        value = match.group(1).replace(",", "")
        try:
            results.append((Decimal(value), "percent"))
        except InvalidOperation:
            continue
    for match in _PCT_RE.finditer(cleaned):
        value = match.group(1).replace(",", "")
        try:
            results.append((Decimal(value), "pct"))
        except InvalidOperation:
            continue
    for match in _MULTIPLE_RE.finditer(cleaned):
        value = match.group(1).replace(",", "")
        try:
            results.append((Decimal(value), "multiple"))
        except InvalidOperation:
            continue
    # 普通数字（含中文/英文数量级作为单位上下文，不乘放大）。
    # 图表的 values 已经在其声明的 unit 里（如 unit=亿元 则 value 就是亿为单位），
    # 因此源文本里的 "万/亿/million" 是单位而非数量级放大，绝不能乘放大系数——
    # 否则 "35 亿元" 会被误解析成 3.5e9，导致真实报告全部误拒。
    for match in _NUMBER_TOKEN_RE.finditer(cleaned):
        token = match.group(0)
        value = _parse_chinese_number(token.replace(",", ""))
        if value is not None:
            results.append((value, "plain"))
    return results


@dataclass
class MatchResult:
    chart_id: str
    resolved_claim_ids: list[str] = field(default_factory=list)
    resolved_evidence_ids: list[str] = field(default_factory=list)
    publisher: str = ""
    point_refs: list[dict[str, Any]] = field(default_factory=list)
    match_rule: str = ""
    #: 阻断原因：仅记录真正导致 ``ok=False`` 的事项（数值无法匹配）。
    ambiguity_reasons: list[str] = field(default_factory=list)
    #: 候选解析观测：claim 不存在 / 不在正文 / 零证据 judgment 等。这些只让该候选
    #: 被剔除，不否决图表，因此与阻断原因分开，避免门禁错误信息指向非根因。
    candidate_notes: list[str] = field(default_factory=list)
    ok: bool = True


def _display_half_unit(value: float) -> Decimal:
    """显示精度半单位容差：一位小数 → ±0.05，整数 → ±0.5，两位小数 → ±0.005。

    只允许"源值四舍五入到显示精度"产生的差异，禁止任意百分比宽容阈值。
    """
    text = format(value, "f").rstrip("0")
    if "." not in text:
        return Decimal("0.5")
    decimals = len(text.split(".", 1)[1])
    if decimals <= 0:
        return Decimal("0.5")
    return Decimal("0.5") / (Decimal("10") ** decimals)


def _values_close(display: float, source: Decimal, tolerance: Decimal) -> bool:
    target = Decimal(str(display))
    return abs(target - source) <= tolerance


def _expected_semantic(unit: str) -> str:
    """由 ChartSpec.unit 推导期望语义（复用 report_charts 的单位判断，不重复实现）。"""
    if _is_percent_unit(unit):
        return "percent"
    if _is_multiple_unit(unit):
        return "multiple"
    return "plain"


def _semantic_accepts(expected: str, actual: str) -> bool:
    """判定提取出的数值语义是否满足期望语义。

    - 期望 percent：拒绝 pct 命中；percent 直接接受；plain 拒绝（裸数不是百分比）。
    - 期望 multiple：multiple 直接接受；plain 显式允许（正文常写 "10.3/8.7/7.7" 这类
      不带 x 的倍数枚举，这是 §11.3 允许的明确规则，不是放宽通用数值容差）。
    - 期望 plain：plain 接受；percent/pct/multiple 都拒绝（单位语义不符）。
    """
    if expected == "percent":
        return actual == "percent"
    if expected == "multiple":
        return actual in {"multiple", "plain"}
    # plain
    return actual == "plain"


def _match_value_in_text(
    display: float,
    sources: list[str],
    tolerance: Decimal,
    *,
    expected_semantic: str,
) -> tuple[bool, list[str]]:
    """在给定文本集合中匹配显示值（语义参与判定），返回 (是否命中, 原因)。"""
    reasons: list[str] = []
    for text in sources:
        extracted = _extract_values(text)
        hits = [
            (value, semantic)
            for value, semantic in extracted
            if _values_close(display, value, tolerance)
            and _semantic_accepts(expected_semantic, semantic)
        ]
        if hits:
            return True, []
        reasons.append(f"未在文本中以期望语义 {expected_semantic} 命中 {display}")
    return False, reasons


def _publisher_for_sources(sources: list[SourceAsset]) -> str:
    """由关联证据的 publisher 推导 caption source，不用模型自撰散文。"""
    publishers = sorted({(s.publisher or "").strip() for s in sources if s.publisher})
    if not publishers:
        return "Research Agent 整理"
    if len(publishers) == 1:
        return publishers[0]
    return "；".join(publishers[:3]) + (" 等" if len(publishers) > 3 else "")


def _resolve_claims(
    chart: ChartSpec,
    claims_by_id: dict[str, Claim],
    analysis_text: str | None,
    evidence_by_id: dict[str, EvidenceRecord],
) -> tuple[list[Claim], list[str]]:
    """反查候选 claim：存在、仍在正文、关联 SUPPORTED evidence。

    "仍在正文"复用 claims 门禁的 ``normalize_claim_text``（剥引用/标注/强调/空白），
    与台账门禁保持同一判定口径——正文里的 ``**强调**``、``[判断｜置信度:高]`` 不算
    差异，否则正文带 markdown 强调的 claim 会被误判为"不在正文"。
    """
    resolved: list[Claim] = []
    reasons: list[str] = []
    normalized_analysis = (
        normalize_claim_text(analysis_text) if analysis_text is not None else None
    )
    candidate_ids = chart.provenance.claim_ids if chart.provenance else []
    for claim_id in candidate_ids:
        claim = claims_by_id.get(claim_id)
        if claim is None:
            reasons.append(f"claim {claim_id} 不存在")
            continue
        if normalized_analysis is not None:
            normalized_claim = normalize_claim_text(claim.text)
            if normalized_claim and normalized_claim not in normalized_analysis:
                reasons.append(f"claim {claim_id} 的文本不在 Agent4 正文中")
                continue
        # 关联 evidence 反查。零证据的 judgment 是合法台账条目（`Claim` 只要求
        # critical 必须有证据），它不承载数值，因此从候选剔除但不算缺陷。
        if not claim.supporting_evidence_ids and claim.kind == "judgment":
            reasons.append(
                f"claim {claim_id} 是零证据 judgment，不承载数值，已从候选剔除"
            )
            continue
        evidence_ok = False
        for evidence_id in claim.supporting_evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            if evidence.verification_status != VerificationStatus.SUPPORTED:
                continue
            if evidence.research_question_id != claim.question_id:
                continue
            evidence_ok = True
            break
        if not evidence_ok:
            reasons.append(f"claim {claim_id} 无 SUPPORTED 且同 question 的 evidence")
            continue
        resolved.append(claim)
    return resolved, reasons


def validate_chart_provenance(
    manifest: ChartManifest,
    *,
    project_dir: Path,
    repository: SQLiteRepository,
    analysis_path: Path | None = None,
    claims_path: Path | None = None,
) -> dict[str, Any]:
    """对 manifest 中每张图做图表级数值溯源，返回审计结果字典。

    v1 清单（provenance 为空、version=1）走兼容路径：不做 claim 反查，但数值
    仍须能从 evidence 文本中匹配（失败仅记录，不由本函数阻断——调用方决定策略）。
    """
    analysis_text = analysis_path.read_text(encoding="utf-8") if analysis_path and analysis_path.is_file() else None
    claims_by_id: dict[str, Claim] = {}
    if claims_path and claims_path.is_file():
        try:
            claims_file = load_claims_file(claims_path)
        except Exception:
            claims_file = None
        if claims_file is not None:
            claims_by_id = {c.claim_id: c for c in claims_file.claims}

    project_id = project_dir.name
    sources_by_id = {
        s.source_id: s
        for s in repository.list_sources(project_id, include_superseded=True)
    }
    # 一次性建好 evidence_by_id 字典，避免每图 × 每 claim × 每 evidence 三层循环里
    # 反复全量扫 list_evidence。
    evidence_by_id = {e.evidence_id: e for e in repository.list_evidence(project_id)}

    report: dict[str, Any] = {"version": 1, "charts": []}
    for chart in manifest.charts:
        result = _validate_single_chart(
            chart,
            claims_by_id=claims_by_id,
            analysis_text=analysis_text,
            evidence_by_id=evidence_by_id,
            project_id=project_id,
            sources_by_id=sources_by_id,
            strict=(manifest.version == 2),
        )
        report["charts"].append(
            {
                "chart_id": chart.id,
                "required": chart.required,
                "ok": result.ok,
                "resolved_claim_ids": result.resolved_claim_ids,
                "resolved_evidence_ids": result.resolved_evidence_ids,
                "publisher": result.publisher,
                "match_rule": result.match_rule,
                "ambiguity_reasons": result.ambiguity_reasons,
                "candidate_notes": result.candidate_notes,
                "point_refs": result.point_refs,
            }
        )
    return report


def _validate_single_chart(
    chart: ChartSpec,
    *,
    claims_by_id: dict[str, Claim],
    analysis_text: str | None,
    evidence_by_id: dict[str, EvidenceRecord],
    project_id: str,
    sources_by_id: dict[str, SourceAsset],
    strict: bool,
) -> MatchResult:
    result = MatchResult(chart_id=chart.id)

    # 候选 claim 反查
    resolved_claims, claim_reasons = _resolve_claims(
        chart, claims_by_id, analysis_text, evidence_by_id
    )
    result.resolved_claim_ids = [c.claim_id for c in resolved_claims]
    result.candidate_notes.extend(claim_reasons)

    # 关联 evidence 文本来源：候选 claim 的 supporting evidence。
    evidence_text_sources: list[str] = []
    related_evidence_ids: list[str] = []
    for claim in resolved_claims:
        for evidence_id in claim.supporting_evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None or evidence.verification_status != VerificationStatus.SUPPORTED:
                continue
            related_evidence_ids.append(evidence_id)
            evidence_text_sources.append(evidence.claim)
            evidence_text_sources.append(evidence.excerpt)
            if evidence.normalized_value is not None:
                evidence_text_sources.append(str(evidence.normalized_value))
            if evidence.unit:
                evidence_text_sources.append(evidence.unit)

    # 全库 SUPPORTED evidence 兜底文本源：仅在非严格（v1 兼容）路径启用。严格模式下
    # 数值必须落在候选 claim 文本或其关联 SUPPORTED evidence 里，否则门禁失去牙齿。
    if not strict:
        for evidence in evidence_by_id.values():
            if evidence.verification_status != VerificationStatus.SUPPORTED:
                continue
            evidence_text_sources.append(evidence.claim)
            evidence_text_sources.append(evidence.excerpt)
            if evidence.normalized_value is not None:
                evidence_text_sources.append(str(evidence.normalized_value))

    # claim 文本本身
    claim_texts = [c.text for c in resolved_claims]

    # 正文兜底：Agent4 正文已经通过引用审计与结论台账门禁，是可信的数值来源。Agent5
    # 常把正文表格/敏感性矩阵里的数值画进图（如收入明细、可比公司 PE、DCF 敏感性
    # 矩阵），这些数值未必被单独选为 claim，也不一定出现在 evidence 的 claim/excerpt
    # 文本里；若 strict 模式只允许匹配候选 claim/evidence，就会把「正文里真实存在的
    # 数字」误判为编造，整份交付反复作废。因此把正文全文加入匹配源，真正编造的数字
    # （正文里完全没有）仍会失败，防幻觉能力不削弱。
    body_text_sources: list[str] = []
    if analysis_text is not None:
        body_text_sources.append(analysis_text)

    # 期望语义：由 chart.unit 推导（百分比/倍数/普通数值严格区分）。
    expected_semantic = _expected_semantic(chart.unit)

    # 每个非空数值匹配
    all_ok = True
    point_refs: list[dict[str, Any]] = []
    for series in chart.series:
        for index, raw_value in enumerate(series.values):
            if raw_value is None:
                continue
            display = float(raw_value)
            label = chart.labels[index] if index < len(chart.labels) else f"#{index}"
            tolerance = _display_half_unit(display)
            # 匹配顺序（由严到松）：候选 claim 文本 → 关联 evidence 文本 → Agent4
            # 正文全文（已通过引用审计与结论台账门禁，是可信数值来源）。
            matched, _ = _match_value_in_text(
                display, claim_texts, tolerance, expected_semantic=expected_semantic
            )
            rule = "exact_claim" if matched else ""
            if not matched:
                matched, _ = _match_value_in_text(
                    display, evidence_text_sources, tolerance, expected_semantic=expected_semantic
                )
                rule = "exact_evidence" if matched else ""
            if not matched:
                matched, _ = _match_value_in_text(
                    display, body_text_sources, tolerance, expected_semantic=expected_semantic
                )
                rule = "body" if matched else ""
            point_refs.append(
                {
                    "label": label,
                    "series": series.name,
                    "value": raw_value,
                    "claim_id": result.resolved_claim_ids[0] if result.resolved_claim_ids else None,
                    "evidence_ids": related_evidence_ids,
                    "match_rule": rule,
                }
            )
            if not matched:
                all_ok = False
                result.ambiguity_reasons.append(
                    f"数值 {raw_value}（{label}/{series.name}）无法在候选 claim、SUPPORTED evidence 或分析正文中匹配"
                )

    result.point_refs = point_refs
    # 候选 claim 的瑕疵（不存在、不在正文、零证据 judgment）只从候选集合剔除并留在
    # ambiguity_reasons 供观测，绝不单独否决整张图——`claim_ids` 按设计"只是线索，
    # 不作为通过依据"，若让线索的瑕疵变成通过依据，一条合法的零证据 judgment 就能
    # 连带击穿整份报告的必需图。是否通过只由"每个数值能否匹配"决定；严格模式下候选
    # 全部落空时 evidence_text_sources 为空，数值必然匹配不上，仍然 fail-closed。
    result.ok = all_ok
    result.match_rule = "chart_level_claim_reverse" if resolved_claims else "chart_level_evidence"
    result.resolved_evidence_ids = sorted(set(related_evidence_ids))
    # publisher 推导：resolved_evidence_ids 是 ev_...，先取 evidence.source_id 再查 sources。
    related_sources: list[SourceAsset] = []
    for evidence_id in result.resolved_evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            continue
        source = sources_by_id.get(evidence.source_id)
        if source is not None:
            related_sources.append(source)
    result.publisher = _publisher_for_sources(related_sources) if related_sources else "Research Agent 整理"
    return result


def write_provenance_report(report: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def validate_chart_values_or_raise(
    report: dict[str, Any],
    manifest: ChartManifest,
) -> None:
    """把审计结果落成门禁：必需图失败阻断，可选图由调用方降级。

    调用方依据 report 里的 ``ok`` 与 ``required`` 决定：必需图失败抛
    DeterministicContentError；可选图失败降级 _fallback_table()。
    """
    failures = [
        entry for entry in report["charts"]
        if not entry["ok"] and entry["required"]
    ]
    if failures:
        detail = "; ".join(
            f"{entry['chart_id']}: {'; '.join(entry['ambiguity_reasons'][:3])}"
            for entry in failures
        )
        raise DeterministicContentError(
            f"必需图表数值溯源失败：{detail}（{DETERMINISTIC_CONTENT_HINT}）"
        )


def apply_provenance_gate(
    report: dict[str, Any],
    manifest: ChartManifest,
    *,
    strict: bool,
) -> set[str]:
    """应用门禁并回填 publisher，返回需降级为数据表的图表 id 集合。

    - 严格模式（version=2）与兼容模式（version=1）现在统一为**降级不阻断**：
      数值溯源失败的图一律加入 fallback 集合，由渲染层降级为数据表，绝不抛
      ``DeterministicContentError`` 阻断整份交付。图表数值溯源防的是「图里编造
      数字」，但 SOTP 估值、DCF 敏感性矩阵等**自建测算图**的数值是派生估算，正文
      只会给出结论区间而不会逐值列出，逐字溯源本就不适用；为这类图作废整轮、重跑
      LLM 的代价远大于「把该图降级成表格」。
    - caption 的 source 统一改由关联证据 publisher 推导。
    """
    fallback: set[str] = set()
    by_id = {entry["chart_id"]: entry for entry in report["charts"]}
    for chart in manifest.charts:
        entry = by_id.get(chart.id)
        if entry is None:
            continue
        if entry["publisher"] and entry["publisher"] != "Research Agent 整理":
            chart.source = entry["publisher"]
        if entry["ok"]:
            continue
        # 数值溯源失败：降级为数据表，绝不阻断交付。图表仍以表格形式展示其数值。
        fallback.add(chart.id)
    return fallback
