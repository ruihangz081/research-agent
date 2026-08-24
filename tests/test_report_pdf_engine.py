"""路 B P2：REPORT_PDF_ENGINE 配置、引擎分发与 Web 侧 tex 假设回归测试。

覆盖验收第 7 条：
- 引擎分发两分支（latex 保留 build_report_latex + compile_report_pdf；chrome 走
  generate_print_pdf 且 tex_path 为 None）；
- chrome 下 tex_path 为 None；
- 非法引擎值启动即报错；
- playwright 缺失时的错误信息（monkeypatch 模拟 ImportError）。
- Web 侧：chrome 下 /download/final-report.tex 返回 404 且 detail 正确、
  state.final_report_tex_path 为 None、重排版端点带 engine 字段。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from research_agent import config


# ═══════════════════════════════════════════════════════════════
# P2-A：非法引擎值启动即报错（不静默回退）
# ═══════════════════════════════════════════════════════════════


def test_invalid_pdf_engine_raises_on_import(tmp_path: Path) -> None:
    """非法 REPORT_PDF_ENGINE 值必须在导入期抛 ValueError，而不是静默回退 latex。

    用子进程跑一次全新导入，等价于「启动即报错」：进程一 import config 就崩。
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["REPORT_PDF_ENGINE"] = "typst"
    proc = subprocess.run(
        [sys.executable, "-c", "import research_agent.config"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode != 0
    assert "REPORT_PDF_ENGINE" in proc.stderr


def test_valid_pdf_engine_values_are_accepted() -> None:
    """合法取值 latex / chrome 均通过校验（子进程导入不报错）。"""
    import os
    import subprocess
    import sys

    for value in ("latex", "chrome"):
        env = dict(os.environ)
        env["REPORT_PDF_ENGINE"] = value
        proc = subprocess.run(
            [sys.executable, "-c", "import research_agent.config"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr


# ═══════════════════════════════════════════════════════════════
# P2-C：playwright 缺失时的明确错误信息
# ═══════════════════════════════════════════════════════════════


def test_print_pdf_playwright_missing_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """playwright 不可用时错误信息必须说明缺什么、怎么装，且不得静默回退。"""
    from research_agent import report_print

    html_path = tmp_path / "05_final_report.html"
    html_path.write_text("<article><h1>标题</h1><p>正文</p></article>", encoding="utf-8")

    # 模拟 playwright 未安装：让 import 抛 ImportError。
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError, match="playwright"):
        report_print._print_pdf(html_path, tmp_path / "out.pdf")


# ═══════════════════════════════════════════════════════════════
# P2-A：引擎分发两分支
# ═══════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_generate_report_artifacts_chrome_engine_dispatches_to_print(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPORT_PDF_ENGINE=chrome 时走 generate_print_pdf，不生成 .tex。"""
    from research_agent import report_formatting
    from research_agent.report_print import PrintResult

    _stub_provenance_and_charts(monkeypatch)
    monkeypatch.setattr(
        report_formatting,
        "build_report_html",
        _fake_build_report_html,
    )
    monkeypatch.setattr(
        report_formatting,
        "build_report_latex",
        _raise_unexpected("chrome 引擎不应调用 build_report_latex"),
    )
    monkeypatch.setattr(
        report_formatting,
        "compile_report_pdf",
        _raise_unexpected("chrome 引擎不应调用 compile_report_pdf"),
    )

    calls: dict[str, bool] = {}

    def fake_generate_print_pdf(*, project_dir, html_path, **kwargs):
        calls["generate_print_pdf"] = True
        pdf_path = project_dir / config.FILE_FINAL_REPORT_PDF
        pdf_path.write_bytes(b"%PDF-fake")
        return PrintResult(
            pdf_path=pdf_path,
            page_count=3,
            coverage=1.0,
            headings=[],
            toc_entries=[],
            bookmark_count=0,
        )

    monkeypatch.setattr(config, "REPORT_PDF_ENGINE", "chrome")
    monkeypatch.setattr(
        "research_agent.report_print.generate_print_pdf",
        fake_generate_print_pdf,
    )
    # inspect_pdf 会对真实 PDF 做解析，替换成轻量桩。
    monkeypatch.setattr(
        report_formatting,
        "inspect_pdf",
        lambda pdf_path, topic: {"page_count": 3, "text_characters": 10, "sample_pages": [1], "previews": []},
    )

    final_report = tmp_path / config.FILE_FINAL_REPORT
    final_report.write_text("# 标题\n\n正文内容\n", encoding="utf-8")

    artifacts = await report_formatting.generate_report_artifacts(
        topic="测试",
        project_dir=tmp_path,
        final_report_path=final_report,
    )

    assert calls.get("generate_print_pdf") is True
    assert artifacts["tex_path"] is None
    assert artifacts["engine_used"] == "chrome"
    assert artifacts["pdf_path"] is not None


@pytest.mark.anyio
async def test_generate_report_artifacts_latex_engine_keeps_legacy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPORT_PDF_ENGINE=latex（默认）时保持 build_report_latex + compile_report_pdf。"""
    from research_agent import report_formatting

    _stub_provenance_and_charts(monkeypatch)
    monkeypatch.setattr(
        report_formatting, "build_report_html", _fake_build_report_html
    )

    calls: dict[str, bool] = {}

    tex_path = tmp_path / config.FILE_FINAL_REPORT_TEX
    pdf_path = tmp_path / config.FILE_FINAL_REPORT_PDF

    def fake_build_report_latex(**kwargs):
        calls["build_report_latex"] = True
        tex_path.write_text(r"\documentclass{article}", encoding="utf-8")
        return tex_path

    def fake_compile_report_pdf(tex):
        calls["compile_report_pdf"] = True
        pdf_path.write_bytes(b"%PDF-fake")
        return pdf_path

    monkeypatch.setattr(config, "REPORT_PDF_ENGINE", "latex")
    monkeypatch.setattr(
        report_formatting, "build_report_latex", fake_build_report_latex
    )
    monkeypatch.setattr(
        report_formatting, "compile_report_pdf", fake_compile_report_pdf
    )
    monkeypatch.setattr(
        report_formatting,
        "inspect_pdf",
        lambda pdf_path, topic: {"page_count": 3, "text_characters": 10, "sample_pages": [1], "previews": []},
    )

    final_report = tmp_path / config.FILE_FINAL_REPORT
    final_report.write_text("# 标题\n\n正文内容\n", encoding="utf-8")

    artifacts = await report_formatting.generate_report_artifacts(
        topic="测试",
        project_dir=tmp_path,
        final_report_path=final_report,
    )

    assert calls.get("build_report_latex") is True
    assert calls.get("compile_report_pdf") is True
    assert artifacts["tex_path"] == tex_path
    assert artifacts["engine_used"] == "latex"
    assert artifacts["pdf_path"] is not None


# ═══════════════════════════════════════════════════════════════
# P2-B：Web 侧 chrome 下 tex 假设
# ═══════════════════════════════════════════════════════════════


def _stub_provenance_and_charts(monkeypatch: pytest.MonkeyPatch) -> None:
    """打桩 generate_report_artifacts 里图表渲染与溯源门禁的本地导入源。

    generate_report_artifacts 内部用 ``from .chart_provenance import ...`` 与
    ``from .sources.runtime import get_service`` 做本地导入，因此必须 patch 它们的
    源模块，而不是 report_formatting 上的名字。
    """
    from research_agent import chart_provenance
    from research_agent import report_formatting
    from research_agent.sources import runtime as sources_runtime

    monkeypatch.setattr(report_formatting, "load_chart_manifest", lambda p: _empty_manifest())
    monkeypatch.setattr(
        report_formatting, "render_chart_manifest", lambda manifest, charts_dir: {}
    )
    monkeypatch.setattr(
        chart_provenance,
        "validate_chart_provenance",
        lambda *a, **k: {"version": 1, "charts": []},
    )
    monkeypatch.setattr(
        chart_provenance,
        "write_provenance_report",
        lambda report, path: path,
    )
    monkeypatch.setattr(
        chart_provenance,
        "apply_provenance_gate",
        lambda *a, **k: set(),
    )
    monkeypatch.setattr(
        sources_runtime,
        "get_service",
        lambda *a, **k: _FakeService(),
    )


def _empty_manifest():
    from research_agent.report_charts import ChartManifest

    return ChartManifest(version=1, charts=[])


class _FakeRepository:
    def list_sources(self, project_id, include_superseded=False):
        return []

    def list_evidence(self, project_id):
        return []


class _FakeService:
    def __init__(self):
        self.repository = _FakeRepository()


def _fake_build_report_html(**kwargs) -> Path:
    html_path = kwargs["project_dir"] / config.FILE_FINAL_REPORT_HTML
    html_path.write_text("<article><h1>标题</h1></article>", encoding="utf-8")
    return html_path


def _raise_unexpected(msg: str):
    def _inner(*args, **kwargs):
        raise AssertionError(msg)

    return _inner


@pytest.mark.anyio
async def test_tex_download_404_in_chrome_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chrome 引擎下 /download/final-report.tex 返回 404 且 detail 说明不产出 LaTeX。"""
    import httpx

    from research_agent import web_app

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "REPORT_PDF_ENGINE", "chrome")
    web_app.JOBS.clear()
    from research_agent.state import ProjectState

    state = ProjectState(topic="chrome 引擎", date_str="20260728")
    state.final_report_path = str(state.project_dir / config.FILE_FINAL_REPORT)
    state.save()
    (state.project_dir / config.FILE_FINAL_REPORT).write_text("# 报告", encoding="utf-8")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/projects/{state.project_dir.name}/download/final-report.tex"
        )

    assert response.status_code == 404
    assert "chrome" in response.json()["detail"]
    assert "LaTeX" in response.json()["detail"]


@pytest.mark.anyio
async def test_typeset_endpoint_returns_engine_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重排版端点返回体带 engine 字段。"""
    import httpx

    from research_agent import web_app

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    from research_agent.state import ProjectState, Stage

    state = ProjectState(topic="引擎字段", date_str="20260728", stage=Stage.FORMATTING)
    report = state.project_dir / config.FILE_FINAL_REPORT
    state.final_report_path = str(report)
    state.save()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# 报告", encoding="utf-8")

    async def fake_artifacts(**kwargs):
        return {
            "tex_path": state.project_dir / config.FILE_FINAL_REPORT_TEX,
            "manifest_path": state.project_dir / config.FILE_CHART_MANIFEST,
            "html_path": state.project_dir / config.FILE_FINAL_REPORT_HTML,
            "pdf_path": state.project_dir / config.FILE_FINAL_REPORT_PDF,
            "engine": "/usr/bin/xelatex",
            "engine_used": "chrome",
        }

    monkeypatch.setattr(web_app, "generate_typeset_artifacts", fake_artifacts)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/typeset/final-report"
        )

    assert response.status_code == 200
    assert response.json()["engine"] == "chrome"
