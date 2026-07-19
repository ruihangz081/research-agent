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
    labels: list[str] = Field(min_length=1)
    series: list[ChartSeries] = Field(min_length=1)
    note: str = Field(default="", max_length=300)
    required: bool = True
    vega_lite_spec: dict[str, Any] | None = None

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
        return self


class ChartManifest(BaseModel):
    version: Literal[1] = 1
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


def load_chart_manifest(path: Path, *, max_charts: int | None = None) -> ChartManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取图表清单 {path}: {exc}") from exc
    manifest = ChartManifest.model_validate(data)
    limit = config.REPORT_MAX_CHARTS if max_charts is None else max_charts
    if len(manifest.charts) > limit:
        raise ValueError(f"图表数量 {len(manifest.charts)} 超过上限 {limit}")
    return manifest


def _load_theme() -> dict[str, Any]:
    skill = load_project_skill(config.REPORT_FORMATTING_SKILL)
    path = skill.assets_dir / "theme.json"
    theme = json.loads(path.read_text(encoding="utf-8"))
    if theme.get("name") != config.REPORT_THEME:
        raise ValueError(f"未找到报告主题：{config.REPORT_THEME}")
    return theme


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
    with plt.rc_context(rc):
        fig, ax = plt.subplots(
            figsize=(theme["figure_width_inches"], theme["figure_height_inches"])
        )
        positions = list(range(len(chart.labels)))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(_format_axis))
        ax.grid(axis="y", color=theme["grid_color"], linewidth=0.7, alpha=0.8)
        ax.set_axisbelow(True)

        if chart.type == "line":
            for index, series in enumerate(chart.series):
                values = _safe_values(series.values)
                color = colors[index % len(colors)]
                actual = [value if kind == "actual" else math.nan for value, kind in zip(values, series.value_kind)]
                projected = [value if kind != "actual" else math.nan for value, kind in zip(values, series.value_kind)]
                first_projected = next((i for i, kind in enumerate(series.value_kind) if kind != "actual"), None)
                if first_projected and not math.isnan(values[first_projected - 1]):
                    projected[first_projected - 1] = values[first_projected - 1]
                ax.plot(positions, actual, marker="o", linewidth=2.0, color=color, label=series.name)
                if any(not math.isnan(value) for value in projected):
                    ax.plot(positions, projected, marker="o", linewidth=2.0, linestyle="--", color=color)

        elif chart.type == "bar":
            width = 0.76 / len(chart.series)
            for index, series in enumerate(chart.series):
                offset = (index - (len(chart.series) - 1) / 2) * width
                bar_colors = [
                    colors[index % len(colors)] if kind == "actual" else theme["forecast_color"]
                    for kind in series.value_kind
                ]
                ax.bar([x + offset for x in positions], _safe_values(series.values), width, label=series.name, color=bar_colors)

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

        ax.set_xticks(positions, chart.labels)
        ax.tick_params(axis="x", rotation=0 if len(chart.labels) <= 8 else 30, labelsize=8.5)
        ax.set_ylabel(chart.unit)
        ax.set_title(chart.title, loc="left", fontsize=13, fontweight="bold", pad=12, color=theme["text_color"])
        if chart.type not in {"combo", "scatter", "heatmap", "waterfall"} and len(chart.series) > 1:
            ax.legend(frameon=False, loc="best", ncols=min(len(chart.series), 3))
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
