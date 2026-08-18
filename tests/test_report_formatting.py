import json
import re
import shutil
from pathlib import Path

import pytest

from research_agent import config
from research_agent import web_app
from research_agent.report_charts import load_chart_manifest, render_chart_manifest
from research_agent.report_formatting import (
    _layout_problems,
    abbreviate_internal_ids_for_pdf,
    build_report_html,
    build_report_latex,
    build_source_legend_markdown,
    citation_source_order,
    find_latex_engine,
    find_pandoc,
    generate_report_artifacts,
    make_latex_long_tokens_breakable,
    make_latex_source_ids_breakable,
    normalize_heading_numbers_for_pdf,
    normalize_markdown_for_pdf,
    render_report_citations_for_html,
    render_report_citations_for_latex,
    replace_chart_placeholders,
    strip_analysis_annotations,
)

FIXTURE = Path(__file__).parent / "fixtures" / "brokerage_report"


def test_latex_engine_keeps_xelatex_symlink_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "xetex"
    engine.write_text("", encoding="utf-8")
    xelatex = tmp_path / "xelatex"
    xelatex.symlink_to(engine)
    monkeypatch.setattr(config, "REPORT_LATEX_ENGINE", str(xelatex))

    assert find_latex_engine() == str(xelatex.absolute())


def test_pdf_normalization_replaces_unsupported_ratings_and_breaks_source_ids() -> None:
    assert normalize_markdown_for_pdf("★★★★☆ ≈ ❌") == "4/5 约 不确定"
    assert normalize_markdown_for_pdf("β ↔ 52 週 公佈 ▪") == "beta  <->  52 周 公布 -"
    source_id = r"src\_1234567890abcdef1234567890abcdef"
    rendered = make_latex_source_ids_breakable(source_id)
    assert rendered.replace(r"\allowbreak{}", "") == source_id
    assert rendered.count(r"\allowbreak{}") == 3


def test_html_citations_render_inside_mixed_inline_code() -> None:
    source_id = "src_1234567890abcdef1234567890abcdef"
    markdown = f"`[推导｜依据如下] [src:{source_id}:v2, End Market Summary]`"

    rendered = render_report_citations_for_html(markdown, "示例项目")

    assert "<code>" not in rendered
    assert "End Market Summary" not in rendered
    assert 'class="source-citation"' in rendered
    assert f'data-source-id="{source_id}"' in rendered
    assert "project=%E7%A4%BA%E4%BE%8B%E9%A1%B9%E7%9B%AE" in rendered


def test_latex_citations_render_as_compact_numbered_references() -> None:
    first = "src_1234567890abcdef1234567890abcdef"
    second = "src_abcdef1234567890abcdef1234567890"
    markdown = (
        f"`[事实] [src:{first}:v2, ev=ev_1, chunk=chk_1, paragraph=3]` "
        f"[src:{first}:v2, ev=ev_2] [src:{second}:v1, ev=ev_3]"
    )

    rendered = render_report_citations_for_latex(markdown)

    assert rendered == (
        "[事实] <sup>[1]</sup> "
        "<sup>[1]</sup> <sup>[2]</sup>"
    )
    assert "src:" not in rendered


def test_pdf_appendix_internal_ids_are_abbreviated() -> None:
    markdown = (
        "ev_1234567890abcdef1234567890abcdef "
        "src_abcdef1234567890abcdef1234567890"
    )

    assert abbreviate_internal_ids_for_pdf(markdown) == "E-12345678 S-abcdef12"


def test_analysis_annotations_are_stripped_from_delivery_text() -> None:
    """Agent4 的内部推导标注不进入交付文本。

    这些标注供证据门禁与人工复核使用；读者要看的是结论本身。实测一份报告正文
    里混着 42 处 `[判断｜置信度: X]` 和 8 处 `[推导...]`，全部原样进了 PDF。
    """
    markdown = (
        "总收入 2,048 亿元。[判断｜置信度: 高]\n"
        "毛利率回落至 37%。[计算]\n"
        "2027 年利润弹性是核心观察点。[推导｜依据如下]\n"
        "该结论已有多源印证。[已验证事实]\n"
    )

    rendered = strip_analysis_annotations(markdown)

    assert "判断｜置信度" not in rendered
    assert "[计算]" not in rendered
    assert "[推导" not in rendered
    assert "[已验证事实]" not in rendered
    assert "总收入 2,048 亿元。" in rendered
    assert "2027 年利润弹性是核心观察点。" in rendered


def test_low_confidence_annotation_becomes_readable_hint() -> None:
    """低置信度是读者需要知道的信息，不能随标注一起静默删掉。"""
    markdown = "目标价区间 620-680 港元。[推导｜假设驱动，低置信度]"

    rendered = strip_analysis_annotations(markdown)

    assert "（低置信度）" in rendered
    assert "假设驱动" not in rendered
    assert "目标价区间 620-680 港元。" in rendered


def test_stripping_annotations_preserves_evidence_citations() -> None:
    """剥离标注不得动到引用标记——引用审计依赖它们逐字存在。"""
    citation = "[src:src_abcdef1234567890abcdef1234567890:v1, ev=ev_1, chunk=chk_1]"
    markdown = f"营收 4200 万。[判断｜置信度: 中] {citation}"

    rendered = strip_analysis_annotations(markdown)

    assert citation in rendered
    assert "置信度" not in rendered


def test_citation_source_order_matches_rendered_numbers() -> None:
    """图例顺序必须与正文上标编号严格一致。

    两者一旦错位，读者顺着 `[3]` 会查到错误的来源——比没有图例更糟。
    """
    first = "src_1111111111111111111111111111aaaa"
    second = "src_2222222222222222222222222222bbbb"
    markdown = (
        f"结论甲 [src:{first}:v1, ev=ev_1] 结论乙 [src:{second}:v1, ev=ev_2] "
        f"结论丙 [src:{first}:v1, ev=ev_3]"
    )

    order = citation_source_order(markdown)
    rendered = render_report_citations_for_latex(markdown)

    assert order == [first, second]
    assert rendered.count("<sup>[1]</sup>") == 2
    assert rendered.count("<sup>[2]</sup>") == 1


def test_citation_source_order_counts_inline_code_first() -> None:
    """inline code 内的引用先编号，与渲染函数的两阶段遍历顺序保持一致。"""
    inline = "src_3333333333333333333333333333cccc"
    body = "src_4444444444444444444444444444dddd"
    markdown = f"正文 [src:{body}:v1, ev=ev_1] 与 `[src:{inline}:v1, ev=ev_2]`"

    order = citation_source_order(markdown)
    rendered = render_report_citations_for_latex(markdown)

    assert order == [inline, body]
    # 渲染结果里 inline code 的引用编号为 1，正文的为 2
    assert rendered.index("<sup>[2]</sup>") < rendered.index("<sup>[1]</sup>")


def test_source_legend_falls_back_without_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """catalog 不可用时图例降级为空，绝不阻断交付。"""
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "missing")
    legend = build_source_legend_markdown(
        "no-such-project", ["src_5555555555555555555555555555eeee"]
    )

    # 项目不存在时仍返回可读表格（编号 + 原始 id），或空串；两者都不得抛异常
    assert "引用来源对照" in legend or legend == ""


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


def test_pdf_markdown_normalization_removes_unsafe_glyphs() -> None:
    normalized = normalize_markdown_for_pdf(
        "ARR $1亿→2月$1.5亿 ✅ ⚠️ 🔴 ① ⭐⭐⭐⭐⭐"
    )
    assert "$1亿 -> 2月$1.5亿" in normalized
    assert "注意：" in normalized
    assert "(1)" in normalized
    assert "5/5" in normalized
    assert not any(glyph in normalized for glyph in ("✅", "⚠", "🔴", "①", "⭐"))


def test_pdf_heading_normalization_removes_only_manual_numbering() -> None:
    markdown = (
        "# 报告标题\n"
        "## 三、章节 2：分板块深度分析与预测\n"
        "### 2.1 游戏板块（国内 + 海外）\n"
        "### 1. 本章研究问题\n"
        "## 2026 年行业展望\n"
        "## 附录 A：关键计算\n"
    )

    assert normalize_heading_numbers_for_pdf(markdown) == (
        "# 报告标题\n"
        "## 分板块深度分析与预测\n"
        "### 游戏板块（国内 + 海外）\n"
        "### 本章研究问题\n"
        "## 2026 年行业展望\n"
        "## 附录 A：关键计算\n"
    )


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
    assert not artifacts["pdf_path"].with_suffix(".layout-warnings.log").exists()
    tex = artifacts["tex_path"].read_text(encoding="utf-8")
    assert "\\$1亿" in tex
    assert "p{(\\linewidth" in tex
    assert "✅" not in tex
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


# ═══════════════════════════════════════════════════════════════
# 排版稳定性：长 token 断行 + 分级门禁
# ═══════════════════════════════════════════════════════════════


def test_long_domain_gets_break_points() -> None:
    """裸域名是窄表格里 Overfull 的主要来源，必须能断行。"""
    result = make_latex_long_tokens_breakable("数据来自 companiesmarketcap.com 页面")

    assert "companiesmarketcap.com" not in result
    assert r"\allowbreak{}" in result
    assert "数据来自" in result and "页面" in result


def test_long_url_gets_break_points() -> None:
    result = make_latex_long_tokens_breakable(
        "见 https://example.com/bank-korea-ends-three-year-rate-freeze.htm"
    )

    assert r"\allowbreak{}" in result
    # 断点只插在标点后，原字符全部保留
    assert result.replace(r"\allowbreak{}", "").endswith("rate-freeze.htm")


def test_punctuation_free_run_is_chunked() -> None:
    """没有标点的长串也要断，否则仍然溢出。"""
    result = make_latex_long_tokens_breakable("x" * 40)

    assert r"\allowbreak{}" in result
    assert result.replace(r"\allowbreak{}", "") == "x" * 40


def test_short_tokens_are_untouched() -> None:
    for text in ["short.txt", "P/E 16.70x", "见 abc.com"]:
        assert make_latex_long_tokens_breakable(text) == text


def test_markup_lines_are_never_modified() -> None:
    """图片路径、标签、超链接必须逐字节保持原样。"""
    lines = [
        r"\includegraphics{05_charts/risk_scenario_matrix_long_name.pdf}",
        r"\hypertarget{ux9644traceable-evidence-index}{}",
        r"\href{https://example.com/a-very-long-path-segment}{链接}",
        r"\label{tbl:evidence-index-appendix-section}",
    ]
    for line in lines:
        assert make_latex_long_tokens_breakable(line) == line


def test_source_id_and_long_token_passes_compose() -> None:
    """两个改写函数串联后不应互相破坏。"""
    tex = r"E01 & src\_71628dada5034e46b9d59067950dac95 & companiesmarketcap.com \\"
    result = make_latex_long_tokens_breakable(make_latex_source_ids_breakable(tex))

    stripped = result.replace(r"\allowbreak{}", "")
    assert stripped == tex


def test_minor_overflow_does_not_block_delivery() -> None:
    """25pt 溢出仍在 54pt 页边距内，PDF 可正常打印，不该阻断交付。"""
    log = "Overfull \\hbox (25.50739pt too wide) in paragraph at lines 1807--1807"

    blocking, tolerable = _layout_problems(log)

    assert blocking == []
    assert len(tolerable) == 1


def test_real_spillover_blocks_delivery() -> None:
    """超过右边距即真正跑出纸面，必须阻断。"""
    log = "Overfull \\hbox (120.5pt too wide) in paragraph at lines 42--42"

    blocking, tolerable = _layout_problems(log)

    assert len(blocking) == 1
    assert tolerable == []


def test_missing_glyph_always_blocks() -> None:
    """字形缺失意味着字符被静默丢弃，与溢出宽度无关。"""
    log = 'Missing character: There is no 錦 (U+9326) in font FandolSong!'

    blocking, _ = _layout_problems(log)

    assert len(blocking) == 1


def test_boundary_overflow_is_treated_as_blocking() -> None:
    log = "Overfull \\hbox (54.0pt too wide) in paragraph at lines 1--1"
    assert len(_layout_problems(log)[0]) == 1


def test_clean_log_reports_no_problems() -> None:
    assert _layout_problems("Underfull \\hbox (badness 1533) in paragraph") == ([], [])


@pytest.mark.anyio
async def test_wide_table_with_long_tokens_compiles_without_blocking(
    tmp_path: Path,
) -> None:
    """回归：5 列证据表 + 裸域名 + 32 位源 ID 曾导致交付被阻断。"""
    if not find_pandoc() or not find_latex_engine():
        pytest.skip("需要 pandoc 与 LaTeX 引擎")

    report, _ = _copy_fixture(tmp_path)
    rows = "\n".join(
        f"| E{index:02d} | src_{index:032x} | companiesmarketcap.com | B | "
        f"TTM P/E {index}.4x, 历史数据 2008-2026, 参见 "
        f"https://example.com/very-long-slug-segment-{index}-detail.htm |"
        for index in range(1, 13)
    )
    report.write_text(
        report.read_text(encoding="utf-8")
        + "\n\n## 附：Traceable Evidence Index\n\n"
        + "| 编号 | 源 ID | 来源简述 | 类型 | 主要证据点 |\n"
        + "| --- | --- | --- | --- | --- |\n"
        + rows
        + "\n",
        encoding="utf-8",
    )

    artifacts = await generate_report_artifacts(
        topic="中国工业软件行业", project_dir=tmp_path, final_report_path=report
    )

    assert artifacts["pdf_path"] is not None
    assert artifacts["pdf_path"].is_file()

    tex = artifacts["tex_path"].read_text(encoding="utf-8")
    # 列宽按内容加权：证据点列应明显宽于编号列
    widths = [float(value) for value in re.findall(r"real\{(0\.\d+)\}", tex)]
    assert widths, "未生成显式列宽"
    assert max(widths) > min(widths) * 3


def test_column_widths_are_content_weighted(tmp_path: Path) -> None:
    """旧实现给所有列同一宽度，窄列浪费空间、宽列溢出。"""
    if not find_pandoc():
        pytest.skip("需要 pandoc")

    report, manifest_path = _copy_fixture(tmp_path)
    markdown = (
        "# 标题\n\n"
        "| 编号 | 主要证据点 |\n| --- | --- |\n"
        "| E01 | 这是一段明显更长的证据描述，包含数值 42 亿与来源说明 |\n"
    )
    manifest = load_chart_manifest(manifest_path)
    manifest.charts = []
    assets = render_chart_manifest(manifest, tmp_path / "05_charts")

    tex_path = build_report_latex(
        topic="加权列宽",
        project_dir=tmp_path,
        markdown=markdown,
        manifest=manifest,
        assets=assets,
    )

    widths = [
        float(value)
        for value in re.findall(r"real\{(0\.\d+)\}", tex_path.read_text(encoding="utf-8"))
    ]
    assert len(widths) == 2
    assert widths[1] > widths[0]
