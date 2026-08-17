"""Pandoc-based HTML/LaTeX/PDF delivery for brokerage-style reports."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config
from .agent_skills import load_project_skill
from .llm import LLMClient
from .report_charts import (
    ChartAsset,
    ChartManifest,
    ChartSpec,
    load_chart_manifest,
    prepare_llm_fallbacks,
    render_chart_manifest,
    SUPPORTED_CHART_TYPES,
)

_PLACEHOLDER = re.compile(
    r"^[ \t]*\{\{chart:([a-z0-9][a-z0-9_-]{0,63})\}\}[ \t]*$",
    re.MULTILINE,
)
_REPORT_CITATION = re.compile(
    r"\[src:\s*(src_[A-Za-z0-9_-]+)(?::v\d+)?(?:\s*,[^\]\r\n]*)?\]",
    re.IGNORECASE,
)
_EVIDENCE_ID = re.compile(r"\bev_([0-9a-f]{32})\b", re.IGNORECASE)
_SOURCE_ID = re.compile(r"\bsrc_([0-9a-f]{32})\b", re.IGNORECASE)
_INLINE_CODE = re.compile(r"`([^`\r\n]+)`")
_PDF_HEADING = re.compile(r"^(#{2,6}[ \t]+)(.+)$", re.MULTILINE)
_CHINESE_HEADING_NUMBER = re.compile(
    r"^(?:第)?[〇零一二三四五六七八九十百]+[、.．][ \t]*"
)
_CHAPTER_HEADING_NUMBER = re.compile(
    r"^(?:章节[ \t]*\d+(?:\.\d+)*|第[ \t]*\d+[ \t]*章)"
    r"[ \t]*[：:、.．][ \t]*"
)
_ARABIC_HEADING_NUMBER = re.compile(
    r"^(?:\d+(?:\.\d+)+[ \t]+|\d+[.．、)][ \t]*)"
)
_DISCLAIMER = "本报告基于公开信息和用户授权材料自动整理，仅供研究参考，不构成任何投资建议。"
_PLAIN_TEXT_REPLACEMENTS = {
    "✅": "",
    "⚠️": "注意：",
    "⚠": "注意：",
    "🔴": "",
    "🟡": "",
    "🟢": "",
    "❌": "不确定",
    "≈": "约",
    "≥": ">=",
    "≤": "<=",
    "σ": "sigma",
    "β": "beta",
    "↔": " <-> ",
    "週": "周",
    "佈": "布",
    "▪": "-",
    "①": "(1)",
    "②": "(2)",
    "③": "(3)",
    "④": "(4)",
    "⑤": "(5)",
    "‑": "-",
    "–": "-",
    "—": "-",
    "→": " -> ",
    "←": " <- ",
    "️": "",
}


def find_pandoc() -> str | None:
    configured = config.REPORT_PANDOC_BIN
    if Path(configured).is_file():
        return str(Path(configured).resolve())
    found = shutil.which(configured)
    if found:
        return found
    try:
        import pypandoc

        bundled = pypandoc.get_pandoc_path()
        return bundled if Path(bundled).is_file() else None
    except (ImportError, OSError):
        return None


def find_latex_engine() -> str | None:
    configured = config.REPORT_LATEX_ENGINE
    if Path(configured).is_file():
        # TeX distributions commonly expose ``xelatex`` as a symlink to the
        # underlying ``xetex`` binary.  Preserving argv[0] is significant:
        # resolving that symlink makes XeTeX load the plain-TeX format instead
        # of the LaTeX format.
        return str(Path(configured).absolute())
    found = shutil.which(configured)
    if found:
        return found
    texbin = Path("/Library/TeX/texbin") / configured
    return str(texbin) if texbin.is_file() else None


def _ensure_disclaimer(markdown: str) -> str:
    if _DISCLAIMER in markdown:
        return markdown
    return (
        markdown.rstrip()
        + "\n\n---\n\n## 风险提示与免责声明\n\n"
        + _DISCLAIMER
        + "\n"
    )


def render_report_citations_for_html(markdown: str, project_id: str) -> str:
    """Render source markers as numbered material links before Pandoc runs."""
    citation_numbers: dict[str, int] = {}

    def citation_markup(match: re.Match[str]) -> str:
        source_id = match.group(1)
        number = citation_numbers.setdefault(source_id, len(citation_numbers) + 1)
        href = (
            f"/materials?project={quote(project_id, safe='')}"
            f"&source={quote(source_id, safe='')}"
        )
        title = escape(f"来源 {number}：{source_id}", quote=True)
        return (
            f'<a class="source-citation" href="{href}" '
            f'data-source-id="{source_id}" data-citation-number="{number}" '
            f'title="{title}" aria-label="{title}"><sup>{number}</sup></a>'
        )

    def inline_code(match: re.Match[str]) -> str:
        code = match.group(1)
        rendered = _REPORT_CITATION.sub(citation_markup, code)
        return rendered if rendered != code else match.group(0)

    return _REPORT_CITATION.sub(
        citation_markup,
        _INLINE_CODE.sub(inline_code, markdown),
    )


def render_report_citations_for_latex(markdown: str) -> str:
    """Replace internal evidence locators with compact printable references."""
    citation_numbers: dict[str, int] = {}

    def citation_markup(match: re.Match[str]) -> str:
        source_id = match.group(1)
        number = citation_numbers.setdefault(source_id, len(citation_numbers) + 1)
        return f"<sup>[{number}]</sup>"

    def inline_code(match: re.Match[str]) -> str:
        code = match.group(1)
        rendered = _REPORT_CITATION.sub(citation_markup, code)
        return rendered if rendered != code else match.group(0)

    return _REPORT_CITATION.sub(
        citation_markup,
        _INLINE_CODE.sub(inline_code, markdown),
    )


def abbreviate_internal_ids_for_pdf(markdown: str) -> str:
    """Keep appendix identifiers traceable without forcing 32-char table cells."""
    abbreviated = _EVIDENCE_ID.sub(lambda match: f"E-{match.group(1)[:8]}", markdown)
    return _SOURCE_ID.sub(lambda match: f"S-{match.group(1)[:8]}", abbreviated)


def normalize_heading_numbers_for_pdf(markdown: str) -> str:
    """Remove author-written numbering where the PDF template numbers headings."""

    def normalize(match: re.Match[str]) -> str:
        marker, title = match.groups()
        title = _CHINESE_HEADING_NUMBER.sub("", title, count=1)
        title = _CHAPTER_HEADING_NUMBER.sub("", title, count=1)
        title = _ARABIC_HEADING_NUMBER.sub("", title, count=1)
        return marker + title

    return _PDF_HEADING.sub(normalize, markdown)


def normalize_markdown_for_pdf(markdown: str) -> str:
    """Remove glyphs and syntax that are unsafe in the deterministic PDF path."""
    normalized = re.sub(
        r"⭐{1,5}",
        lambda match: f"{len(match.group(0))}/5",
        markdown,
    )
    normalized = re.sub(
        r"[★☆]{1,5}",
        lambda match: f"{match.group(0).count('★')}/{len(match.group(0))}",
        normalized,
    )
    for source, replacement in _PLAIN_TEXT_REPLACEMENTS.items():
        normalized = normalized.replace(source, replacement)
    return normalized


def make_latex_source_ids_breakable(tex: str) -> str:
    """Add safe line-break points to long escaped source identifiers."""
    pattern = re.compile(r"src\\_([0-9a-f]{32})")

    def replacement(match: re.Match[str]) -> str:
        source_hash = match.group(1)
        chunks = [source_hash[index:index + 8] for index in range(0, 32, 8)]
        return r"src\_" + r"\allowbreak{}".join(chunks)

    return pattern.sub(replacement, tex)


# Long unbreakable runs are the main cause of Overfull \hbox inside narrow table
# cells: domains, URLs and hyphenated slugs carry no natural break point, so TeX
# is forced to push them past the column edge.
_LONG_TOKEN = re.compile(
    r"(?<![\\{A-Za-z0-9._/:-])[A-Za-z0-9][A-Za-z0-9._/:\-]{15,}"
)
_BREAK_AFTER = re.compile(r"([._/:\-])")
# Runs with no punctuation at all still need help; break every N characters.
_HARD_CHUNK = 12
# Lines carrying markup arguments (paths, labels, links) must stay byte-exact.
_UNSAFE_LINE = re.compile(
    r"\\(?:includegraphics|hypertarget|label|ref|href|url|input|include|"
    r"usepackage|documentclass|graphicspath|newcommand|renewcommand|def|"
    r"begin\{verbatim|begin\{lstlisting|begin\{Highlighting)"
)


def _split_long_token(token: str) -> str:
    parts = _BREAK_AFTER.split(token)
    rebuilt: list[str] = []
    for part in parts:
        if not part:
            continue
        if part in "._/:-":
            rebuilt.append(part + r"\allowbreak{}")
            continue
        # A punctuation-free run still has to break somewhere.
        if len(part) > _HARD_CHUNK:
            chunks = [
                part[index:index + _HARD_CHUNK]
                for index in range(0, len(part), _HARD_CHUNK)
            ]
            rebuilt.append(r"\allowbreak{}".join(chunks))
        else:
            rebuilt.append(part)
    return "".join(rebuilt)


def make_latex_long_tokens_breakable(tex: str) -> str:
    """Insert ``\\allowbreak`` inside long ASCII runs in document text.

    Lines that carry LaTeX markup arguments (image paths, labels, hyperlinks,
    verbatim) are left untouched so we never corrupt the document structure.
    """
    lines = tex.split("\n")
    for index, line in enumerate(lines):
        if not line or "\\" in line and _UNSAFE_LINE.search(line):
            continue
        lines[index] = _LONG_TOKEN.sub(
            lambda match: _split_long_token(match.group(0)), line
        )
    return "\n".join(lines)


def _fallback_table(chart: ChartSpec) -> str:
    headers = ["项目", *chart.labels]
    separator = ["---", *["---:" for _ in chart.labels]]
    rows = []
    for series in chart.series:
        values = ["—" if value is None else f"{value:g}" for value in series.values]
        rows.append([series.name, *values])
    lines = [
        f"**{chart.title}（图表降级为数据表）**",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(separator) + " |",
        *["| " + " | ".join(row) + " |" for row in rows],
        "",
        f"> 单位：{chart.unit}；截至：{chart.as_of_date}；资料来源：{chart.source}",
    ]
    return "\n".join(lines)


def replace_chart_placeholders(
    markdown: str,
    manifest: ChartManifest,
    assets: dict[str, ChartAsset],
    *,
    target: str,
    project_id: str = "",
) -> str:
    chart_map = {chart.id: chart for chart in manifest.charts}
    used = set(_PLACEHOLDER.findall(markdown))
    unknown = used - set(chart_map)
    if unknown:
        raise ValueError(f"Markdown 引用了不存在的图表：{sorted(unknown)}")
    unused_required = {chart.id for chart in manifest.charts if chart.required} - used
    if unused_required:
        raise ValueError(f"必需图表未在 Markdown 中使用：{sorted(unused_required)}")

    def replacement(match: re.Match[str]) -> str:
        chart = chart_map[match.group(1)]
        asset = assets.get(chart.id)
        if asset is None:
            if chart.required:
                raise ValueError(f"必需图表没有渲染资产：{chart.id}")
            return _fallback_table(chart)
        note = f"；备注：{chart.note}" if chart.note else ""
        if target == "html":
            source = escape(chart.source)
            title = escape(chart.title)
            unit = escape(chart.unit)
            as_of = escape(chart.as_of_date)
            src = f"/api/projects/{quote(project_id, safe='')}/charts/{chart.id}.svg"
            return (
                f'<figure class="report-chart" id="chart-{chart.id}">'
                f'<img src="{src}" alt="{title}" loading="lazy">'
                f'<figcaption><strong>{title}</strong>'
                f'<small>单位：{unit}；截至：{as_of}；资料来源：{source}{escape(note)}</small>'
                "</figcaption></figure>"
            )
        if target == "latex":
            return (
                f"![{chart.title}](05_charts/{chart.id}.pdf)\n\n"
                f"> 单位：{chart.unit}；截至：{chart.as_of_date}；资料来源：{chart.source}{note}"
            )
        raise ValueError(f"未知图表替换目标：{target}")

    rendered = _PLACEHOLDER.sub(replacement, markdown)
    if "{{chart:" in rendered:
        raise ValueError("报告仍包含未解析的图表占位符")
    return rendered


def _run(command: list[str], *, cwd: Path, input_text: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=config.REPORT_RENDER_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"报告渲染超时（{config.REPORT_RENDER_TIMEOUT}s）：{command[0]}") from exc


def _sanitize_html(html: str) -> str:
    try:
        import bleach
    except ImportError as exc:
        raise RuntimeError("缺少 bleach，无法安全生成报告 HTML") from exc
    tags = {
        "a", "blockquote", "br", "caption", "code", "col", "colgroup", "div",
        "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "hr", "img",
        "li", "ol", "p", "pre", "section", "small", "span", "strong", "sub",
        "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
    attributes = {
        "a": [
            "href",
            "title",
            "aria-label",
            "data-source-id",
            "data-citation-number",
        ],
        "img": ["src", "alt", "loading", "width", "height"],
        "td": ["colspan", "rowspan", "align"],
        "th": ["colspan", "rowspan", "align"],
        "*": ["class", "id"],
    }
    return bleach.clean(
        html,
        tags=tags,
        attributes=attributes,
        protocols={"http", "https", "mailto"},
        strip=True,
    )


def build_report_html(
    *,
    topic: str,
    project_id: str,
    project_dir: Path,
    markdown: str,
    manifest: ChartManifest,
    assets: dict[str, ChartAsset],
) -> Path:
    pandoc = find_pandoc()
    if not pandoc:
        raise RuntimeError("缺少 Pandoc；请安装 pandoc 或项目依赖 pypandoc-binary")
    prepared = replace_chart_placeholders(
        _ensure_disclaimer(markdown), manifest, assets, target="html", project_id=project_id
    )
    prepared = render_report_citations_for_html(prepared, project_id)
    proc = _run(
        [pandoc, "--from=gfm+raw_html", "--to=html5", "--wrap=none"],
        cwd=project_dir,
        input_text=prepared,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Pandoc HTML 转换失败：{proc.stderr.strip()}")
    html_path = project_dir / config.FILE_FINAL_REPORT_HTML
    html_path.write_text(
        f'<article class="brokerage-report" data-topic="{escape(topic)}">{_sanitize_html(proc.stdout)}</article>',
        encoding="utf-8",
    )
    return html_path


def build_report_latex(
    *,
    topic: str,
    project_dir: Path,
    markdown: str,
    manifest: ChartManifest,
    assets: dict[str, ChartAsset],
) -> Path:
    pandoc = find_pandoc()
    if not pandoc:
        raise RuntimeError("缺少 Pandoc；请安装 pandoc 或项目依赖 pypandoc-binary")
    skill = load_project_skill(config.REPORT_FORMATTING_SKILL)
    template = skill.assets_dir / "brokerage-report.tex"
    style = skill.assets_dir / "brokerage-report.sty"
    table_filter = skill.assets_dir / "brokerage-report-tables.lua"
    if not template.is_file() or not style.is_file() or not table_filter.is_file():
        raise RuntimeError("券商研报 LaTeX 模板资产不完整")
    shutil.copyfile(style, project_dir / style.name)
    prepared = normalize_heading_numbers_for_pdf(
        normalize_markdown_for_pdf(
            replace_chart_placeholders(
                _ensure_disclaimer(markdown), manifest, assets, target="latex"
            )
        )
    )
    prepared = render_report_citations_for_latex(prepared)
    prepared = abbreviate_internal_ids_for_pdf(prepared)
    prepared = re.sub(r"\A# [^\n]+\n+", "", prepared, count=1)
    source_path = project_dir / "05_final_report.render.md"
    source_path.write_text(prepared, encoding="utf-8")
    tex_path = project_dir / config.FILE_FINAL_REPORT_TEX
    proc = _run(
        [
            pandoc,
            source_path.name,
            "--from=gfm-tex_math_dollars+raw_html",
            "--to=latex",
            "--standalone",
            "--shift-heading-level-by=-1",
            f"--lua-filter={table_filter}",
            f"--template={template}",
            f"--metadata=title:{topic} 调研报告",
            f"--metadata=date:{date.today().isoformat()}",
            f"--resource-path={project_dir}",
            f"--output={tex_path.name}",
        ],
        cwd=project_dir,
    )
    source_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Pandoc LaTeX 转换失败：{proc.stderr.strip()}")
    tex_path.write_text(
        make_latex_long_tokens_breakable(
            make_latex_source_ids_breakable(tex_path.read_text(encoding="utf-8"))
        ),
        encoding="utf-8",
    )
    return tex_path


_OVERFULL = re.compile(r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)")
_MISSING_GLYPH = re.compile(r"Missing character: There is no (.+?) \(")
# Right page margin is 19mm ≈ 54pt. Anything narrower than this stays inside the
# margin: visually tight, but not spilling off the paper. Only real spillover
# should block delivery — a 25pt overhang used to fail the whole run even though
# the PDF was perfectly printable.
_OVERFULL_TOLERANCE_PT = 54.0


def _layout_problems(log: str) -> tuple[list[str], list[str]]:
    """Split layout warnings into blocking problems and tolerable notes."""
    blocking: list[str] = []
    tolerable: list[str] = []
    for line in log.splitlines():
        match = _OVERFULL.search(line)
        if match:
            if float(match.group(1)) >= _OVERFULL_TOLERANCE_PT:
                blocking.append(line.strip())
            else:
                tolerable.append(line.strip())
            continue
        # A missing glyph means the character is silently dropped from the PDF —
        # always a content-integrity failure regardless of size.
        if _MISSING_GLYPH.search(line):
            blocking.append(line.strip())
    return blocking, tolerable


def compile_report_pdf(tex_path: Path) -> Path | None:
    engine = find_latex_engine()
    if not engine:
        return None
    env = dict(os.environ)
    skill = load_project_skill(config.REPORT_FORMATTING_SKILL)
    current_texinputs = env.get("TEXINPUTS", "")
    env["TEXINPUTS"] = f"{skill.assets_dir}{os.pathsep}{current_texinputs}"
    combined = ""
    for _ in range(2):
        proc = _run(
            [engine, "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            cwd=tex_path.parent,
            env=env,
        )
        combined += proc.stdout + "\n" + proc.stderr + "\n"
        if proc.returncode != 0:
            compile_log = tex_path.with_suffix(".compile.log")
            compile_log.write_text(combined, encoding="utf-8")
            raise RuntimeError(f"LaTeX 编译失败，日志：{compile_log.name}")

    blocking, tolerable = _layout_problems(combined)
    warning_path = tex_path.with_suffix(".layout-warnings.log")
    if blocking:
        report = ["# 阻断交付的排版问题", *blocking]
        if tolerable:
            report += ["", "# 可接受的轻微溢出（未阻断）", *tolerable]
        warning_path.write_text("\n".join(report) + "\n", encoding="utf-8")
        raise RuntimeError(
            f"PDF 存在 {len(blocking)} 处严重排版问题（文字溢出纸面或字形缺失）："
            f"{warning_path.name}"
        )
    if tolerable:
        # Keep them visible for tuning, but do not fail an otherwise good PDF.
        warning_path.write_text(
            "# 轻微溢出，仍在页边距内，未阻断交付\n" + "\n".join(tolerable) + "\n",
            encoding="utf-8",
        )
    else:
        warning_path.unlink(missing_ok=True)
    tex_path.with_suffix(".compile.log").unlink(missing_ok=True)
    pdf_path = tex_path.with_suffix(".pdf")
    return pdf_path if pdf_path.is_file() else None


def inspect_pdf(pdf_path: Path, *, topic: str) -> dict[str, Any]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("缺少 PyMuPDF，无法检查 PDF") from exc
    document = fitz.open(pdf_path)
    if document.page_count < 1:
        raise RuntimeError("PDF 没有有效页面")
    text = "\n".join(page.get_text() for page in document)
    if re.sub(r"\s+", "", topic) not in re.sub(r"\s+", "", text):
        raise RuntimeError("PDF 文本检查失败：缺少报告标题")
    if "不构成任何投资建议" not in text:
        raise RuntimeError("PDF 文本检查失败：缺少免责声明")
    preview_dir = pdf_path.parent / "tmp" / "pdfs"
    preview_dir.mkdir(parents=True, exist_ok=True)
    if document.page_count <= 8:
        sample_pages = list(range(document.page_count))
    else:
        image_pages = [index for index, page in enumerate(document) if page.get_images(full=True)]
        sample_pages = sorted({
            0,
            min(1, document.page_count - 1),
            document.page_count // 4,
            document.page_count // 2,
            (document.page_count * 3) // 4,
            document.page_count - 1,
            *image_pages[:3],
        })
    previews = []
    for page_index in sample_pages:
        page = document.load_page(page_index)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        output = preview_dir / f"{pdf_path.stem}-page-{page_index + 1}.png"
        pixmap.save(output)
        if output.stat().st_size < 5_000:
            raise RuntimeError(f"PDF 预览页异常：{output.name}")
        previews.append(str(output))
    result = {
        "page_count": document.page_count,
        "text_characters": len(text),
        "sample_pages": [index + 1 for index in sample_pages],
        "previews": previews,
    }
    document.close()
    (pdf_path.parent / "05_pdf_qa.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


async def generate_report_artifacts(
    *,
    topic: str,
    project_dir: Path,
    final_report_path: Path,
    client: LLMClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    del force  # Artifacts are deterministic and intentionally regenerated together.
    manifest_path = project_dir / config.FILE_CHART_MANIFEST
    manifest = load_chart_manifest(manifest_path)
    unsupported = [
        chart
        for chart in manifest.charts
        if chart.type not in SUPPORTED_CHART_TYPES and chart.vega_lite_spec is None
    ]
    if unsupported:
        if client is not None:
            await prepare_llm_fallbacks(manifest, project_dir=project_dir, client=client)
        else:
            async with LLMClient(
                base_url=config.LLM_BASE_URL,
                api_key=config.LLM_API_KEY,
                model=config.LLM_MODEL,
                timeout=config.LLM_TIMEOUT,
                max_retries=config.LLM_MAX_RETRIES,
            ) as active_client:
                await prepare_llm_fallbacks(manifest, project_dir=project_dir, client=active_client)
    charts_dir = project_dir / "05_charts"
    assets = render_chart_manifest(manifest, charts_dir)
    markdown = final_report_path.read_text(encoding="utf-8")
    html_path = build_report_html(
        topic=topic,
        project_id=project_dir.name,
        project_dir=project_dir,
        markdown=markdown,
        manifest=manifest,
        assets=assets,
    )
    tex_path = build_report_latex(
        topic=topic,
        project_dir=project_dir,
        markdown=markdown,
        manifest=manifest,
        assets=assets,
    )
    pdf_path = compile_report_pdf(tex_path)
    qa = inspect_pdf(pdf_path, topic=topic) if pdf_path else None
    return {
        "manifest_path": manifest_path,
        "charts_dir": charts_dir,
        "html_path": html_path,
        "tex_path": tex_path,
        "pdf_path": pdf_path,
        "engine": find_latex_engine(),
        "pandoc": find_pandoc(),
        "qa": qa,
    }
