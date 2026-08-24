"""HTML→PDF 打印管线（裸 Chrome 打印 + PyMuPDF 后处理，独立于 LaTeX 主链路）。

路 B P0：把 scripts/print_report_pdf.py 的试点提升为可用管线。输入是项目目录下
已有的 ``05_final_report.html`` 与 ``05_charts/``，输出正式 PDF。

设计要点（均基于实测结论）：
- 不用 paged.js（真实报告上会静默丢内容，见脚本死路 1）；
- 不用 JS 测 offsetTop 换算打印页号（屏幕坐标与打印分页无关，死路 2）；
- 用 Playwright 的 ``page.pdf()`` 替代 subprocess 调 Chrome；
- CSS counter 在裸 Chrome 打印里可用（章节/图表自动编号，事实 1）；
- 两趟打印稳定可复现（事实 2）：pass1 无目录 → PyMuPDF 全文搜索定位标题页号
  → pass2 注入真实页码目录 + running header 后重打；
- 内容完整性硬门禁：PDF 提取文本 / HTML 纯文本 < 95% 抛 DeterministicContentError。
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from html import escape
from pathlib import Path

from . import config
from .pipeline_errors import DETERMINISTIC_CONTENT_HINT, DeterministicContentError

logger = logging.getLogger(__name__)

#: 图表 src 重写：/api/projects/{id}/charts/{chart_id}.svg → file:// 绝对路径。
API_CHART = re.compile(r'src="/api/projects/([^"/]+)/charts/([a-z0-9_-]+\.svg)"')
#: 标题锚点：h1/h2/h3 带 id。目录只用 h1/h2，书签用到 h3（对齐 LaTeX 的导航深度）。
HEADING = re.compile(r'<h([123])[^>]*id="([^"]+)"[^>]*>(.*?)</h\1>', re.S)
#: 目录/书签只排除"目录"自身（正文标题不会出现这个文本，防御性跳过）。
SKIP_TOC = {"目录"}
#: 目录层级：只有 h1/h2 进目录页；h3 只进 PDF 书签。
TOC_MAX_LEVEL = 2

#: 目录页码占位（pass2 注入真实页码）。
PAGE_PLACEHOLDER = "__PAGE__"
#: 覆盖率的判定阈值：低于此值即抛 DeterministicContentError。
MIN_COVERAGE = 0.95

_CSS_PATH = Path(__file__).parent / "report_print.css"


@dataclass
class Heading:
    level: int
    anchor: str
    text: str


@dataclass
class TocEntry:
    heading: Heading
    page: int = 0


@dataclass
class PrintResult:
    pdf_path: Path
    page_count: int
    coverage: float
    headings: list[Heading] = field(default_factory=list)
    toc_entries: list[TocEntry] = field(default_factory=list)
    bookmark_count: int = 0


def _plain_text(fragment: str) -> str:
    """剥 HTML 标签取纯文本（去空白），用于覆盖率计算。"""
    text = re.sub(r"<script.*?</script>", "", fragment, flags=re.S)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    from html import unescape

    text = unescape(text)
    return re.sub(r"\s+", "", text)


def _extract_headings(fragment: str) -> list[Heading]:
    """提取 h1/h2（带 id）作为目录与书签来源，按出现顺序去重。"""
    headings: list[Heading] = []
    seen: set[str] = set()
    for level_str, anchor, content in HEADING.findall(fragment):
        text = re.sub(r"<[^>]+>", "", content).strip()
        if not text or text in seen or text in SKIP_TOC:
            continue
        seen.add(text)
        headings.append(Heading(level=int(level_str), anchor=anchor, text=text))
    return headings


#: 已知 CJK 字体名特征。判定产出 PDF 是否真的嵌入了能渲染中文的字体。
#:
#: 为什么用"读产出 PDF 的嵌入字体"而不是别的办法（三条都实测否决过）：
#: - ``fc-list``：macOS 默认不带，返回空会造成恒定误警；Linux 缺字体时它本身也
#:   可能不存在，恰好在最该报警的场景失效。
#: - CDP ``CSS.getPlatformFontsForNode``：结果正确但不稳定——反复
#:   ``new_cdp_session`` 会让 nodeId 失效，实测同一页第三次调用即返回空。
#: - ``document.fonts.check`` / canvas 测宽：对不存在的字体也返回命中；全角中文
#:   字宽恒为 ``size × N``，任何字体测出来都一样，没有区分力。
#: 读嵌入字体名是结果导向的：字体真的缺失时 PDF 里不会出现任何 CJK 字体。
_CJK_FONT_MARKERS = (
    "song", "hei", "kai", "ming", "yuan", "fang",
    "noto", "fandol", "source", "wenquanyi", "wqy", "cjk", "droidsans",
)


def _embedded_cjk_fonts(doc) -> list[str]:
    """列出 PDF 里疑似能渲染中文的嵌入字体（按名称特征启发式判定）。"""
    names: set[str] = set()
    for page_index in range(doc.page_count):
        for font in doc[page_index].get_fonts():
            # get_fonts() 返回元组，索引 3 是 basefont 名（可能带 ABCDEF+ 前缀）
            base = str(font[3]).split("+")[-1]
            if base:
                names.add(base)
    lowered = {name: name.lower() for name in names}
    return sorted(
        name for name, low in lowered.items()
        if any(marker in low for marker in _CJK_FONT_MARKERS)
    )


def _warn_if_no_cjk_font(doc) -> list[str]:
    """产出 PDF 未嵌入任何 CJK 字体时告警（弃权，不阻断）。

    字体缺失时 Chrome 会把中文渲染成方框，但文本提取仍可能拿到正确 Unicode，
    因此覆盖率门禁检测不到这类问题——需要这道独立检查。判定为启发式，只告警。
    """
    fonts = _embedded_cjk_fonts(doc)
    if not fonts:
        logger.warning(
            "产出 PDF 未嵌入任何可识别的 CJK 字体：中文可能渲染为方框。"
            "请在本机安装 Noto CJK / 思源字体后重新生成。"
        )
    else:
        logger.debug("PDF 已嵌入 CJK 字体：%s", ", ".join(fonts))
    return fonts


def _find_chart_paths(project_dir: Path, fragment: str) -> str:
    """把图表 src 重写为 file:// 绝对路径，并去 loading=lazy 改 eager。"""
    charts_dir = (project_dir / "05_charts").resolve()

    def rewrite(match: re.Match[str]) -> str:
        return f'src="file://{charts_dir / match.group(2)}"'

    fragment = API_CHART.sub(rewrite, fragment)
    fragment = fragment.replace('loading="lazy"', 'loading="eager"')
    return fragment


def _build_document(
    project_dir: Path,
    fragment: str,
    *,
    toc_entries: list[TocEntry] | None,
) -> str:
    """组装完整 HTML 文档。

    toc_entries 为 None 时不含目录；否则注入带页码的目录。pass1 传占位页码
    （page=0 → 渲染成 __PAGE__ 占位），pass2 传真实页码。目录页恒占第 2 页，
    保证两趟打印正文分页一致。
    """
    css = _CSS_PATH.read_text(encoding="utf-8")
    # :root 色值由 design-tokens.json 生成后注入；CSS 文件里只保留 var() 引用。
    from .design_tokens import css_root_variables

    root_vars = css_root_variables()
    fragment = _find_chart_paths(project_dir, fragment)

    title_match = re.search(r"<h1[^>]*>(.*?)</h1>", fragment, re.S)
    raw_title = title_match.group(1) if title_match else project_dir.name
    title = re.sub(r"<[^>]+>", "", raw_title).strip() or project_dir.name

    toc_html = ""
    if toc_entries is not None:
        items = []
        for entry in toc_entries:
            # 目录页只列 h1/h2；h3 只进 PDF 书签，避免目录过碎（腾讯报告 h3 有 60+ 条）。
            if entry.heading.level > TOC_MAX_LEVEL:
                continue
            cls = "lvl1" if entry.heading.level == 1 else "lvl2"
            page_display = PAGE_PLACEHOLDER if entry.page == 0 else str(entry.page)
            items.append(
                f'    <li class="{cls}" data-anchor="{escape(entry.heading.anchor, quote=True)}">'
                f"<span>{escape(entry.heading.text)}</span>"
                f'<span class="dots"></span><span class="pg">{page_display}</span></li>'
            )
        toc_html = (
            '<div class="toc">\n  <h2>目录</h2>\n  <ol>\n'
            + "\n".join(items)
            + "\n  </ol>\n</div>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>{root_vars}\n{css}</style>
</head>
<body>
<div class="cover">
  <div class="kicker">DEEP RESEARCH</div>
  <div class="rule"></div>
  <h1>{escape(title)}</h1>
  <div class="subtitle">深度研究报告</div>
  <div class="meta">
    <div><b>报告日期</b>　{escape(date.today().isoformat())}</div>
    <div><b>报告版本</b>　v1.0 · 打印管线</div>
    <div><b>生成方式</b>　Research Agent 自动生成</div>
  </div>
  <div class="foot">本报告基于公开信息和用户授权材料自动整理，仅供研究参考，不构成任何投资建议。</div>
</div>

{toc_html}
{fragment}
</body>
</html>
"""


def _print_pdf(html_path: Path, pdf_path: Path) -> None:
    """用 Playwright chromium 打印 PDF（无 header/footer，背景开）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "REPORT_PDF_ENGINE=chrome 需要 playwright，但当前环境未安装："
            "请先运行 `pip install playwright`，再运行 `playwright install chromium`。"
        ) from exc

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(html_path.as_uri(), wait_until="networkidle")
                # 等待图片/图表全部加载完成
                page.wait_for_function(
                    "() => Array.from(document.images).every(img => img.complete)"
                )
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "24mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
                )
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover
        # 可能是 playwright 驱动启动失败，或 chromium 未安装导致的 launch 失败。
        # 用户显式选了 chrome 引擎，绝不静默回退，而是给出明确的安装指引。
        raise RuntimeError(
            "REPORT_PDF_ENGINE=chrome 打印失败：Playwright 或 Chromium 不可用。"
            "请先运行 `pip install playwright`，再运行 `playwright install chromium`。"
            f"（原始错误：{exc}）"
        ) from exc


def _locate_heading_pages(doc, headings: list[Heading]) -> list[TocEntry]:
    """用 PyMuPDF 定位每个标题所在页号。

    关键点：
    - 目录页（第 2 页）包含所有标题文本，必须跳过。
    - 标题文本可能作为子串出现在正文句子里（如"详见综合结论…"），因此按
      "整行文本 == 标题" 判定（标题独占一行），避免子串误匹配。实测腾讯/快手/
      SK 三份报告所有 h2 标题文本均恰好以整行形式出现一次。
    - h1 标题在封面（第 1 页）。
    """
    entries: list[TocEntry] = []
    for heading in headings:
        if heading.level == 1:
            entries.append(TocEntry(heading=heading, page=1))
            continue
        page = 0
        # 跳过封面（index 0）与目录页（index 1），从正文第 3 页起搜索。
        for page_index in range(2, doc.page_count):
            page_dict = doc[page_index].get_text("dict")
            found = False
            for block in page_dict["blocks"]:
                for line in block.get("lines", []):
                    line_text = "".join(span["text"] for span in line["spans"]).strip()
                    if line_text == heading.text:
                        found = True
                        break
                if found:
                    break
            if found:
                page = page_index + 1
                break
        entries.append(TocEntry(heading=heading, page=page))
    return entries


def _compute_coverage(doc, html_plain_len: int) -> float:
    """PDF 提取文本长度 / HTML 纯文本长度。"""
    import fitz

    pdf_text = "".join(doc[page_index].get_text() for page_index in range(doc.page_count))
    pdf_len = len(re.sub(r"\s+", "", pdf_text))
    if html_plain_len == 0:
        return 0.0
    return pdf_len / html_plain_len


#: running header 文字基线 y 坐标（pt）。@page margin-top=24mm≈68pt，正文通常从
#: y≈70pt 起排，因此 50pt 落在空白页眉带内。
HEADER_BASELINE_Y = 50.0
#: 判定"页眉带被正文侵入"的阈值：正文块顶边高于此值即视为侵入。
HEADER_CLEAR_Y = 60.0


def _inject_running_headers(
    doc,
    toc_entries: list[TocEntry],
) -> list[int]:
    """用 pass1 的页号映射决定每页章节名，在 PDF 页眉带写入 running header。

    不用 string-set（裸 Chrome 不支持），不用 offsetTop 换算（屏幕坐标与打印分页
    无关）。返回被跳过的页号列表。

    为什么需要侵入检测：``break-after: avoid`` 会把"标题 + 紧随的大图"整体推到
    下一页，此时标题排在 y≈34pt——比页眉基线更高，直接写会与标题重叠。实测腾讯
    第 20 页、SK 第 11 页各有一处。这类页宁可不写 header，也不能压住正文文字。
    """
    import fitz

    # 页号 → 该页起生效的章节名（只用进目录的层级，h3 不参与页眉）
    page_section: dict[int, str] = {}
    for entry in toc_entries:
        if entry.page <= 0 or entry.heading.level > TOC_MAX_LEVEL:
            continue
        page_section.setdefault(entry.page, entry.heading.text)

    skipped: list[int] = []
    current_section = ""
    for page_index in range(doc.page_count):
        page_number = page_index + 1
        if page_number in page_section:
            current_section = page_section[page_number]
        # 只在正文页（跳过封面第 1 页与目录第 2 页）注入。
        if page_number <= 2:
            continue
        page = doc[page_index]
        # 侵入检测：页眉带内已有正文内容时跳过，避免覆盖正文文字。
        if any(block[1] < HEADER_CLEAR_Y for block in page.get_text("blocks")):
            skipped.append(page_number)
            continue
        header_text = f"Research Agent 深度研究 | {page_number} | {current_section}"
        page.insert_text(
            fitz.Point(51, HEADER_BASELINE_Y),
            header_text,
            fontsize=7.5,
            fontname="china-s",
            color=(0.54, 0.59, 0.64),
        )
    return skipped


def _build_bookmarks(doc, toc_entries: list[TocEntry]) -> int:
    """用 pass1 页号映射写 PDF 书签（h1 + h2 + h3），返回书签数。

    书签比目录深一层是常规做法：目录页只列 h1/h2 保持简洁，PDF 侧边栏则给到 h3
    以便实际导航。只取到 h3——h4 以下过碎反而难用。
    """
    toc = [
        [entry.heading.level, entry.heading.text, entry.page]
        for entry in toc_entries
        if entry.page > 0
    ]
    doc.set_toc(toc)
    return len(toc)


def _rebuild_toc_links(doc, toc_entries: list[TocEntry]) -> None:
    """目录条目改 kind=1 GOTO 内部链接（Chrome 默认输出 LAUNCH 外部文件链接）。"""
    import fitz

    toc_page_index = 1  # 封面第 1 页，目录第 2 页
    if doc.page_count < 2:
        return
    toc_page = doc[toc_page_index]
    # 目录条目的页码文本与标题文本都在目录页；按 data-anchor 无法直接对应，
    # 这里按标题文本逐条定位目录页上的文本块，把该块改为内部链接。
    for entry in toc_entries:
        if entry.page <= 0 or entry.heading.level > TOC_MAX_LEVEL:
            continue
        # 找到目录页上标题文本所在的文本块，取其 bbox
        blocks = toc_page.get_text("blocks")
        target_text = entry.heading.text
        for block in blocks:
            if block[4].strip() == target_text or target_text in block[4]:
                rect = fitz.Rect(block[0], block[1], block[2], block[3])
                # GOTO 内部跳转：kind=1, page=目标页（0-based）
                link = {
                    "kind": fitz.LINK_GOTO,
                    "from": rect,
                    "page": entry.page - 1,
                }
                toc_page.insert_link(link)
                break


def generate_print_pdf(
    *,
    project_dir: Path,
    html_path: Path | None = None,
    out_pdf_path: Path | None = None,
    keep_intermediate: bool = False,
) -> PrintResult:
    """两趟打印生成正式 PDF，含内容覆盖率硬门禁。

    返回 PrintResult（含覆盖率、页数、书签数等）。
    """
    import fitz

    html_path = html_path or (project_dir / config.FILE_FINAL_REPORT_HTML)
    if not html_path.is_file():
        raise FileNotFoundError(f"找不到 {html_path}")
    fragment = html_path.read_text(encoding="utf-8")
    html_plain_len = len(_plain_text(fragment))

    headings = _extract_headings(fragment)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # ── pass1：带占位页码的目录（保证目录页存在，正文分页与 pass2 一致）──
        # 目录独立成页（break-before:page），只要 pass1 也带目录页，正文分页就不变。
        placeholder_entries = [
            TocEntry(heading=h, page=0) for h in headings
        ]
        pass1_html = tmp_dir / "pass1.html"
        pass1_html.write_text(
            _build_document(project_dir, fragment, toc_entries=placeholder_entries),
            encoding="utf-8",
        )
        pass1_pdf = tmp_dir / "pass1.pdf"
        _print_pdf(pass1_html, pass1_pdf)

        doc1 = fitz.open(pass1_pdf)
        pass1_pages = doc1.page_count
        toc_entries = _locate_heading_pages(doc1, headings)
        doc1.close()

        # ── pass2：注入真实页码目录 + running header ──────────
        pass2_html = tmp_dir / "pass2.html"
        pass2_html.write_text(
            _build_document(project_dir, fragment, toc_entries=toc_entries),
            encoding="utf-8",
        )
        pass2_pdf = tmp_dir / "pass2.pdf"
        _print_pdf(pass2_html, pass2_pdf)

        doc2 = fitz.open(pass2_pdf)
        pass2_pages = doc2.page_count

        # pass1 与 pass2 页数不一致时报错（目录页码不改变分页）
        if pass1_pages != pass2_pages:
            raise DeterministicContentError(
                f"两趟打印页数不一致：pass1={pass1_pages}, pass2={pass2_pages}"
                f"（{DETERMINISTIC_CONTENT_HINT}）"
            )

        # 内容覆盖率硬门禁
        coverage = _compute_coverage(doc2, html_plain_len)
        if coverage < MIN_COVERAGE:
            doc2.close()
            raise DeterministicContentError(
                f"打印 PDF 内容覆盖率不足：{coverage:.1%} < {MIN_COVERAGE:.0%}"
                f"（疑似渲染引擎静默丢内容，{DETERMINISTIC_CONTENT_HINT}）"
            )

        # 后处理：running header + 书签 + 目录内部链接
        # CJK 字体检查放在这里：读产出 PDF 实际嵌入的字体（结果导向，跨平台）。
        _warn_if_no_cjk_font(doc2)
        header_skipped = _inject_running_headers(doc2, toc_entries)
        if header_skipped:
            logger.info(
                "%d 页因页眉带被正文占用而未写 running header：%s",
                len(header_skipped),
                header_skipped,
            )
        bookmark_count = _build_bookmarks(doc2, toc_entries)
        _rebuild_toc_links(doc2, toc_entries)

        # pass3：复现验证（重打一遍确认页数一致）
        pass3_pdf = tmp_dir / "pass3.pdf"
        _print_pdf(pass2_html, pass3_pdf)
        doc3 = fitz.open(pass3_pdf)
        pass3_pages = doc3.page_count
        doc3.close()
        if pass3_pages != pass2_pages:
            logger.warning("pass2=%d 页, pass3=%d 页（不可复现）", pass2_pages, pass3_pages)

        # 输出
        out_pdf = out_pdf_path or (project_dir / config.FILE_FINAL_REPORT_PDF)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        # 保存最终带书签/链接/header 的版本
        doc2.save(str(out_pdf), garbage=4, deflate=True)
        doc2.close()

        if keep_intermediate:
            for name, src in (("pass1.html", pass1_html), ("pass2.html", pass2_html)):
                shutil.copyfile(src, out_pdf.with_name(out_pdf.stem + f".{name}"))

    return PrintResult(
        pdf_path=out_pdf,
        page_count=pass2_pages,
        coverage=coverage,
        headings=headings,
        toc_entries=toc_entries,
        bookmark_count=bookmark_count,
    )


__all__ = [
    "PrintResult",
    "Heading",
    "TocEntry",
    "generate_print_pdf",
]
