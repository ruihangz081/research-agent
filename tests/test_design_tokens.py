"""路 B P1 一致性守卫：设计 token 的单一来源 + 反漂移 + 联动 + 契约校验。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from research_agent import config
from research_agent.design_tokens import (
    css_root_variables,
    latex_tokens_tex,
    load_design_tokens,
    design_tokens_path,
)
from research_agent.pipeline_errors import DeterministicContentError

ROOT = Path(__file__).resolve().parents[1]

#: 反漂移扫描目标：(路径, 是否只扫某段)。None = 扫整文件。
#: report_print.css 与 brokerage-report.sty 扫整文件；
#: styles.css 只扫 .brokerage-report 段（其余 770 行与报告无关）。
_DRIFT_TARGETS = {
    "src/research_agent/report_print.css": None,
    "src/research_agent/web_static/styles.css": ".brokerage-report",
    "skills/brokerage-report-formatting/assets/brokerage-report.sty": None,
}

#: 正文语义色键——这些值若在样式文件里硬编码，即视为漂移（#1f2937 就是这么来的）。
#: surface 白色与封面/页码/点线等装饰色不进守卫：白就是白（th color:#fff），
#: 装饰色（封面深蓝渐变/页码灰/点线灰）从未在四份样式间共享，不存在漂移。
_GUARD_COLOR_KEYS = (
    "primary",
    "accent",
    "text",
    "muted",
    "grid",
    "surface_alt",
    "heading_sub",
    "forecast",
)


def _extract_report_section(text: str, marker: str) -> str:
    """截取 styles.css 里 .brokerage-report 段（从该 marker 到下一个空行块结束）。"""
    idx = text.find(marker)
    if idx < 0:
        return ""
    # 段结束：下一个以 . 或 # 开头且非 .brokerage-report/.report-chart/.table-scroll 的选择器
    lines = text[idx:].splitlines()
    collected = []
    for line in lines:
        stripped = line.strip()
        # 段边界：遇到新的独立选择器块（前面是空行且不是本段相关选择器）
        if (
            collected
            and stripped
            and not stripped.startswith((".brokerage-report", ".report-chart", ".table-scroll"))
            and re.match(r"^[.#a-zA-Z][^{]*\{", stripped)
        ):
            break
        collected.append(line)
    return "\n".join(collected)


def test_no_drift_hardcoded_body_colors() -> None:
    r"""正文语义色不得在三个样式文件里硬编码出现（防 #1f2937 类漂移复发）。

    豁免：``--brk-*: #xxx`` 变量定义行（tokens 的 CSS 镜像，非硬编码引用）；
    ``\definecolor`` 行（LaTeX 侧 tokens 镜像，但 sty 已改为 \input，见下）。
    """
    tokens = load_design_tokens()
    guard_colors = {tokens.color[k].lower() for k in _GUARD_COLOR_KEYS}
    guard_colors.update(s.lower() for s in tokens.color["series"])

    # 豁免：--brk-xxx: #value; 定义行里的色值（变量定义 = 单一来源镜像）
    var_def_re = re.compile(r"--brk-[a-z0-9-]+\s*:\s*#[0-9a-fA-F]{6}")

    for rel, section in _DRIFT_TARGETS.items():
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        if section:
            text = _extract_report_section(text, section)
        # 剥离变量定义行后再查硬编码
        stripped = var_def_re.sub("", text)
        lowered = stripped.lower()
        for color in guard_colors:
            assert color not in lowered, (
                f"{rel} 硬编码了正文语义色 {color}，应从 design-tokens 取（反漂移）"
            )


def test_token_change_propagates_to_all_four_sides(tmp_path: Path, monkeypatch) -> None:
    """改一个 token 值 → 图表 / 打印 CSS / LaTeX / Web 四处同步变化。

    Web 侧此前是 styles.css 里手写的 ``--brk-*`` 镜像：名字像从 token 来的，实际
    是平行的第二份定义，改 token 不会跟着变，且反漂移守卫豁免了变量定义行所以测
    不出来。现在改为由 ``/static/design-tokens.css`` 端点生成。
    """
    tokens = load_design_tokens()
    changed = type(tokens)(
        name=tokens.name,
        color={**tokens.color, "text": "#ABCDEF"},
        font=tokens.font,
        chart=tokens.chart,
        scale=tokens.scale,
    )
    css = css_root_variables(changed)
    assert "--brk-text: #ABCDEF" in css
    latex = latex_tokens_tex(changed)
    assert r"\definecolor{BrokerText}{HTML}{ABCDEF}" in latex

    # 图表侧：_load_theme 从同一 tokens 派生，text_color 同步。
    from research_agent import design_tokens as dt
    from research_agent.report_charts import _load_theme

    monkeypatch.setattr(dt, "load_design_tokens", lambda: changed)
    theme = _load_theme()
    assert theme["text_color"] == "#ABCDEF"

    # Web 侧：/static/design-tokens.css 端点由同一函数生成，随 token 同步。
    from fastapi.testclient import TestClient

    from research_agent.web_app import app

    response = TestClient(app).get("/static/design-tokens.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert "--brk-text: #ABCDEF" in response.text


def test_web_css_has_no_parallel_brk_definition() -> None:
    """styles.css 不得再自带 --brk-* 定义（否则又成平行来源）。

    刻意不留兜底值：漏引入 design-tokens.css 时应立刻表现为颜色缺失，而不是静默
    用一份可能过期的镜像。
    """
    text = (ROOT / "src/research_agent/web_static/styles.css").read_text(encoding="utf-8")
    assert not re.search(r"--brk-[a-z0-9-]+\s*:\s*#", text), (
        "styles.css 里出现了 --brk-* 色值定义，应只保留 var() 引用"
    )


def test_all_html_pages_link_design_tokens_css() -> None:
    """每个页面都必须在 styles.css 之后引入 design-tokens.css。"""
    static_dir = ROOT / "src/research_agent/web_static"
    pages = [
        "index.html", "app.html", "research.html", "materials.html",
        "results.html", "settings.html", "workspace.html",
    ]
    for name in pages:
        text = (static_dir / name).read_text(encoding="utf-8")
        styles_at = text.find("/static/styles.css")
        tokens_at = text.find("/static/design-tokens.css")
        assert styles_at >= 0, f"{name} 缺少 styles.css"
        assert tokens_at >= 0, f"{name} 缺少 design-tokens.css"
        assert tokens_at > styles_at, f"{name} 的 design-tokens.css 必须在 styles.css 之后"


def test_contract_missing_key_raises() -> None:
    """tokens 缺必需键抛 DeterministicContentError，不静默用默认值。"""
    raw = json.loads(design_tokens_path().read_text(encoding="utf-8"))
    del raw["color"]["primary"]
    from research_agent.design_tokens import _validate

    with pytest.raises(DeterministicContentError, match="primary"):
        _validate(raw)


def test_contract_invalid_color_raises() -> None:
    """色值非法（非 6 位 hex）抛错。"""
    raw = json.loads(design_tokens_path().read_text(encoding="utf-8"))
    raw["color"]["text"] = "red"
    from research_agent.design_tokens import _validate

    with pytest.raises(DeterministicContentError, match="非法色值"):
        _validate(raw)


def test_contract_name_mismatch_raises() -> None:
    """name 与 REPORT_THEME 不一致抛错（保留主题名校验语义）。"""
    raw = json.loads(design_tokens_path().read_text(encoding="utf-8"))
    raw["name"] = "wrong_theme_name"
    from research_agent.design_tokens import _validate

    with pytest.raises(DeterministicContentError, match="不一致"):
        _validate(raw)


def test_contract_series_empty_raises() -> None:
    """series 空数组抛错。"""
    raw = json.loads(design_tokens_path().read_text(encoding="utf-8"))
    raw["color"]["series"] = []
    from research_agent.design_tokens import _validate

    with pytest.raises(DeterministicContentError, match="series"):
        _validate(raw)


def test_load_design_tokens_returns_expected_theme() -> None:
    """正常加载：name 与 REPORT_THEME 一致，必需键齐全。"""
    tokens = load_design_tokens()
    assert tokens.name == config.REPORT_THEME == "brokerage_research_v1"
    assert tokens.color["text"] == "#18212B"
    assert tokens.chart["dpi"] == 180.0
