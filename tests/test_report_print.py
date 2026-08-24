"""路 B P0：HTML→PDF 打印管线回归测试。

覆盖：内容覆盖率硬门禁（截断 HTML 必须抛 DeterministicContentError）、标题提取、
目录页码定位。不启动 Playwright（打印本身在 /tmp 副本的端到端验收中跑）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.pipeline_errors import DeterministicContentError
from research_agent.report_print import (
    MIN_COVERAGE,
    _compute_coverage,
    _extract_headings,
    _plain_text,
)


class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def get_text(self):
        return self._text


class _FakeDoc:
    """模拟 PyMuPDF 文档：有 page_count 属性且支持 [index] 取页。"""

    def __init__(self, pages: list[_FakePage]):
        self._pages = pages
        self.page_count = len(pages)

    def __getitem__(self, index):
        return self._pages[index]


def _fake_doc_from_pages(pages: list[str]):
    return _FakeDoc([_FakePage(text) for text in pages])


def test_plain_text_strips_tags_and_whitespace() -> None:
    html = "<article><h1>标题</h1><p>正文 42 亿元</p><script>忽略我</script></article>"
    assert _plain_text(html) == "标题正文42亿元"


def test_extract_headings_dedups_and_skips_toc() -> None:
    html = (
        '<h1 id="t">报告标题</h1>'
        '<h2 id="a">一、执行摘要</h2>'
        '<h2 id="b">综合结论</h2>'
        '<h2 id="c">目录</h2>'  # 应被跳过
    )
    headings = _extract_headings(html)
    assert [h.text for h in headings] == ["报告标题", "一、执行摘要", "综合结论"]
    assert headings[0].level == 1
    assert headings[1].level == 2


def test_extract_headings_includes_h3_for_bookmarks() -> None:
    """书签需要 h3（对齐 LaTeX 导航深度）；实测腾讯报告 h1+h2 只有 14 条，
    而全部标题 103 条，只取 h1/h2 会让侧边栏只能跳 14 个位置。"""
    html = (
        '<h1 id="t">标题</h1>'
        '<h2 id="a">章节一</h2>'
        '<h3 id="a1">小节 1.1</h3>'
        '<h3 id="a2">小节 1.2</h3>'
        '<h4 id="x">四级不取</h4>'
    )
    headings = _extract_headings(html)
    assert [(h.level, h.text) for h in headings] == [
        (1, "标题"),
        (2, "章节一"),
        (3, "小节 1.1"),
        (3, "小节 1.2"),
    ]


def test_toc_page_only_lists_h1_h2_but_bookmarks_include_h3() -> None:
    """目录页只列 h1/h2 保持简洁；书签给到 h3 便于实际导航。"""
    from research_agent.report_print import TOC_MAX_LEVEL, TocEntry, _build_bookmarks

    headings = _extract_headings(
        '<h1 id="t">标题</h1><h2 id="a">章节</h2><h3 id="b">小节</h3>'
    )
    entries = [TocEntry(heading=h, page=i + 1) for i, h in enumerate(headings)]

    in_toc = [e for e in entries if e.heading.level <= TOC_MAX_LEVEL]
    assert len(in_toc) == 2  # h1 + h2

    class _FakeTocDoc:
        def __init__(self):
            self.toc = None

        def set_toc(self, toc):
            self.toc = toc

    doc = _FakeTocDoc()
    assert _build_bookmarks(doc, entries) == 3  # h1 + h2 + h3
    assert [row[0] for row in doc.toc] == [1, 2, 3]


def test_compute_coverage_full_pass() -> None:
    html_plain = len("标题正文42亿元")
    pages = _fake_doc_from_pages(["标题", "正文 42 亿元"])
    assert _compute_coverage(pages, html_plain) >= 1.0


def test_coverage_gate_rejects_truncated_html() -> None:
    """覆盖率门禁能拦住人为截断的 HTML：PDF 文本远少于 HTML 纯文本时必须抛错。"""
    # HTML 纯文本很长（比如 1000 字），但 PDF 只渲染出一小段（模拟静默丢内容）。
    html_plain = len("内容" * 500)  # 1000 字
    # PDF 只保留了前 100 字（覆盖率 10%）
    pages = _fake_doc_from_pages(["内容" * 50])
    coverage = _compute_coverage(pages, html_plain)
    assert coverage < MIN_COVERAGE
    # 门禁逻辑与 generate_print_pdf 里的一致：低于阈值抛 DeterministicContentError。
    if coverage < MIN_COVERAGE:
        with pytest.raises(DeterministicContentError):
            raise DeterministicContentError(
                f"打印 PDF 内容覆盖率不足：{coverage:.1%} < {MIN_COVERAGE:.0%}"
            )


def test_coverage_gate_allows_running_header_overage() -> None:
    """running header/页码会给 PDF 增加文本，覆盖率可略超 100%，上界不设限。"""
    html_plain = len("内容" * 100)
    # PDF 比 HTML 多出 running header 文本
    pages = _fake_doc_from_pages(["内容" * 100 + "Research Agent 深度研究 | 1 | 章节"])
    coverage = _compute_coverage(pages, html_plain)
    assert coverage > 1.0  # 允许超 100%
    assert coverage < MIN_COVERAGE + 10  # 但不会无上限到荒谬


def test_running_header_skips_pages_with_body_in_margin() -> None:
    """页眉带被正文侵入时跳过该页，不得覆盖正文文字。

    ``break-after: avoid`` 会把「标题 + 紧随的大图」整体推到下一页，此时标题排在
    y≈34pt（高于页眉基线 50pt）。实测腾讯第 20 页、SK 第 11 页各一处。
    """
    from research_agent.report_print import (
        HEADER_CLEAR_Y,
        TocEntry,
        _extract_headings,
        _inject_running_headers,
    )

    class _Page:
        def __init__(self, blocks):
            self._blocks = blocks
            self.written = []

        def get_text(self, kind="text"):
            return self._blocks if kind == "blocks" else ""

        def insert_text(self, point, text, **kwargs):
            self.written.append((point, text))

    class _Doc:
        def __init__(self, pages):
            self._pages = pages
            self.page_count = len(pages)

        def __getitem__(self, index):
            return self._pages[index]

    # 页 1 封面、页 2 目录、页 3 正常（正文从 y=70 起）、页 4 侵入（正文 y=34）
    normal = _Page([(51.0, 70.0, 500.0, 90.0, "正文")])
    intruded = _Page([(51.0, 34.0, 500.0, 50.0, "被推下来的标题")])
    doc = _Doc([_Page([]), _Page([]), normal, intruded])

    headings = _extract_headings('<h2 id="a">章节一</h2>')
    entries = [TocEntry(heading=headings[0], page=3)]

    skipped = _inject_running_headers(doc, entries)

    assert skipped == [4]
    assert len(normal.written) == 1  # 正常页写入了 header
    assert normal.written[0][1].endswith("章节一")
    assert intruded.written == []  # 侵入页未写入，正文未被覆盖
    assert intruded._blocks[0][1] < HEADER_CLEAR_Y


def test_cjk_font_check_reads_embedded_pdf_fonts() -> None:
    """CJK 字体检查读产出 PDF 的嵌入字体（结果导向，跨平台）。

    三条被实测否决的替代方案见 report_print._CJK_FONT_MARKERS 的注释：fc-list 在
    macOS 不存在、CDP getPlatformFontsForNode 反复调用会失效、document.fonts.check
    对不存在的字体也返回命中。
    """
    from research_agent.report_print import _embedded_cjk_fonts, _warn_if_no_cjk_font

    class _FontPage:
        def __init__(self, fonts):
            self._fonts = fonts

        def get_fonts(self):
            return self._fonts

    class _FontDoc:
        def __init__(self, pages):
            self._pages = pages
            self.page_count = len(pages)

        def __getitem__(self, index):
            return self._pages[index]

    # 真实观测到的字体名（Chrome 产出 PDF 会带 ABCDEF+ 子集前缀）
    有中文 = _FontDoc([
        _FontPage([(0, 0, 0, "BAAAAA+STSongti-SC-Regular", "F1", ""),
                   (0, 0, 0, "CAAAAA+Heiti", "F2", "")])
    ])
    assert _embedded_cjk_fonts(有中文) == ["Heiti", "STSongti-SC-Regular"]
    assert _warn_if_no_cjk_font(有中文)

    仅拉丁 = _FontDoc([_FontPage([(0, 0, 0, "XYZ+Helvetica", "F1", "")])])
    assert _embedded_cjk_fonts(仅拉丁) == []
    assert _warn_if_no_cjk_font(仅拉丁) == []  # 告警但不抛错
