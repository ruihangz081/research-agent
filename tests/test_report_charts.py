import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.llm.types import LLMResponse
from research_agent.report_charts import (
    ChartManifest,
    ChartSpec,
    load_chart_manifest,
    prepare_llm_fallbacks,
    render_chart_manifest,
    validate_vega_lite_spec,
)


def _series(name: str, values: list[float], kinds: list[str] | None = None) -> dict:
    return {
        "name": name,
        "values": values,
        "value_kind": kinds or ["actual"] * len(values),
    }


def _chart(chart_id: str, chart_type: str, series: list[dict] | None = None) -> dict:
    return {
        "id": chart_id,
        "type": chart_type,
        "title": f"{chart_type} 测试图表",
        "unit": "亿元",
        "as_of_date": "2026-07-19",
        "source": "测试数据，Research Agent 整理",
        "labels": ["A", "B", "C"],
        "series": series or [_series("指标", [10, 13, 17])],
    }


def test_manifest_validation_rejects_bad_shapes_and_resources(tmp_path: Path) -> None:
    invalid = _chart("bad", "line")
    invalid["series"][0]["values"] = [1, 2]
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(invalid)
    invalid = _chart("bad", "line")
    invalid["source"] = "https://example.com/data.csv"
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(invalid)

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "charts": [_chart(f"c{i}", "bar") for i in range(3)]}), encoding="utf-8")
    with pytest.raises(ValueError, match="超过上限"):
        load_chart_manifest(path, max_charts=2)


def test_matrix_heatmap_normalized_to_series(tmp_path: Path) -> None:
    """模型输出矩阵式 heatmap（x_labels/y_labels/values）时应被重组为契约结构。

    数值只做结构重排、不换算不新增，后续数值溯源门禁仍逐值校验。
    """
    raw = {
        "id": "sens",
        "type": "heatmap",
        "title": "敏感性",
        "unit": "港元",
        "as_of_date": "2026-08-25",
        "source": "自建模型",
        "placement_after": "## 章节 4",
        "x_labels": ["1.5%", "2.0%", "2.5%"],
        "y_labels": ["10%", "12%"],
        "values": [[42, 48, 55], [32, 36, 40]],
        "provenance": {"claim_ids": ["c1"]},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 2, "charts": [raw]}, ensure_ascii=False), encoding="utf-8")
    manifest = load_chart_manifest(path)
    chart = manifest.charts[0]
    assert chart.labels == ["1.5%", "2.0%", "2.5%"]
    assert [series.name for series in chart.series] == ["10%", "12%"]
    assert [series.values for series in chart.series] == [[42, 48, 55], [32, 36, 40]]
    assert all(series.value_kind == ["estimate"] * 3 for series in chart.series)


def test_single_series_range_bar_normalized_to_two_series(tmp_path: Path) -> None:
    """模型输出单 series 内嵌 range_lower/range_upper 时应重组为两个 series。"""
    raw = {
        "id": "range",
        "type": "range_bar",
        "title": "目标价区间",
        "unit": "港元",
        "as_of_date": "2026-08-25",
        "source": "三方法合成",
        "placement_after": "## 章节 6",
        "labels": ["熊市", "基准", "牛市"],
        "series": [
            {
                "name": "目标价区间",
                "values": [None, None, None],
                "range_lower": [28, 38, 50],
                "range_upper": [35, 45, 80],
                "value_kind": ["forecast", "forecast", "forecast"],
            }
        ],
        "provenance": {"claim_ids": ["c9"]},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 2, "charts": [raw]}, ensure_ascii=False), encoding="utf-8")
    manifest = load_chart_manifest(path)
    chart = manifest.charts[0]
    assert [series.name for series in chart.series] == ["下限", "上限"]
    assert [series.values for series in chart.series] == [[28, 38, 50], [35, 45, 80]]


def test_all_seven_deterministic_chart_types_render(tmp_path: Path) -> None:
    charts = [
        _chart("line", "line", [_series("规模", [10, 13, 18], ["actual", "actual", "forecast"])]),
        _chart("bar", "bar", [_series("甲", [8, 11, 13]), _series("乙", [6, 10, 15])]),
        _chart("stack", "stacked_bar", [_series("甲", [8, 9, 10]), _series("乙", [2, 4, 6])]),
        _chart("combo", "combo", [_series("收入", [10, 13, 17]), {**_series("增速", [8, 30, 31]), "axis": "right"}]),
        _chart("scatter", "scatter", [_series("收入", [10, 13, 17]), _series("利润率", [5, 8, 7])]),
        _chart("heat", "heatmap", [_series("低", [80, 90, 100]), _series("高", [95, 105, 120])]),
        _chart("waterfall", "waterfall", [_series("贡献", [10, -3, 6])]),
    ]
    manifest = ChartManifest.model_validate({"version": 1, "charts": charts})
    assets = render_chart_manifest(manifest, tmp_path / "charts")
    assert set(assets) == {chart["id"] for chart in charts}
    for asset in assets.values():
        assert asset.svg_path.stat().st_size > 1_000
        assert asset.pdf_path.stat().st_size > 1_000
        assert asset.png_path.stat().st_size > 5_000


def test_vega_lite_security_validation() -> None:
    chart = ChartSpec.model_validate(_chart("facet", "facet_bar"))
    safe = {
        "data": {"name": "reportData"},
        "mark": "bar",
        "encoding": {
            "x": {"field": "label", "type": "nominal"},
            "y": {"field": "指标", "type": "quantitative"},
        },
    }
    assert validate_vega_lite_spec(safe, chart) == safe
    with pytest.raises(ValueError, match="data"):
        validate_vega_lite_spec({**safe, "data": {"url": "https://example.com/a.json"}}, chart)
    with pytest.raises(ValueError, match="禁止字段"):
        validate_vega_lite_spec({**safe, "transform": [{"calculate": "datum.x * 2"}]}, chart)
    with pytest.raises(ValueError, match="禁止字段"):
        validate_vega_lite_spec({**safe, "data": {"name": "reportData"}, "encoding": {"color": {"values": ["red"]}}}, chart)


@pytest.mark.anyio
async def test_llm_fallback_gets_one_repair_attempt(tmp_path: Path) -> None:
    chart = _chart("facet", "facet_bar")
    manifest = ChartManifest.model_validate({"version": 1, "charts": [chart]})

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content='{"data":{"url":"https://example.com"},"mark":"bar"}')
            return LLMResponse(content=json.dumps({
                "data": {"name": "reportData"},
                "mark": "bar",
                "encoding": {
                    "x": {"field": "label", "type": "nominal"},
                    "y": {"field": "指标", "type": "quantitative"},
                },
            }, ensure_ascii=False))

    client = FakeClient()
    await prepare_llm_fallbacks(manifest, project_dir=tmp_path, client=client)
    assert client.calls == 2
    assert manifest.charts[0].vega_lite_spec is not None
    assert (tmp_path / "05_chart_specs/facet.validated.json").is_file()
