import json
import shutil
from pathlib import Path

import pytest

from research_agent import config
from research_agent import web_app
from research_agent.report_charts import load_chart_manifest, render_chart_manifest
from research_agent.report_formatting import (
    build_report_html,
    find_latex_engine,
    find_pandoc,
    generate_report_artifacts,
    replace_chart_placeholders,
)

FIXTURE = Path(__file__).parent / "fixtures" / "brokerage_report"


def _copy_fixture(tmp_path: Path) -> tuple[Path, Path]:
    report = tmp_path / config.FILE_FINAL_REPORT
    manifest = tmp_path / config.FILE_CHART_MANIFEST
    shutil.copyfile(FIXTURE / "report.md", report)
    shutil.copyfile(FIXTURE / config.FILE_CHART_MANIFEST, manifest)
    return report, manifest


def test_html_uses_pandoc_tables_and_sanitized_chart_urls(tmp_path: Path) -> None:
    report, manifest_path = _copy_fixture(tmp_path)
    manifest = load_chart_manifest(manifest_path)
    assets = render_chart_manifest(manifest, tmp_path / "05_charts")
    html_path = build_report_html(
        topic="中国工业软件行业",
        project_id="sample_20260719",
        project_dir=tmp_path,
        markdown=report.read_text(encoding="utf-8") + "\n<script>alert(1)</script>\n",
        manifest=manifest,
        assets=assets,
    )
    html = html_path.read_text(encoding="utf-8")
    assert "<table" in html
    assert "report-chart" in html
    assert "/api/projects/sample_20260719/charts/market_growth.svg" in html
    assert "<script" not in html


def test_unresolved_or_unused_required_chart_is_rejected(tmp_path: Path) -> None:
    _, manifest_path = _copy_fixture(tmp_path)
    manifest = load_chart_manifest(manifest_path)
    with pytest.raises(ValueError, match="不存在"):
        replace_chart_placeholders("{{chart:not_found}}", manifest, {}, target="html")
    with pytest.raises(ValueError, match="未在 Markdown 中使用"):
        replace_chart_placeholders("# 没有图表", manifest, {}, target="html")


@pytest.mark.anyio
async def test_full_fixture_generates_one_formal_pdf_and_qa_previews(tmp_path: Path) -> None:
    if not find_pandoc() or not find_latex_engine():
        pytest.skip("Pandoc or XeLaTeX is unavailable")
    report, _ = _copy_fixture(tmp_path)
    artifacts = await generate_report_artifacts(
        topic="中国工业软件行业",
        project_dir=tmp_path,
        final_report_path=report,
    )
    assert artifacts["html_path"].is_file()
    assert artifacts["tex_path"].is_file()
    assert artifacts["pdf_path"].is_file()
    assert artifacts["pdf_path"].name == config.FILE_FINAL_REPORT_PDF
    assert artifacts["qa"]["page_count"] >= 3
    assert len(artifacts["qa"]["previews"]) >= 2
    qa = json.loads((tmp_path / "05_pdf_qa.json").read_text(encoding="utf-8"))
    assert qa["text_characters"] > 200


@pytest.mark.anyio
async def test_legacy_and_primary_pdf_downloads_use_the_same_formal_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / config.FILE_FINAL_REPORT_PDF
    pdf_path.write_bytes(b"%PDF-test")

    async def fake_pdf_path(_state) -> Path:
        return pdf_path

    monkeypatch.setattr(web_app, "_load_state", lambda _project_id: object())
    monkeypatch.setattr(web_app, "_final_report_pdf_path", fake_pdf_path)
    primary = await web_app.api_download_final_report_pdf("sample")
    legacy = await web_app.api_download_typeset_pdf("sample")
    assert Path(primary.path) == pdf_path
    assert Path(legacy.path) == pdf_path
    assert primary.headers["content-disposition"] == legacy.headers["content-disposition"]
