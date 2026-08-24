"""设计 token 的单一来源加载器。

路 B P1 的核心：把 Web / Print / LaTeX / Chart 四份独立样式里的颜色与字体族
收敛到 skills/brokerage-report-formatting/assets/design-tokens.json，由本模块
统一加载、校验、缓存。任何消费者（图表渲染、打印 CSS 注入、Web 样式、LaTeX
tokens 生成）都从这里取 token，缺失或非法时抛错，绝不静默回退默认值。

字号（scale）按媒介分组是刻意的：语义键统一（body/h1/h2/h3/table），数值各自
标定（Web 用 px、打印用 pt、LaTeX 用相对尺寸）。三者本就不该压成同一个数值。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config
from .agent_skills import load_project_skill
from .pipeline_errors import DETERMINISTIC_CONTENT_HINT, DeterministicContentError

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 顶层必需键。
_REQUIRED_TOP_KEYS = ("name", "color", "font", "chart", "scale")

#: color 块必需键。
_REQUIRED_COLOR_KEYS = (
    "primary",
    "accent",
    "text",
    "muted",
    "grid",
    "surface",
    "surface_alt",
    "heading_sub",
    "series",
    "forecast",
)

#: font 块必需键。
_REQUIRED_FONT_KEYS = ("cjk_sans", "cjk_serif", "chart_candidates")

#: chart 块必需键。
_REQUIRED_CHART_KEYS = ("width_inches", "height_inches", "dpi")

#: scale 块必需键。
_REQUIRED_SCALE_KEYS = ("web", "print")


@dataclass(frozen=True)
class DesignTokens:
    """已校验的设计 token（不可变快照）。"""

    name: str
    color: dict[str, Any]
    font: dict[str, list[str]]
    chart: dict[str, float]
    scale: dict[str, dict[str, str]]


def _validate(tokens: dict[str, Any]) -> DesignTokens:
    if not isinstance(tokens, dict):
        raise DeterministicContentError(
            f"设计 token 根必须是对象（{DETERMINISTIC_CONTENT_HINT}）"
        )
    for key in _REQUIRED_TOP_KEYS:
        if key not in tokens:
            raise DeterministicContentError(
                f"设计 token 缺少必需键：{key!r}（{DETERMINISTIC_CONTENT_HINT}）"
            )

    name = tokens["name"]
    if name != config.REPORT_THEME:
        raise DeterministicContentError(
            f"设计 token 主题名 {name!r} 与 REPORT_THEME {config.REPORT_THEME!r} 不一致"
            f"（{DETERMINISTIC_CONTENT_HINT}）"
        )

    color = tokens["color"]
    if not isinstance(color, dict):
        raise DeterministicContentError("设计 token 的 color 块必须是对象")
    for key in _REQUIRED_COLOR_KEYS:
        if key not in color:
            raise DeterministicContentError(
                f"设计 token 的 color 缺少必需键：{key!r}（{DETERMINISTIC_CONTENT_HINT}）"
            )
    for key, value in color.items():
        if key == "series":
            if not isinstance(value, list) or not value:
                raise DeterministicContentError(
                    "设计 token 的 color.series 必须是非空数组"
                    f"（{DETERMINISTIC_CONTENT_HINT}）"
                )
            for item in value:
                if not _HEX_RE.match(str(item)):
                    raise DeterministicContentError(
                        f"设计 token 的 color.series 含非法色值：{item!r}"
                        f"（{DETERMINISTIC_CONTENT_HINT}）"
                    )
        elif not isinstance(value, str) or not _HEX_RE.match(value):
            raise DeterministicContentError(
                f"设计 token 的 color.{key} 非法色值：{value!r}"
                f"（{DETERMINISTIC_CONTENT_HINT}）"
            )

    font = tokens["font"]
    if not isinstance(font, dict):
        raise DeterministicContentError("设计 token 的 font 块必须是对象")
    for key in _REQUIRED_FONT_KEYS:
        if key not in font:
            raise DeterministicContentError(
                f"设计 token 的 font 缺少必需键：{key!r}（{DETERMINISTIC_CONTENT_HINT}）"
            )
        value = font[key]
        if not isinstance(value, list) or not value or not all(
            isinstance(item, str) for item in value
        ):
            raise DeterministicContentError(
                f"设计 token 的 font.{key} 必须是非空字符串数组"
                f"（{DETERMINISTIC_CONTENT_HINT}）"
            )

    chart = tokens["chart"]
    if not isinstance(chart, dict):
        raise DeterministicContentError("设计 token 的 chart 块必须是对象")
    for key in _REQUIRED_CHART_KEYS:
        if key not in chart:
            raise DeterministicContentError(
                f"设计 token 的 chart 缺少必需键：{key!r}（{DETERMINISTIC_CONTENT_HINT}）"
            )
        if not isinstance(chart[key], (int, float)):
            raise DeterministicContentError(
                f"设计 token 的 chart.{key} 必须是数值（{DETERMINISTIC_CONTENT_HINT}）"
            )

    scale = tokens["scale"]
    if not isinstance(scale, dict):
        raise DeterministicContentError("设计 token 的 scale 块必须是对象")
    for key in _REQUIRED_SCALE_KEYS:
        if key not in scale:
            raise DeterministicContentError(
                f"设计 token 的 scale 缺少必需键：{key!r}（{DETERMINISTIC_CONTENT_HINT}）"
            )
        medium = scale[key]
        if not isinstance(medium, dict):
            raise DeterministicContentError(f"设计 token 的 scale.{key} 必须是对象")

    return DesignTokens(
        name=name,
        color=color,
        font=font,
        chart={k: float(chart[k]) for k in _REQUIRED_CHART_KEYS},
        scale=scale,
    )


@lru_cache(maxsize=1)
def load_design_tokens() -> DesignTokens:
    """加载并校验设计 token（进程内缓存，缺失/非法即抛错）。"""
    skill = load_project_skill(config.REPORT_FORMATTING_SKILL)
    path = skill.assets_dir / "design-tokens.json"
    if not path.is_file():
        raise DeterministicContentError(
            f"设计 token 文件缺失：{path}（{DETERMINISTIC_CONTENT_HINT}）"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _validate(raw)


def design_tokens_path() -> Path:
    """返回 design-tokens.json 的绝对路径（供 CSS/LaTeX 生成直接读取）。"""
    skill = load_project_skill(config.REPORT_FORMATTING_SKILL)
    return skill.assets_dir / "design-tokens.json"


def css_root_variables(tokens: DesignTokens | None = None) -> str:
    """从 tokens 生成 :root { --brk-*: ...; } 段（供打印 CSS 注入）。"""
    tokens = tokens or load_design_tokens()
    c = tokens.color
    lines = [
        ":root {",
        f"  --brk-primary: {c['primary']};",
        f"  --brk-accent: {c['accent']};",
        f"  --brk-text: {c['text']};",
        f"  --brk-muted: {c['muted']};",
        f"  --brk-grid: {c['grid']};",
        f"  --brk-surface: {c['surface']};",
        f"  --brk-surface-alt: {c['surface_alt']};",
        f"  --brk-heading-sub: {c['heading_sub']};",
        f"  --brk-forecast: {c['forecast']};",
        f"  --brk-series-0: {c['series'][0]};",
        f"  --brk-series-1: {c['series'][1]};",
        f"  --brk-series-2: {c['series'][2]};",
        f"  --brk-series-3: {c['series'][3]};",
        f"  --brk-series-4: {c['series'][4]};",
        f"  --brk-series-5: {c['series'][5]};",
        "}",
    ]
    return "\n".join(lines)


def latex_tokens_tex(tokens: DesignTokens | None = None) -> str:
    """从 tokens 生成 brokerage-tokens.tex（颜色命令名保持不变）。"""
    tokens = tokens or load_design_tokens()
    c = tokens.color
    # 颜色命令名与 brokerage-report.sty 现有定义严格一致：BrokerBlue / BrokerGold /
    # BrokerMuted / BrokerGrid / BrokerText。只换值来源，不改命令名。
    html = lambda hexstr: hexstr.lstrip("#").upper()  # noqa: E731
    return "\n".join(
        [
            "% 由 design-tokens.json 自动生成，勿手改（brokerage_research_v1）。",
            r"\definecolor{BrokerBlue}{HTML}{" + html(c["primary"]) + "}",
            r"\definecolor{BrokerGold}{HTML}{" + html(c["accent"]) + "}",
            r"\definecolor{BrokerMuted}{HTML}{" + html(c["muted"]) + "}",
            r"\definecolor{BrokerGrid}{HTML}{" + html(c["grid"]) + "}",
            r"\definecolor{BrokerText}{HTML}{" + html(c["text"]) + "}",
            "",
        ]
    )


__all__ = [
    "DesignTokens",
    "load_design_tokens",
    "design_tokens_path",
    "css_root_variables",
    "latex_tokens_tex",
]
