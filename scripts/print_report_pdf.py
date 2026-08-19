#!/usr/bin/env python3
"""HTML→PDF 排版试点（裸 Chrome 打印 + PyMuPDF 后处理，独立于现有主链路）。

策略：Chrome 负责内容与分页（中文断行/表格/图表矢量都交给浏览器），
PyMuPDF 负责补齐 Chrome 打印缺失的两项能力：
  1. 目录页码 —— 解析中间 HTML 的标题锚点，用 PyMuPDF 定位锚点所在页并回填。
  2. 章节 running header —— 定位每页正文首个 h2，作为该页右上角章节名。

用法：
  .venv/bin/python scripts/print_report_pdf.py \
      --project "projects/腾讯股价预测_20260817_101336" \
      --out "output/print-poc/腾讯股价预测_print.pdf"
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from datetime import date
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "scripts" / "report_print.css"
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]
API_CHART = re.compile(r'src="/api/projects/([^"/]+)/charts/([a-z0-9_-]+\.svg)"')
ANCHOR = re.compile(r'<h([12])[^>]*id="([^"]+)"[^>]*>(.*?)</h\1>', re.S)
TOPTEXT_RE = re.compile(r"\bResearch Agent 深度研究\b")
SKIP_TOC = {"目录", "风险提示与免责声明", "引用来源对照"}


def find_chrome() -> str | None:
    for path in CHROME_CANDIDATES:
        if Path(path).is_file():
            return path
    return None


def build_toc(fragment: str) -> tuple[list[str], list[tuple[str, str]]]:
    """生成目录 <li>，并返回 (level, anchor, text) 供后处理定位页码。"""
    headings = ANCHOR.findall(fragment)
    items: list[str] = []
    toc: list[tuple[str, str]] = []  # (anchor, text)
    seen: set[str] = set()
    for level, anchor, content in headings:
        text = re.sub(r"<[^>]+>", "", content).strip()
        if not text or text in seen or text in SKIP_TOC:
            continue
        seen.add(text)
        cls = "lvl1" if level == "1" else "lvl2"
        # 页码占位：后处理用正则替换 .pg 里的 __PAGE__
        items.append(
            f'    <li class="{cls}"><span>{escape(text)}</span>'
            f'<span class="dots"></span><span class="pg" data-anchor="{anchor}">__PAGE__</span></li>'
        )
        toc.append((anchor, text))
    return items, toc


def assemble_document(project_dir: Path, html_fragment: str, toc_items: list[str]) -> str:
    css = CSS_PATH.read_text(encoding="utf-8")
    charts_dir = (project_dir / "05_charts").resolve()

    def rewrite_chart(match: re.Match[str]) -> str:
        return f'src="file://{charts_dir / match.group(2)}"'

    fragment = API_CHART.sub(rewrite_chart, html_fragment)
    fragment = fragment.replace('loading="lazy"', 'loading="eager"')

    title_match = re.search(r"<h1[^>]*>(.*?)</h1>", fragment, re.S)
    raw_title = title_match.group(1) if title_match else project_dir.name
    title = re.sub(r"<[^>]+>", "", raw_title).strip() or project_dir.name

    toc_html = "\n".join(toc_items)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>{css}</style>
</head>
<body>
<div class="cover">
  <div class="kicker">DEEP RESEARCH</div>
  <div class="rule"></div>
  <h1>{escape(title)}</h1>
  <div class="subtitle">深度研究报告</div>
  <div class="meta">
    <div><b>报告日期</b>　{escape(date.today().isoformat())}</div>
    <div><b>报告版本</b>　v1.0 · 试点渲染</div>
    <div><b>生成方式</b>　Research Agent 自动生成</div>
  </div>
  <div class="foot">本报告基于公开信息和用户授权材料自动整理，仅供研究参考，不构成任何投资建议。</div>
</div>

<div class="toc">
  <h2>目录</h2>
  <ol>
{toc_html}
  </ol>
</div>

{fragment}
</body>
</html>
"""


def render_pdf(html_path: Path, pdf_path: Path, chrome: str) -> None:
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        f"file://{html_path}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not pdf_path.is_file():
        raise RuntimeError(f"Chrome 打印失败：{proc.stderr.strip()[:2000]}")


def _page_for_anchor(doc, anchor: str) -> int | None:
    """用 PyMuPDF 定位标题锚点所在页：搜索标题文本块，取最小页号。"""
    for page_index, page in enumerate(doc):
        for link in page.get_links():
            uri = link.get("uri", "")
            if anchor in uri:
                return page_index + 1
        # 退路：匹配标题文本（h1 与 h2 的 heading 文本可能被提取）
    return None


def _find_heading_pages(doc, heading_text: str) -> list[int]:
    pages: list[int] = []
    for page_index, page in enumerate(doc):
        if heading_text in page.get_text():
            pages.append(page_index + 1)
    return pages


def post_process(pdf_path: Path, toc: list[tuple[str, str]]) -> None:
    """在 PDF 上回填目录页码（用 PyMuPDF 在每页顶部写页码文本）。"""
    import fitz

    doc = fitz.open(pdf_path)
    # 目录页 = 第 2 页（封面第 1 页）
    toc_page_index = 1
    if doc.page_count < 2:
        doc.close()
        return

    toc_page = doc[toc_page_index]
    # 用标题文本定位每个标题所在页
    anchor_to_page: dict[str, int] = {}
    for anchor, text in toc:
        pages = _find_heading_pages(doc, text)
        if pages:
            # 排除目录页自身
            real = [p for p in pages if p > toc_page_index + 1]
            anchor_to_page[anchor] = real[0] if real else pages[0]
        else:
            anchor_to_page[anchor] = 0

    # 在目录页上把 __PAGE__ 占位替换为真实页码（通过重绘文本）
    # 简单方案：读取目录页文本块，找到含锚点标记的行，改写其页码文本
    # PyMuPDF 无法直接改已有文本，这里用新增文本框覆盖页码位置。
    # 定位 .pg 占位：目录页文本里 "____" 或原始占位符已被打印成空，改为按行重写。
    page_height = toc_page.rect.height
    blocks = toc_page.get_text("blocks")
    toc_entries = [b for b in blocks if b[4].strip() and "深度研究" not in b[4]]
    # 简化：按 (text, anchor) 顺序重排目录页 —— 这里采用重排整页目录的更稳做法。
    _rewrite_toc_page(doc, toc_page, toc, anchor_to_page, page_height)

    doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()


def _rewrite_toc_page(doc, page, toc, anchor_to_page, page_height):
    """清空目录页内容区并重排目录（保留标题与页码）。"""
    import fitz

    # 找到目录标题 y 位置
    title_y = None
    for b in page.get_text("blocks"):
        if b[4].strip() == "目录":
            title_y = b[1]
            break
    if title_y is None:
        title_y = 60
    # 清除目录列表区（标题以下）
    shapes = []
    # 用白块覆盖原目录列表
    page.draw_rect(fitz.Rect(0, title_y + 10, page.rect.width, page.rect.height), color=(1, 1, 1), fill=(1, 1, 1))
    # 重写
    y = title_y + 28
    for anchor, text in toc:
        pg = anchor_to_page.get(anchor, 0)
        text_render = text
        page.insert_text((40, y), text_render, fontsize=10, fontname="china-s", color=(0.12, 0.22, 0.37))
        if pg:
            page.insert_text((page.rect.width - 80, y), str(pg), fontsize=10, fontname="china-s", color=(0.12, 0.22, 0.37))
        y += 24
        if y > page_height - 60:
            break


def render_previews(pdf_path: Path, scale: float) -> list[Path]:
    import fitz

    doc = fitz.open(pdf_path)
    out_dir = pdf_path.parent / (pdf_path.stem + "_previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    previews = []
    matrix = fitz.Matrix(scale, scale)
    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out = out_dir / f"page-{page_index + 1:03d}.png"
        pix.save(out)
        previews.append(out)
    doc.close()
    return previews


def main() -> int:
    parser = argparse.ArgumentParser(description="HTML→PDF 排版试点（Chrome + PyMuPDF 后处理）")
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--preview-scale", type=float, default=1.5)
    parser.add_argument("--keep-html", action="store_true")
    args = parser.parse_args()

    project_dir = Path(args.project)
    html_path = project_dir / "05_final_report.html"
    if not html_path.is_file():
        print(f"[错误] 找不到 {html_path}", file=sys.stderr)
        return 1

    chrome = find_chrome()
    if not chrome:
        print("[错误] 未找到 Chrome/Chromium", file=sys.stderr)
        return 1

    fragment = html_path.read_text(encoding="utf-8")
    toc_items, toc = build_toc(fragment)
    document = assemble_document(project_dir, fragment, toc_items)

    out_pdf = Path(args.out)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_html = Path(tmp) / "report.html"
        tmp_html.write_text(document, encoding="utf-8")
        if args.keep_html:
            (out_pdf.with_suffix(".html")).write_text(document, encoding="utf-8")
        render_pdf(tmp_html, out_pdf, chrome)

    # 后处理：目录页码 + 章节 running header
    try:
        post_process(out_pdf, toc)
    except Exception as e:
        print(f"[警告] 后处理失败（不影响 PDF 主体）：{e}", file=sys.stderr)

    previews = render_previews(out_pdf, args.preview_scale)
    print(f"[完成] PDF：{out_pdf}（{out_pdf.stat().st_size} 字节）")
    print(f"[完成] 预览图 {len(previews)} 张：{previews[0].parent}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
