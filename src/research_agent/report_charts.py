"""Validated chart manifests and deterministic brokerage-style rendering."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator, model_validator

from . import config
from .agent_skills import load_project_skill
from .llm.types import ChatMessage
from .pipeline_errors import DETERMINISTIC_CONTENT_HINT, DeterministicContentError

if TYPE_CHECKING:
    from .llm import LLMClient

SUPPORTED_CHART_TYPES = {
    "line",
    "bar",
    "stacked_bar",
    "combo",
    "scatter",
    "heatmap",
    "waterfall",
    "horizontal_bar",
    "range_bar",
}
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FORBIDDEN_SPEC_KEYS = {
    "url",
    "href",
    "calculate",
    "expr",
    "signal",
    "config",
    "datasets",
    "transform",
    "values",
}
_ALLOWED_MARKS = {"bar", "line", "point", "area", "rule", "rect", "text", "tick"}


class ChartVisual(BaseModel):
    """声明式视觉参数（不包含任何可执行绘图代码）。"""

    orientation: Literal["auto", "vertical", "horizontal"] = "auto"
    show_values: bool = True
    highlight_labels: list[str] = Field(default_factory=list)
    highlight_series: list[str] = Field(default_factory=list)
    number_format: Literal["auto", "integer", "decimal_1", "percent_1", "multiple_1"] = "auto"
    legend_position: Literal["auto", "top", "right", "none"] = "auto"


class ReferenceLine(BaseModel):
    """语义参考线：只允许固定数值或绑定现有 label，禁止物理画布坐标。"""

    axis: Literal["value", "category"] = "value"
    value: float | None = None
    category: str | None = None
    label: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def validate_anchor(self) -> "ReferenceLine":
        if self.axis == "value" and self.value is None:
            raise ValueError("value 轴参考线必须提供 value")
        if self.axis == "category" and not self.category:
            raise ValueError("category 轴参考线必须提供 category")
        if self.axis == "value" and self.value is not None and not math.isfinite(self.value):
            raise ValueError("参考线 value 必须为有限数值")
        return self


class Band(BaseModel):
    """区间带：预测区间、合理估值区间、阈值区间。"""

    axis: Literal["value", "category"] = "value"
    lower: float | None = None
    upper: float | None = None
    label: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def validate_band(self) -> "Band":
        if self.lower is None or self.upper is None:
            raise ValueError("band 必须提供 lower 与 upper")
        if self.lower >= self.upper:
            raise ValueError("band 的 lower 必须小于 upper")
        return self


class Callout(BaseModel):
    """点注释：绑定现有 label 或数值点，禁止任意坐标。"""

    label: str = Field(min_length=1, max_length=120)
    value: float | None = None
    text: str = Field(min_length=1, max_length=200)


class ChartProvenance(BaseModel):
    """图表候选 claim 集合——仅作待核验线索，不是通过依据。"""

    claim_ids: list[str] = Field(default_factory=list)


class ChartSeries(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    values: list[float | None] = Field(min_length=1)
    value_kind: list[Literal["actual", "forecast", "estimate"]] = Field(min_length=1)
    x_values: list[float | None] | None = None
    axis: Literal["left", "right"] = "left"

    @model_validator(mode="after")
    def validate_lengths(self) -> "ChartSeries":
        if len(self.value_kind) != len(self.values):
            raise ValueError("series.value_kind 与 values 长度必须一致")
        if self.x_values is not None and len(self.x_values) != len(self.values):
            raise ValueError("series.x_values 与 values 长度必须一致")
        return self


class ChartSpec(BaseModel):
    id: str
    type: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=160)
    unit: str = Field(min_length=1, max_length=40)
    as_of_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source: str = Field(min_length=1, max_length=300)
    placement_after: str | None = Field(default=None, max_length=500)
    labels: list[str] = Field(min_length=1)
    series: list[ChartSeries] = Field(min_length=1)
    note: str = Field(default="", max_length=300)
    required: bool = True
    vega_lite_spec: dict[str, Any] | None = None
    visual: ChartVisual = Field(default_factory=ChartVisual)
    reference_lines: list[ReferenceLine] = Field(default_factory=list)
    bands: list[Band] = Field(default_factory=list)
    callouts: list[Callout] = Field(default_factory=list)
    provenance: ChartProvenance | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("chart.id 只能包含小写字母、数字、下划线和连字符")
        return value

    @field_validator("type")
    @classmethod
    def normalize_type(cls, value: str) -> str:
        return value.strip().lower().replace("-", "_")

    @field_validator("title", "unit", "source", "note")
    @classmethod
    def reject_unsafe_text(cls, value: str) -> str:
        lowered = value.lower()
        if "://" in lowered or "javascript:" in lowered or "file:" in lowered:
            raise ValueError("图表文本不得包含 URL 或文件资源")
        return value.strip()

    @field_validator("placement_after")
    @classmethod
    def normalize_placement_after(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_shape(self) -> "ChartSpec":
        if len(set(self.labels)) != len(self.labels):
            raise ValueError(f"chart {self.id} 的 labels 不得重复")
        for series in self.series:
            if len(series.values) != len(self.labels):
                raise ValueError(f"chart {self.id} 的 labels 与 series.values 长度不一致")
        names = [series.name for series in self.series]
        if len(names) != len(set(names)):
            raise ValueError(f"chart {self.id} 的 series.name 必须唯一")
        if self.type == "range_bar" and len(self.series) != 2:
            raise ValueError(f"range_bar {self.id} 必须恰好两个 series（lower / upper）")
        known_labels = set(self.labels)
        for callout in self.callouts:
            if callout.label not in known_labels:
                raise ValueError(f"chart {self.id} 的 callout.label 不在 labels 中：{callout.label}")
        for highlight in self.visual.highlight_labels:
            if highlight not in known_labels:
                raise ValueError(f"chart {self.id} 的 highlight_labels 不在 labels 中：{highlight}")
        known_series = set(names)
        for highlight in self.visual.highlight_series:
            if highlight not in known_series:
                raise ValueError(f"chart {self.id} 的 highlight_series 不在 series 中：{highlight}")
        for ref in self.reference_lines:
            if ref.axis == "category" and ref.category not in known_labels:
                raise ValueError(f"chart {self.id} 的 category 参考线不在 labels 中：{ref.category}")
        return self


class ChartManifest(BaseModel):
    version: Literal[1, 2] = 1
    charts: list[ChartSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ids(self) -> "ChartManifest":
        ids = [chart.id for chart in self.charts]
        if len(ids) != len(set(ids)):
            raise ValueError("chart.id 必须唯一")
        return self


@dataclass(frozen=True)
class ChartAsset:
    chart_id: str
    svg_path: Path
    pdf_path: Path
    png_path: Path


def _normalize_chart_dict(chart: dict[str, Any]) -> dict[str, Any]:
    """把模型偶发的「语义等价但结构不同」的图表条目归一化为 schema 契约结构。

    模型常凭想象输出两种非契约结构，而契约要求的是另一种布局：
    - heatmap 常被写成矩阵式 ``x_labels`` + ``y_labels`` + ``values``，契约要求
      ``labels``（列）+ 每行一个 ``series``（name 为行标签）；
    - range_bar 常被写成单个 series 内嵌 ``range_lower``/``range_upper``，契约要求
      恰好两个 series（下限 / 上限）。

    这两种格式的数值都来自正文、只差结构，硬校验 fail-closed 会让整份交付反复
    作废。因此在加载层做确定性重组：只重新布局模型原始数值，不生成、不换算任何
    新数字，后续的数值溯源门禁仍逐值校验，防幻觉能力不受影响。
    """
    normalized = dict(chart)
    chart_type = str(normalized.get("type", "")).strip().lower().replace("-", "_")

    if chart_type == "heatmap" and "labels" not in normalized:
        x_labels = normalized.get("x_labels")
        y_labels = normalized.get("y_labels")
        values = normalized.get("values")
        if (
            isinstance(x_labels, list)
            and isinstance(y_labels, list)
            and isinstance(values, list)
            and len(values) == len(y_labels)
        ):
            series: list[dict[str, Any]] = []
            for row_name, row_values in zip(y_labels, values):
                if isinstance(row_values, list):
                    series.append(
                        {
                            "name": str(row_name),
                            "values": list(row_values),
                            "value_kind": ["estimate"] * len(row_values),
                        }
                    )
            normalized["labels"] = list(x_labels)
            normalized["series"] = series
            normalized.pop("x_labels", None)
            normalized.pop("y_labels", None)
            normalized.pop("values", None)

    if chart_type == "range_bar":
        series = normalized.get("series")
        if isinstance(series, list) and len(series) == 1 and isinstance(series[0], dict):
            only = series[0]
            lower = only.get("range_lower")
            upper = only.get("range_upper")
            if (
                isinstance(lower, list)
                and isinstance(upper, list)
                and len(lower) == len(upper)
            ):
                kinds = only.get("value_kind")
                if not isinstance(kinds, list) or len(kinds) != len(lower):
                    kinds = ["forecast"] * len(lower)
                normalized["series"] = [
                    {"name": "下限", "values": list(lower), "value_kind": list(kinds)},
                    {"name": "上限", "values": list(upper), "value_kind": list(kinds)},
                ]
            for item in normalized["series"]:
                if isinstance(item, dict):
                    item.pop("range_lower", None)
                    item.pop("range_upper", None)

    return normalized


def load_chart_manifest(path: Path, *, max_charts: int | None = None) -> ChartManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取图表清单 {path}: {exc}") from exc
    # 加载前先做确定性归一化，把矩阵式 heatmap / 单序列 range_bar 重组为契约结构。
    if isinstance(data, dict):
        charts = data.get("charts")
        if isinstance(charts, list):
            data["charts"] = [
                _normalize_chart_dict(chart) if isinstance(chart, dict) else chart
                for chart in charts
            ]
    manifest = ChartManifest.model_validate(data)
    limit = config.REPORT_MAX_CHARTS if max_charts is None else max_charts
    if len(manifest.charts) > limit:
        raise ValueError(f"图表数量 {len(manifest.charts)} 超过上限 {limit}")
    # 同一 placement_after 出现多张图时，读者会在同一锚点后连撞两张图。仅对 v2
    # 新输出阻断；v1 历史清单允许继续渲染（旧项目存在实测撞车，按兼容策略放行，
    # 重新运行 Agent5 时输出 v2 才会被这道门拦下）。
    if manifest.version == 2:
        anchors: dict[str, str] = {}
        for chart in manifest.charts:
            if not chart.placement_after:
                continue
            previous = anchors.get(chart.placement_after)
            if previous is not None and previous != chart.id:
                raise DeterministicContentError(
                    f"图表 {previous} 与 {chart.id} 共享同一 placement_after 锚点："
                    f"{chart.placement_after!r}（{DETERMINISTIC_CONTENT_HINT}）"
                )
            anchors[chart.placement_after] = chart.id
    return manifest


def _load_theme() -> dict[str, Any]:
    """从 design tokens 派生图表主题（键名保持历史契约，_draw_matplotlib 不改）。

    旧实现读 skills/.../assets/theme.json；P1-B 起改为读单一来源
    design-tokens.json，返回 dict 键名不变：colors / forecast_color / grid_color /
    text_color / muted_color / background_color / font_candidates /
    figure_width_inches / figure_height_inches / dpi。
    """
    from .design_tokens import load_design_tokens

    tokens = load_design_tokens()
    color = tokens.color
    chart = tokens.chart
    return {
        "name": tokens.name,
        "colors": list(color["series"]),
        "forecast_color": color["forecast"],
        "grid_color": color["grid"],
        "text_color": color["text"],
        "muted_color": color["muted"],
        "background_color": color["surface"],
        "font_candidates": list(tokens.font["chart_candidates"]),
        "figure_width_inches": chart["width_inches"],
        "figure_height_inches": chart["height_inches"],
        "dpi": chart["dpi"],
    }


def _safe_values(values: list[float | None]) -> list[float]:
    return [math.nan if value is None else float(value) for value in values]


def _format_axis(value: float, _position: int) -> str:
    if math.isnan(value):
        return ""
    absolute = abs(value)
    if absolute >= 1000:
        return f"{value:,.0f}"
    if absolute >= 10:
        return f"{value:,.1f}".rstrip("0").rstrip(".")
    return f"{value:,.2f}".rstrip("0").rstrip(".")


#: 数值标签格式：按 unit 语义选择（百分数/倍数/整数/一位小数），不依赖物理轴。
_PERCENT_UNITS = ("%", "％", "pct", "percent", "同比", "同比增速", "同比变动")
_MULTIPLE_UNITS = ("倍", "x", "X", "PE", "P/E", "P/S", "P/ARR", "EV/EBITDA")


def _is_percent_unit(unit: str) -> bool:
    return any(token in unit for token in _PERCENT_UNITS)


def _is_multiple_unit(unit: str) -> bool:
    return any(token in unit for token in _MULTIPLE_UNITS)


def _format_value_label(value: float, unit: str, number_format: str) -> str:
    """按显示语义格式化关键值标签；仅用于整数值标签，不用于坐标轴刻度。"""
    if math.isnan(value):
        return ""
    if number_format == "percent_1" or (number_format == "auto" and _is_percent_unit(unit)):
        return f"{value:.1f}%"
    if number_format == "multiple_1" or (number_format == "auto" and _is_multiple_unit(unit)):
        return f"{value:.1f}x"
    if number_format == "decimal_1":
        return f"{value:.1f}"
    if number_format == "integer":
        return f"{value:,.0f}"
    # auto：整数单位走千分位整数，其余一位小数（保留一位便于对齐）
    absolute = abs(value)
    if absolute >= 1000 or float(absolute).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.1f}"


def _resolve_orientation(chart: ChartSpec) -> str:
    """auto：按标签总宽度阈值自动切 horizontal（长标签/多类别自动横向）。"""
    if chart.type == "horizontal_bar":
        return "horizontal"
    if chart.visual.orientation != "auto":
        return chart.visual.orientation
    total_width = sum(len(label) for label in chart.labels)
    max_label = max((len(label) for label in chart.labels), default=0)
    # 类别多或标签总宽超阈值 → 横向；否则竖向。
    if len(chart.labels) > 6 or total_width > 36 or max_label > 8:
        return "horizontal"
    return "vertical"


def _legend_loc(position: str) -> str:
    """把 schema 里的 legend_position 语义值映射到 matplotlib 合法 loc。

    schema 允许 ``auto/top/right/none``，但 matplotlib 的 ``loc`` 不接受 ``top``/
    ``right`` 这类简称（合法值如 ``upper left``/``upper right``/``best``）。这里做
    确定性映射：``auto``→``best``，``top``→``upper center``，``right``→``center right``。
    ``none`` 由调用方提前 return，不会走到本函数。
    """
    mapping = {
        "auto": "best",
        "top": "upper center",
        "right": "center right",
    }
    return mapping.get(position, "best")


def _draw_matplotlib(chart: ChartSpec, output_dir: Path, theme: dict[str, Any]) -> ChartAsset:
    cache_dir = output_dir.parent / "tmp" / "matplotlib"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager, ticker

    fonts = {font.name for font in font_manager.fontManager.ttflist}
    font = next((name for name in theme["font_candidates"] if name in fonts), "DejaVu Sans")
    colors = theme["colors"]
    rc = {
        "font.family": font,
        "axes.unicode_minus": False,
        "text.color": theme["text_color"],
        "axes.labelcolor": theme["text_color"],
        "xtick.color": theme["muted_color"],
        "ytick.color": theme["muted_color"],
        "axes.edgecolor": theme["grid_color"],
        "figure.facecolor": theme["background_color"],
        "axes.facecolor": theme["background_color"],
    }
    orientation = _resolve_orientation(chart)
    horizontal = orientation == "horizontal" and chart.type not in {
        "scatter", "heatmap", "waterfall", "combo", "stacked_bar", "line"
    }
    # 横向图高按类别数自适应；竖向保持默认。
    height = theme["figure_height_inches"]
    if horizontal:
        height = max(2.2, 0.55 * len(chart.labels) + 1.6)

    with plt.rc_context(rc):
        fig, ax = plt.subplots(
            figsize=(theme["figure_width_inches"], height)
        )
        positions = list(range(len(chart.labels)))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(_format_axis))
        ax.grid(axis="y", color=theme["grid_color"], linewidth=0.7, alpha=0.8)
        ax.set_axisbelow(True)

        # 预测分界竖线（line 图，标记 actual → forecast/estimate 的转折点）。
        def _draw_forecast_boundary(series: ChartSeries) -> None:
            kinds = series.value_kind
            boundary = next((i for i, kind in enumerate(kinds) if kind != "actual"), None)
            if boundary is None or boundary == 0:
                return
            # 分界落在第 boundary 个点与前一 actual 点之间。
            x = positions[boundary] - 0.5
            if horizontal:
                # 横向图：类别在 y 轴，预测分界应是水平线。
                ax.axhline(x, color=theme["muted_color"], linewidth=0.8, linestyle=":")
            else:
                ax.axvline(x, color=theme["muted_color"], linewidth=0.8, linestyle=":")

        def _draw_reference_lines_and_bands() -> None:
            for band in chart.bands:
                if band.axis == "category":
                    continue
                if horizontal:
                    ax.axvspan(band.lower, band.upper, color=theme["grid_color"], alpha=0.35)
                else:
                    ax.axhspan(band.lower, band.upper, color=theme["grid_color"], alpha=0.35)
            for ref in chart.reference_lines:
                if ref.axis == "category":
                    continue
                if horizontal:
                    ax.axvline(ref.value, color=colors[1], linewidth=1.0, linestyle="--")
                    if ref.label:
                        ax.text(ref.value, -0.5, ref.label, ha="center", va="top", fontsize=8, color=colors[1])
                else:
                    ax.axhline(ref.value, color=colors[1], linewidth=1.0, linestyle="--")
                    if ref.label:
                        ax.text(-0.5, ref.value, ref.label, ha="right", va="center", fontsize=8, color=colors[1])

        def _annotate_bar_values(bars, values, unit, number_format, *, horizontal: bool, series_name: str) -> None:
            if not chart.visual.show_values:
                return
            for rect, value in zip(bars, values):
                if value is None or (isinstance(value, float) and math.isnan(value)):
                    continue
                if horizontal:
                    ax.text(rect.get_width(), rect.get_y() + rect.get_height() / 2,
                            _format_value_label(float(value), unit, number_format),
                            va="center", ha="left", fontsize=7.5)
                else:
                    ax.text(rect.get_x() + rect.get_width() / 2, rect.get_height(),
                            _format_value_label(float(value), unit, number_format),
                            ha="center", va="bottom", fontsize=7.5)

        if chart.type == "line":
            for index, series in enumerate(chart.series):
                values = _safe_values(series.values)
                color = colors[index % len(colors)]
                kinds = series.value_kind
                actual_count = sum(1 for kind in kinds if kind == "actual")
                series_label = series.name if len(chart.series) > 1 else None
                # actual <2 时不画实线段（画不出），只用实心标记 + 预测分界竖线表达；
                # 其余点按 forecast 虚线绘制。无论哪个分支，都要把 label 交给第一条
                # 画出的线，避免 legend 空。
                if actual_count >= 2:
                    actual = [value if kind == "actual" else math.nan for value, kind in zip(values, kinds)]
                    ax.plot(positions, actual, marker="o", linewidth=2.0, color=color,
                            label=series_label)
                projected = [value if kind != "actual" else math.nan for value, kind in zip(values, kinds)]
                if any(not math.isnan(value) for value in projected):
                    ax.plot(positions, projected, marker="o", linewidth=2.0, linestyle="--",
                            color=color, label=series_label if actual_count < 2 else None)
                # actual <2：单独把 actual 点用实心标记画出来（不连线）
                if actual_count < 2:
                    for i, kind in enumerate(kinds):
                        if kind == "actual" and not math.isnan(values[i]):
                            ax.plot([positions[i]], [values[i]], marker="o", markersize=7,
                                    color=color, linestyle="none",
                                    label=series_label if not any(not math.isnan(v) for v in projected) else None)
                _draw_forecast_boundary(series)
                # 标注首末点与极值
                if chart.visual.show_values:
                    _annotate_line_values(ax, positions, values, chart.unit, chart.visual.number_format)

        elif chart.type == "bar" or chart.type == "horizontal_bar":
            width = 0.76 / len(chart.series)
            for index, series in enumerate(chart.series):
                offset = (index - (len(chart.series) - 1) / 2) * width
                has_actual = any(kind == "actual" for kind in series.value_kind)
                # 序列内无 actual 时用序列主色；预测属性用 hatch/描边表达，不再整体变灰。
                base_color = colors[index % len(colors)]
                bar_colors = []
                hatches = []
                edge_colors = []
                for kind in series.value_kind:
                    if not has_actual:
                        bar_colors.append(base_color)
                    elif kind == "actual":
                        bar_colors.append(base_color)
                    else:
                        bar_colors.append(theme["forecast_color"])
                    hatches.append("" if kind == "actual" else "///")
                    edge_colors.append(base_color if kind != "actual" else "none")
                if horizontal:
                    bars = ax.barh([x + offset for x in positions], _safe_values(series.values),
                                   width, label=series.name if len(chart.series) > 1 else None,
                                   color=bar_colors, hatch=hatches, edgecolor=edge_colors)
                    _annotate_bar_values(bars, series.values, chart.unit, chart.visual.number_format,
                                         horizontal=True, series_name=series.name)
                else:
                    bars = ax.bar([x + offset for x in positions], _safe_values(series.values),
                                  width, label=series.name if len(chart.series) > 1 else None,
                                  color=bar_colors, hatch=hatches, edgecolor=edge_colors)
                    _annotate_bar_values(bars, series.values, chart.unit, chart.visual.number_format,
                                         horizontal=False, series_name=series.name)

        elif chart.type == "range_bar":
            # lower / upper 两个 series 表达区间上下限。
            lower = _safe_values(chart.series[0].values)
            upper = _safe_values(chart.series[1].values)
            widths = [u - l if not (math.isnan(l) or math.isnan(u)) else 0.0 for l, u in zip(lower, upper)]
            base_color = colors[0]
            if horizontal:
                ax.barh(positions, widths, left=lower, color=base_color, alpha=0.55, height=0.5)
                for i, (l, u) in enumerate(zip(lower, upper)):
                    if not (math.isnan(l) or math.isnan(u)):
                        ax.plot([l, u], [positions[i], positions[i]], marker="|", color=base_color, linewidth=1.5)
                        if chart.visual.show_values:
                            ax.text(l, positions[i] + 0.2, _format_value_label(l, chart.unit, chart.visual.number_format),
                                    ha="left", va="bottom", fontsize=7.5)
                            ax.text(u, positions[i] + 0.2, _format_value_label(u, chart.unit, chart.visual.number_format),
                                    ha="right", va="bottom", fontsize=7.5)
            else:
                ax.bar(positions, widths, bottom=lower, color=base_color, alpha=0.55, width=0.5)
                for i, (l, u) in enumerate(zip(lower, upper)):
                    if not (math.isnan(l) or math.isnan(u)):
                        if chart.visual.show_values:
                            ax.text(positions[i], l, _format_value_label(l, chart.unit, chart.visual.number_format),
                                    ha="center", va="bottom", fontsize=7.5)
                            ax.text(positions[i], u, _format_value_label(u, chart.unit, chart.visual.number_format),
                                    ha="center", va="bottom", fontsize=7.5)

        elif chart.type == "stacked_bar":
            bottoms = [0.0] * len(positions)
            for index, series in enumerate(chart.series):
                values = [0.0 if value is None else float(value) for value in series.values]
                ax.bar(positions, values, bottom=bottoms, label=series.name, color=colors[index % len(colors)], width=0.68)
                bottoms = [bottom + value for bottom, value in zip(bottoms, values)]

        elif chart.type == "combo":
            first = chart.series[0]
            ax.bar(positions, _safe_values(first.values), width=0.62, color=colors[0], label=first.name)
            if len(chart.series) > 1:
                right = ax.twinx()
                right.grid(False)
                right.yaxis.set_major_formatter(ticker.FuncFormatter(_format_axis))
                for index, series in enumerate(chart.series[1:], start=1):
                    right.plot(positions, _safe_values(series.values), marker="o", linewidth=2.0, color=colors[index % len(colors)], label=series.name)
                left_handles, left_labels = ax.get_legend_handles_labels()
                right_handles, right_labels = right.get_legend_handles_labels()
                ax.legend(left_handles + right_handles, left_labels + right_labels, frameon=False, loc="best")

        elif chart.type == "scatter":
            if len(chart.series) >= 2:
                xs = _safe_values(chart.series[0].values)
                ys = _safe_values(chart.series[1].values)
                x_label, y_label = chart.series[0].name, chart.series[1].name
            else:
                series = chart.series[0]
                if series.x_values is None:
                    raise ValueError(f"散点图 {chart.id} 需要两个 series 或 x_values")
                xs, ys = _safe_values(series.x_values), _safe_values(series.values)
                x_label, y_label = "X", series.name
            ax.scatter(xs, ys, s=55, color=colors[0], alpha=0.9)
            ax.set_xlabel(x_label)
            ax.set_ylabel(y_label)
            for label, x, y in zip(chart.labels, xs, ys):
                if not math.isnan(x) and not math.isnan(y):
                    ax.annotate(label, (x, y), xytext=(4, 4), textcoords="offset points", fontsize=8)

        elif chart.type == "heatmap":
            matrix = [_safe_values(series.values) for series in chart.series]
            image = ax.imshow(matrix, cmap="Blues", aspect="auto")
            ax.set_yticks(range(len(chart.series)), [series.name for series in chart.series])
            for row, values in enumerate(matrix):
                for column, value in enumerate(values):
                    if not math.isnan(value):
                        ax.text(column, row, _format_axis(value, 0), ha="center", va="center", fontsize=8)
            fig.colorbar(image, ax=ax, fraction=0.035, pad=0.03)
            ax.grid(False)

        elif chart.type == "waterfall":
            values = [0.0 if value is None else float(value) for value in chart.series[0].values]
            starts: list[float] = []
            running = 0.0
            for value in values:
                starts.append(running if value >= 0 else running + value)
                running += value
            bar_colors = [colors[0] if value >= 0 else colors[4] for value in values]
            ax.bar(positions, [abs(value) for value in values], bottom=starts, color=bar_colors, width=0.62)
            ax.axhline(0, color=theme["muted_color"], linewidth=0.8)

        else:
            raise ValueError(f"非确定性图表类型：{chart.type}")

        _draw_reference_lines_and_bands()
        for callout in chart.callouts:
            if callout.label in chart.labels:
                idx = chart.labels.index(callout.label)
                # callout 应标注在该点的数值处，不是类别索引。
                value = _callout_anchor_value(chart, idx)
                if horizontal:
                    ax.annotate(callout.text, (value, positions[idx]), xytext=(4, 4),
                                textcoords="offset points", fontsize=8, color=colors[1])
                else:
                    ax.annotate(callout.text, (positions[idx], value), xytext=(4, 4),
                                textcoords="offset points", fontsize=8, color=colors[1])

        if horizontal:
            ax.set_yticks(positions, chart.labels)
            ax.tick_params(axis="y", labelsize=8.5)
        else:
            ax.set_xticks(positions, chart.labels)
            ax.tick_params(axis="x", rotation=0 if len(chart.labels) <= 8 else 30, labelsize=8.5)
        ax.set_title(chart.title, loc="left", fontsize=13, fontweight="bold", pad=12, color=theme["text_color"])
        # 单位放在标题下方独立一行（x 对齐标题左缘），不再放 y=1.02 与标题下沿重叠。
        ax.text(0, 1.0, f"单位：{chart.unit}", transform=ax.transAxes,
                ha="left", va="top", fontsize=8.5, color=theme["muted_color"],
                linespacing=1.6)
        if chart.type not in {"combo", "scatter", "heatmap", "waterfall", "range_bar"} and len(chart.series) > 1:
            legend_position = chart.visual.legend_position
            if legend_position != "none":
                ax.legend(frameon=False, loc=_legend_loc(legend_position),
                          ncols=min(len(chart.series), 3))
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        fig.tight_layout()

        output_dir.mkdir(parents=True, exist_ok=True)
        base = output_dir / chart.id
        svg_path = base.with_suffix(".svg")
        pdf_path = base.with_suffix(".pdf")
        png_path = base.with_suffix(".png")
        fig.savefig(svg_path, format="svg", bbox_inches="tight")
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
        fig.savefig(png_path, format="png", dpi=theme["dpi"], bbox_inches="tight")
        plt.close(fig)
    return ChartAsset(chart.id, svg_path, pdf_path, png_path)


def _callout_anchor_value(chart: ChartSpec, index: int) -> float:
    """callout 绑定 label 时，取其数值锚点（该 label 上各序列的最大值，忽略 None）。"""
    candidates: list[float] = []
    for series in chart.series:
        if index < len(series.values):
            value = series.values[index]
            if value is not None:
                candidates.append(float(value))
    return max(candidates) if candidates else 0.0


def _annotate_line_values(ax, positions, values, unit, number_format) -> None:
    """标注 line 图首末点与极值点，避免为每个点添加噪声。"""
    import matplotlib
    import math as _math

    finite = [(i, v) for i, v in enumerate(values) if not _math.isnan(v)]
    if not finite:
        return
    first_i, first_v = finite[0]
    last_i, last_v = finite[-1]
    peak_i, peak_v = max(finite, key=lambda iv: iv[1])
    trough_i, trough_v = min(finite, key=lambda iv: iv[1])
    annotate = {first_i, last_i, peak_i, trough_i}
    for i in annotate:
        v = values[i]
        if _math.isnan(v):
            continue
        ax.annotate(_format_value_label(float(v), unit, number_format),
                    (positions[i], v), xytext=(0, 5), textcoords="offset points",
                    ha="center", fontsize=7.5)


def _walk_spec(value: Any, *, allowed_fields: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in _FORBIDDEN_SPEC_KEYS:
                raise ValueError(f"Vega-Lite 禁止字段：{key}")
            if key == "field" and child not in allowed_fields:
                raise ValueError(f"Vega-Lite 使用了未知数据列：{child}")
            if key == "mark":
                mark = child if isinstance(child, str) else child.get("type") if isinstance(child, dict) else None
                if mark not in _ALLOWED_MARKS:
                    raise ValueError(f"Vega-Lite mark 不允许：{mark}")
            _walk_spec(child, allowed_fields=allowed_fields)
    elif isinstance(value, list):
        for child in value:
            _walk_spec(child, allowed_fields=allowed_fields)
    elif isinstance(value, str):
        lowered = value.lower()
        if "://" in lowered or lowered.startswith(("javascript:", "file:")):
            raise ValueError("Vega-Lite 不得引用外部资源")


def validate_vega_lite_spec(spec: dict[str, Any], chart: ChartSpec) -> dict[str, Any]:
    if not isinstance(spec, dict) or not spec:
        raise ValueError("Vega-Lite spec 必须是非空对象")
    data = spec.get("data")
    if data != {"name": "reportData"}:
        raise ValueError("Vega-Lite data 必须仅引用 reportData")
    allowed_fields = {"label", *[series.name for series in chart.series]}
    _walk_spec(spec, allowed_fields=allowed_fields)
    return spec


def _chart_records(chart: ChartSpec) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, label in enumerate(chart.labels):
        row: dict[str, Any] = {"label": label}
        for series in chart.series:
            row[series.name] = series.values[index]
        records.append(row)
    return records


def _render_vega(chart: ChartSpec, output_dir: Path) -> ChartAsset:
    if chart.vega_lite_spec is None:
        raise ValueError(f"图表 {chart.id} 缺少 Vega-Lite spec")
    spec = json.loads(json.dumps(validate_vega_lite_spec(chart.vega_lite_spec, chart)))
    spec["data"] = {"values": _chart_records(chart)}
    try:
        import vl_convert as vlc
    except ImportError as exc:
        raise RuntimeError("缺少 vl-convert-python，无法渲染特殊图表") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / chart.id
    svg_path = base.with_suffix(".svg")
    pdf_path = base.with_suffix(".pdf")
    png_path = base.with_suffix(".png")
    svg_path.write_text(vlc.vegalite_to_svg(spec), encoding="utf-8")
    pdf_path.write_bytes(vlc.vegalite_to_pdf(spec))
    png_path.write_bytes(vlc.vegalite_to_png(spec, scale=2))
    return ChartAsset(chart.id, svg_path, pdf_path, png_path)


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


async def prepare_llm_fallbacks(
    manifest: ChartManifest,
    *,
    project_dir: Path,
    client: "LLMClient",
) -> None:
    fallback_dir = project_dir / "05_chart_specs"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    for chart in manifest.charts:
        if chart.type in SUPPORTED_CHART_TYPES or chart.vega_lite_spec is not None:
            continue
        if not config.REPORT_ENABLE_LLM_CHART_FALLBACK:
            if chart.required:
                raise RuntimeError(f"图表 {chart.id} 需要 LLM 兜底，但兜底已禁用")
            continue
        allowed_fields = ["label", *[series.name for series in chart.series]]
        error = ""
        for attempt in range(2):
            prompt = (
                "为一个特殊研究图表生成受限 Vega-Lite JSON。只输出 JSON 对象。"
                "data 必须严格为 {\"name\":\"reportData\"}；不得输出 values、URL、config、"
                "transform、calculate、expr、signal、datasets、外部资源或脚本。"
                "允许 layer、facet、repeat、concat 以及 mark/encoding 内的聚合、排序、分箱和堆叠。"
                f"允许的数据列只有：{allowed_fields}。图表意图：{chart.type}；"
                f"标题：{chart.title}；单位：{chart.unit}。"
            )
            if error:
                prompt += f"上一次校验错误：{error}。请只修复该错误。"
            response = await client.chat(
                [
                    ChatMessage(role="system", content="你只生成安全、声明式的 Vega-Lite JSON。"),
                    ChatMessage(role="user", content=prompt),
                ],
                temperature=0.1,
            )
            raw = response.content or ""
            (fallback_dir / f"{chart.id}.attempt{attempt + 1}.json").write_text(raw, encoding="utf-8")
            try:
                spec = json.loads(_strip_json_fence(raw))
                chart.vega_lite_spec = validate_vega_lite_spec(spec, chart)
                (fallback_dir / f"{chart.id}.validated.json").write_text(
                    json.dumps(chart.vega_lite_spec, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                break
            except (json.JSONDecodeError, ValueError) as exc:
                error = str(exc)
        if chart.vega_lite_spec is None and chart.required:
            raise RuntimeError(f"图表 {chart.id} 的 LLM 兜底失败：{error}")


def render_chart_manifest(manifest: ChartManifest, output_dir: Path) -> dict[str, ChartAsset]:
    theme = _load_theme()
    assets: dict[str, ChartAsset] = {}
    for chart in manifest.charts:
        if chart.type in SUPPORTED_CHART_TYPES:
            assets[chart.id] = _draw_matplotlib(chart, output_dir, theme)
        elif chart.vega_lite_spec is not None:
            assets[chart.id] = _render_vega(chart, output_dir)
        elif chart.required:
            raise RuntimeError(f"必需图表 {chart.id} 没有可用渲染器")
    return assets
