"""P0/P1/P2 图表改造回归测试：渲染器确定性缺陷、数值溯源门禁、声明式标注层。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.chart_provenance import (
    _display_half_unit,
    _extract_values,
    apply_provenance_gate,
    validate_chart_provenance,
)
from research_agent.pipeline_errors import DeterministicContentError
from research_agent.report_charts import (
    ChartManifest,
    ChartSpec,
    load_chart_manifest,
    render_chart_manifest,
)
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.enums import VerificationStatus
from research_agent.sources.models import EvidenceRecord


def _series(name: str, values: list[float], kinds: list[str] | None = None) -> dict:
    return {
        "name": name,
        "values": values,
        "value_kind": kinds or ["actual"] * len(values),
    }


def _chart(chart_id: str, chart_type: str, series: list[dict] | None = None, **extra) -> dict:
    base = {
        "id": chart_id,
        "type": chart_type,
        "title": f"{chart_type} 测试图表",
        "unit": "亿元",
        "as_of_date": "2026-07-19",
        "source": "测试数据，Research Agent 整理",
        "labels": ["A", "B", "C"],
        "series": series or [_series("指标", [10, 13, 17])],
    }
    base.update(extra)
    return base


# ═══════════════════════════════════════════════════════════════
# P2：声明式标注层与新类型 schema 校验
# ═══════════════════════════════════════════════════════════════


def test_visual_fields_schema_validation() -> None:
    """visual 字段只允许声明的枚举，越界必须拒绝。"""
    good = _chart("v1", "bar", visual={"orientation": "horizontal", "number_format": "percent_1"})
    ChartSpec.model_validate(good)

    for field, bad_value in (
        ("orientation", "diagonal"),
        ("number_format", "currency_2"),
        ("legend_position", "bottom"),
    ):
        bad = _chart("v2", "bar", visual={field: bad_value})
        with pytest.raises(ValidationError):
            ChartSpec.model_validate(bad)


def test_reference_line_and_band_validation() -> None:
    """reference_lines / bands 只允许语义轴，value 必须有限，band 端点有序。"""
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(_chart("r1", "bar", reference_lines=[{"axis": "value"}]))
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(_chart("r2", "bar", reference_lines=[{"axis": "category", "value": 3}]))
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(_chart("r3", "bar", bands=[{"lower": 5, "upper": 2}]))
    # callout 只能绑定现有 label
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(_chart("r4", "bar", callouts=[{"label": "不在标签里", "text": "x"}]))


def test_range_bar_requires_two_series() -> None:
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(_chart("rb1", "range_bar", [_series("下限", [1, 2, 3])]))
    ok = ChartSpec.model_validate(
        _chart("rb2", "range_bar", [_series("下限", [1, 2, 3]), _series("上限", [3, 4, 5])])
    )
    assert ok.type == "range_bar"


def test_new_types_render(tmp_path: Path) -> None:
    """horizontal_bar 与 range_bar 纳入渲染回归（三种资产）。"""
    charts = [
        _chart("hb", "horizontal_bar", [_series("甲", [8, 11, 13])],
               labels=["很长的公司名称一", "很长的公司名称二", "很长的公司名称三"]),
        _chart("rb", "range_bar", [_series("下限", [1, 2, 3]), _series("上限", [3, 5, 7])]),
    ]
    manifest = ChartManifest.model_validate({"version": 2, "charts": charts})
    assets = render_chart_manifest(manifest, tmp_path / "charts")
    assert set(assets) == {"hb", "rb"}
    for asset in assets.values():
        assert asset.svg_path.stat().st_size > 1_000
        assert asset.pdf_path.stat().st_size > 1_000
        assert asset.png_path.stat().st_size > 5_000


def test_duplicate_anchor_blocks(tmp_path: Path) -> None:
    """同一 placement_after 出现多张图必须抛 DeterministicContentError。"""
    a = _chart("a", "bar", placement_after="## 章节 5")
    b = _chart("b", "line", placement_after="## 章节 5")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 2, "charts": [a, b]}), encoding="utf-8")
    with pytest.raises(DeterministicContentError, match="共享同一 placement_after"):
        load_chart_manifest(path)


def test_compose_anchor_mismatch_degrades_to_end(tmp_path: Path) -> None:
    """锚点匹配不到时降级到文末插入，不再作废整轮。

    锚点只是排版提示，不是事实正确性门禁。模型偶发多打/少打 ``#`` 或复制到行首
    缩进就作废整轮、重跑 LLM，代价远大于图表位置略有偏移。
    """
    from research_agent.agents.formatter import _compose_final_report_from_analysis

    analysis = tmp_path / "04_analysis.md"
    report = tmp_path / "05_final_report.md"
    analysis.write_text("# 行业分析\n\n## 供需格局\n", encoding="utf-8")
    manifest = ChartManifest.model_validate(
        {
            "version": 1,
            "charts": [
                {
                    "id": "c",
                    "type": "line",
                    "title": "t",
                    "unit": "%",
                    "as_of_date": "2026-08-17",
                    "source": "s",
                    "labels": ["2025"],
                    "series": [{"name": "增速", "values": [8], "value_kind": ["actual"]}],
                    "placement_after": "## 不存在的锚点",
                }
            ],
        }
    )
    _compose_final_report_from_analysis(analysis, report, manifest)
    # 图表占位符仍被插入（降级到文末），报告得以交付
    assert "{{chart:c}}" in report.read_text(encoding="utf-8")


def test_compose_anchor_ignores_leading_indentation(tmp_path: Path) -> None:
    """锚点复制时丢掉行首缩进不应阻断：正文 ``  **加粗行**`` 与锚点 ``**加粗行**`` 一致。

    修复前用 ``line.rstrip("\\r\\n") == anchor`` 只去行尾空白，行首缩进会让
    加粗正文行（常见于"**DCF 敏感性矩阵...**"这类非标题锚点）匹配 0 行、整轮作废。
    """
    from research_agent.agents.formatter import _compose_final_report_from_analysis

    analysis = tmp_path / "04_analysis.md"
    report = tmp_path / "05_final_report.md"
    analysis.write_text(
        "# 分析\n\n  **DCF 敏感性矩阵：**\n\n正文内容\n", encoding="utf-8"
    )
    manifest = ChartManifest.model_validate(
        {
            "version": 1,
            "charts": [
                {
                    "id": "c",
                    "type": "line",
                    "title": "t",
                    "unit": "%",
                    "as_of_date": "2026-08-17",
                    "source": "s",
                    "labels": ["2025"],
                    "series": [{"name": "增速", "values": [8], "value_kind": ["actual"]}],
                    "placement_after": "**DCF 敏感性矩阵：**",
                }
            ],
        }
    )
    _compose_final_report_from_analysis(analysis, report, manifest)
    assert "{{chart:c}}" in report.read_text(encoding="utf-8")


def test_reuse_check_accepts_manifest_with_unmatched_anchor(tmp_path: Path) -> None:
    """锚点匹配不到不再阻止复用：清单可解析且时间戳新于正文即可复用。

    锚点位置漂移由 ``_resolve_anchor_index`` 降级处理，不构成复用障碍，避免
    重新调用 LLM 生成清单。
    """
    from research_agent.agents.formatter import _can_reuse_chart_manifest

    analysis = tmp_path / "04_analysis.md"
    analysis.write_text("# 行业分析\n\n## 供需格局\n", encoding="utf-8")
    manifest_path = tmp_path / "05_chart_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "charts": [
                    {
                        "id": "c",
                        "type": "line",
                        "title": "t",
                        "unit": "%",
                        "as_of_date": "2026-08-17",
                        "source": "s",
                        "labels": ["2025"],
                        "series": [{"name": "增速", "values": [8], "value_kind": ["actual"]}],
                        "placement_after": "## 不存在的锚点",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    import os

    os.utime(manifest_path, (analysis.stat().st_mtime + 10, analysis.stat().st_mtime + 10))
    assert _can_reuse_chart_manifest(manifest_path, analysis) is True


# ═══════════════════════════════════════════════════════════════
# P1：数值溯源匹配原型 fixture
# ═══════════════════════════════════════════════════════════════


def test_display_half_unit_tolerance() -> None:
    assert _display_half_unit(9.3) == Decimal("0.05")
    assert _display_half_unit(192.87) == Decimal("0.005")
    assert _display_half_unit(8.0) == Decimal("0.5")
    assert _display_half_unit(35.0) == Decimal("0.5")


def test_extract_thousands_chinese_magnitude_negative() -> None:
    values = _extract_values("营收 1,928.7 亿元，同比 -9.3%，约 2.5 万件")
    plain = [v for v, s in values if s == "plain"]
    assert Decimal("1928.7") in plain  # 千分位去除
    assert Decimal("-9.3") in [v for v, s in values if s == "percent"]
    assert Decimal("2.5") in plain  # 单位不作为数值放大


def test_percent_vs_pct_are_not_equivalent() -> None:
    """0.12 / 12% / +12pct 严格区分，不自动等价——断言匹配行为而非仅提取集合。"""
    values = _extract_values("0.12 12% +12pct")
    plain = {v for v, s in values if s == "plain"}
    percents = {v for v, s in values if s == "percent"}
    pcts = {v for v, s in values if s == "pct"}
    assert Decimal("0.12") in plain
    assert Decimal("12") in percents
    assert Decimal("12") in pcts
    assert Decimal("0.12") not in percents


def test_semantic_matching_three_cases() -> None:
    """unit="%"、显示值 12 的三个实测用例：百分点 False、裸数 False、百分比 True。"""
    from research_agent.chart_provenance import _match_value_in_text, _display_half_unit

    tolerance = _display_half_unit(12.0)
    # 百分点 → 拒绝
    assert _match_value_in_text(
        12.0, ["提升 12 个百分点"], tolerance, expected_semantic="percent"
    )[0] is False
    # 裸数 → 拒绝
    assert _match_value_in_text(
        12.0, ["编号 12 号"], tolerance, expected_semantic="percent"
    )[0] is False
    # 百分比 → 接受
    assert _match_value_in_text(
        12.0, ["增速 12%"], tolerance, expected_semantic="percent"
    )[0] is True


def test_semantic_multiple_allows_plain_enumeration() -> None:
    """unit="x" 时显式允许 plain 命中（正文常写 10.3/8.7/7.7 不带 x 的倍数枚举）。"""
    from research_agent.chart_provenance import _match_value_in_text, _display_half_unit

    tolerance = _display_half_unit(10.3)
    assert _match_value_in_text(
        10.3, ["10.3/8.7/7.7"], tolerance, expected_semantic="multiple"
    )[0] is True
    assert _match_value_in_text(
        10.3, ["10.3x"], tolerance, expected_semantic="multiple"
    )[0] is True


def _setup_project(tmp_path: Path, *, with_claims: bool):
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    source = service.register_bytes("project", "report.txt", b"Revenue was 42 million").source
    source.source_tier = "S"
    repository.update_source(source)
    service.parse_source("project", source.source_id)
    service.index_source("project", source.source_id)
    chunk = repository.all_chunks("project")[0]
    evidence = EvidenceRecord(
        evidence_id="ev_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        project_id="project",
        research_question_id="q1",
        claim="Revenue was 42 million",
        normalized_value="42",
        unit="百万美元",
        period="2025",
        source_id=source.source_id,
        source_version=source.version,
        chunk_id=chunk.chunk_id,
        locator=chunk.locators[0],
        excerpt="Revenue was 42 million",
        source_tier="S",
        verification_status=VerificationStatus.SUPPORTED,
        confidence=1,
    )
    service.record_evidence(evidence)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "04_analysis.md").write_text(
        "# 分析\n\nRevenue was 42 million\n\n营收 42 百万美元。\n", encoding="utf-8"
    )
    if with_claims:
        (project_dir / "04_claims.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "claims": [
                        {
                            "claim_id": "c1",
                            "question_id": "q1",
                            "kind": "fact",
                            "importance": "critical",
                            "text": "营收 42 百万美元",
                            "supporting_evidence_ids": ["ev_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return repository, project_dir


def test_provenance_matches_value_from_claim_and_evidence(tmp_path: Path) -> None:
    repository, project_dir = _setup_project(tmp_path, with_claims=True)
    try:
        chart = _chart(
            "c", "bar", [_series("营收", [42])], labels=["2025"],
            provenance={"claim_ids": ["c1"]},
        )
        manifest = ChartManifest.model_validate({"version": 2, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=project_dir / "04_claims.json",
        )
        assert report["charts"][0]["ok"] is True
        assert report["charts"][0]["resolved_claim_ids"] == ["c1"]
        assert report["charts"][0]["match_rule"] == "chart_level_claim_reverse"
    finally:
        repository.close()


def test_zero_evidence_judgment_claim_does_not_fail_chart(tmp_path: Path) -> None:
    """零证据 judgment 是合法台账条目，只从候选剔除，不得否决整张图。

    修复前：`result.ok = all_ok and not claim_reasons`，候选里任何一条不可反查的
    claim 都会让整张图判失败。实测快手 24 条 claim 中仅 1 条零证据 judgment（c17），
    却让 6 张必需图全部 ok=False，一旦 Agent5 写出 v2 清单就会阻断整份报告交付。
    """
    repository, project_dir = _setup_project(tmp_path, with_claims=True)
    try:
        # 追加一条零证据 judgment，模拟真实台账
        claims_path = project_dir / "04_claims.json"
        data = json.loads(claims_path.read_text(encoding="utf-8"))
        data["claims"].append(
            {
                "claim_id": "c9",
                "question_id": "q1",
                "kind": "judgment",
                "importance": "major",
                "text": "营收 42 百万美元",
                "supporting_evidence_ids": [],
            }
        )
        claims_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        chart = _chart(
            "c", "bar", [_series("营收", [42])], labels=["2025"],
            provenance={"claim_ids": ["c1", "c9"]},
        )
        manifest = ChartManifest.model_validate({"version": 2, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=claims_path,
        )
        entry = report["charts"][0]
        # 数值仍能从 c1 匹配 → 通过；c9 被剔除但原因留在 candidate_notes 供观测
        assert entry["ok"] is True
        assert entry["resolved_claim_ids"] == ["c1"]
        assert any("零证据 judgment" in note for note in entry["candidate_notes"])
        # 候选解析观测不得混进阻断原因，否则门禁错误信息会指向非根因
        assert entry["ambiguity_reasons"] == []
    finally:
        repository.close()


def test_unresolvable_candidate_alone_still_fails_when_value_unmatched(
    tmp_path: Path,
) -> None:
    """候选 claim 落空但数值仍在正文中时应通过——claim_ids 只是线索不是通过依据。

    修复前 strict 模式只允许匹配候选 claim/evidence，候选落空就让正文里真实存在的
    数字被误判为编造。现在正文（已通过 Agent4 门禁）兜底匹配，数值 42 在正文里，
    因此通过；候选瑕疵只留在 candidate_notes 供观测。
    """
    repository, project_dir = _setup_project(tmp_path, with_claims=True)
    try:
        chart = _chart(
            "c", "bar", [_series("营收", [42])], labels=["2025"],
            provenance={"claim_ids": ["c_missing"]},
        )
        manifest = ChartManifest.model_validate({"version": 2, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=project_dir / "04_claims.json",
        )
        entry = report["charts"][0]
        assert entry["ok"] is True
        assert entry["match_rule"] == "chart_level_evidence"
        assert any("c_missing" in note for note in entry["candidate_notes"])
        # 数值 42 通过正文兜底匹配到
        assert any(p["match_rule"] == "body" for p in entry["point_refs"])
    finally:
        repository.close()


def test_strict_mode_body_fallback_still_rejects_fabricated_value(
    tmp_path: Path,
) -> None:
    """正文兜底不削弱防幻觉：正文里完全没有的数字仍须 fail-closed。"""
    repository, project_dir = _setup_project(tmp_path, with_claims=True)
    try:
        chart = _chart(
            "c", "bar", [_series("营收", [999])], labels=["2025"],
            provenance={"claim_ids": ["c_missing"]},
        )
        manifest = ChartManifest.model_validate({"version": 2, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=project_dir / "04_claims.json",
        )
        entry = report["charts"][0]
        assert entry["ok"] is False
        assert any("无法在候选 claim" in note for note in entry["ambiguity_reasons"])
    finally:
        repository.close()


def test_strict_mode_does_not_use_full_library_fallback(tmp_path: Path) -> None:
    """strict 模式下数值必须落在候选 claim 文本或该 claim 关联 evidence 里。"""
    repository, project_dir = _setup_project(tmp_path, with_claims=True)
    try:
        # 该证据库只有一条 evidence（42），值 999 不在候选 claim 里，也不在 evidence 里。
        chart = _chart(
            "c", "bar", [_series("营收", [999])], labels=["2025"],
            provenance={"claim_ids": ["c1"]},
        )
        manifest = ChartManifest.model_validate({"version": 2, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=project_dir / "04_claims.json",
        )
        assert report["charts"][0]["ok"] is False
    finally:
        repository.close()


def test_publisher_derived_from_evidence_source(tmp_path: Path) -> None:
    """publisher 由 evidence.source_id 反查 sources_by_id 得到，不再恒为兜底值。"""
    from research_agent.sources.models import SourceAsset
    from research_agent.sources.enums import SourceStatus

    repository, project_dir = _setup_project(tmp_path, with_claims=True)
    try:
        # 给 evidence 的 source 设 publisher
        source = repository.list_sources("project", include_superseded=True)[0]
        source.publisher = "示例发布方"
        repository.update_source(source)
        chart = _chart(
            "c", "bar", [_series("营收", [42])], labels=["2025"],
            provenance={"claim_ids": ["c1"]},
        )
        manifest = ChartManifest.model_validate({"version": 2, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=project_dir / "04_claims.json",
        )
        assert report["charts"][0]["publisher"] == "示例发布方"
    finally:
        repository.close()


def test_provenance_fabricated_value_degrades_to_table(tmp_path: Path) -> None:
    repository, project_dir = _setup_project(tmp_path, with_claims=True)
    try:
        chart = _chart(
            "c", "bar", [_series("营收", [999])], labels=["2025"],
            provenance={"claim_ids": ["c1"]},
        )
        manifest = ChartManifest.model_validate({"version": 2, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=project_dir / "04_claims.json",
        )
        assert report["charts"][0]["ok"] is False
        # 数值溯源失败不再阻断整份交付：该图降级为数据表。
        fallback = apply_provenance_gate(report, manifest, strict=True)
        assert fallback == {"c"}
    finally:
        repository.close()


def test_v1_manifest_is_compatible_without_claims(tmp_path: Path) -> None:
    """v1 清单不强制 claim_ids，兼容历史项目。"""
    repository, project_dir = _setup_project(tmp_path, with_claims=False)
    try:
        chart = _chart("c", "bar", [_series("营收", [42])], labels=["2025"])
        manifest = ChartManifest.model_validate({"version": 1, "charts": [chart]})
        report = validate_chart_provenance(
            manifest, project_dir=project_dir, repository=repository,
            analysis_path=project_dir / "04_analysis.md",
            claims_path=None,
        )
        # v1 兼容：数值仍须能从 evidence 匹配，但不阻断
        assert report["charts"][0]["ok"] is True
        assert apply_provenance_gate(report, manifest, strict=False) == set()
    finally:
        repository.close()
