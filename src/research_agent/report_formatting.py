"""Pandoc-based HTML/LaTeX/PDF delivery for brokerage-style reports."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config
from .agent_skills import load_project_skill
from .llm import LLMClient
from .pipeline_errors import DETERMINISTIC_CONTENT_HINT, DeterministicContentError
from .report_charts import (
    ChartAsset,
    ChartManifest,
    ChartSpec,
    load_chart_manifest,
    prepare_llm_fallbacks,
    render_chart_manifest,
    SUPPORTED_CHART_TYPES,
)

logger = logging.getLogger(__name__)

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

#: Agent4 在正文里用方括号标注每句结论的推导类别与置信度，例如
#: `[判断｜置信度: 高]`、`[计算]`、`[推导｜依据如下]`。这些是分析过程的内部标注，
#: 供证据门禁与人工复核使用，不是给读者看的内容。上一版它们原样进入 PDF——
#: 实测一份报告里有 42 处置信度标注和 8 处推导标注混在正文中。
#: 置信度信息本身有价值，但应该以可读形式呈现，而不是内部标签。
_ANALYSIS_ANNOTATION = re.compile(
    r"[ \t]*\[(判断|计算|推导|已验证事实|事实|假设|证据不足)"
    r"(?:[｜|][^\[\]\r\n]*)?\][ \t]*"
)
_LOW_CONFIDENCE_HINT = re.compile(r"低置信度")

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
    "⑥": "(6)",
    "⑦": "(7)",
    "⑧": "(8)",
    "⑨": "(9)",
    "⑩": "(10)",
    "⁰": "^0",
    "¹": "^1",
    "²": "^2",
    "³": "^3",
    "⁴": "^4",
    "⁵": "^5",
    "⁶": "^6",
    "⁷": "^7",
    "⁸": "^8",
    "⁹": "^9",
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


#: 融合式引用：标注与引用被 Agent4 合并写进同一括号。拆分为两个独立 token。
#: 第二段同时覆盖旧格式 ``[事实｜src:src_...]`` 与 P2 新格式 ``[事实｜ev=ev_...]``。
_MERGED_CITATION_RE = re.compile(
    r"\[(事实|已验证事实|推导|计算|判断|假设|证据不足)[｜|]((?:(?:src:)?src_|ev=ev_)[A-Za-z0-9_-]+[^\]\r\n]*?)\]"
)
#: 归一化后仍残留的融合式引用（方括号内出现 ｜ev= 或 ｜src:）。命中即说明有标注
#: 种类不在白名单内或引用写法异常，必须 fail-closed，不能静默丢引用。刻意限定在
#: 方括号内，避免误伤表格/代码里的普通竖线。
_LEFTOVER_MERGED_RE = re.compile(r"\[[^\]\r\n]*[｜|](?:ev=|src:)")


def count_merged_citations(markdown: str) -> int:
    """统计融合式 ``[事实｜src:...]`` 引用的数量（用于记录归一化是否发生）。"""
    return len(_MERGED_CITATION_RE.findall(markdown))


def normalize_merged_citations(markdown: str) -> str:
    """拆分 Agent4 把标注与引用合并写进同一括号的 ``[事实｜src:...]`` 形式。

    prompt 要求的标准格式是 `[事实] [src:...]`（分开两个括号），但 Agent4 偶发会把
    两者合并成 `[事实｜src:...]`。合并后的引用因为不再以 ``[src:`` 开头，无法被
    ``_REPORT_CITATION`` 识别，导致整段内部 ID 原样泄入 PDF。这里把合并括号拆回
    两个独立 token：标注交给 ``strip_analysis_annotations`` 移除，引用交给
    ``render_report_citations_*`` 压缩。
    """
    return _MERGED_CITATION_RE.sub(
        lambda m: f"[{m.group(1)}] [{m.group(2).strip(' ,')}]",
        markdown,
    )


def canonicalize_report_text(
    text: str,
    *,
    repository: Any | None = None,
    project_id: str | None = None,
) -> str:
    """把报告文本归一化到机器可审计的标准形式（单一入口）。

    依次执行：
    1. ``normalize_merged_citations`` —— 把 ``[事实｜src:...]`` 拆回 ``[事实] [src:...]``；
    2. ``expand_evidence_citations`` —— 把 ``[ev=ev_xxx]`` 用 ``render_citation()``
       展开为完整标准引用（传入 repository/project_id 时）。

    三处引用审计（orchestrator、Agent5、claims 归一化）与两个 ``build_*`` 都统一
    走这里，保证审计看到的引用与交付渲染看到的引用一致。纯文本路径（不带
    repository）只做第一步，供 Web 预览等无需反查证据库的场景使用。
    """
    normalized = normalize_merged_citations(text)
    if repository is not None and project_id is not None:
        from .sources.citations import expand_evidence_citations

        normalized = expand_evidence_citations(normalized, repository, project_id)
    if _LEFTOVER_MERGED_RE.search(normalized):
        # 归一化后仍有 [..｜ev=..] 或 [..｜src:..] 残留：标注种类不在拆分白名单内，
        # 或引用写法异常。若静默放行，这些引用会被 strip_analysis_annotations 整段
        # 吞掉——fail-closed 阻断，绝不静默丢引用。
        raise DeterministicContentError(
            "引用归一化后仍残留融合式引用（｜ev= 或 ｜src:）："
            f"{_LEFTOVER_MERGED_RE.search(normalized).group(0)!r}"
            f"（{DETERMINISTIC_CONTENT_HINT}）"
        )
    return normalized


def strip_analysis_annotations(markdown: str) -> str:
    """把 Agent4 的内部推导标注从交付文本中移除。

    Agent4 需要在正文标注每条结论的推导类别与置信度，确定性门禁与人工复核都依赖
    它们；但读者看到的应该是结论本身，不是 `[判断｜置信度: 高]` 这样的内部标签。
    Markdown 源文件保持原样（引用审计与结论门禁都基于它），只在渲染阶段剥离。

    低置信度是读者需要知道的信息，因此不静默丢弃：标注里带"低置信度"时替换为
    可读的中文提示，其余标注直接删除。
    """

    def replacement(match: re.Match[str]) -> str:
        if _LOW_CONFIDENCE_HINT.search(match.group(0)):
            return "（低置信度）"
        return ""

    return _ANALYSIS_ANNOTATION.sub(replacement, markdown)


def citation_source_order(markdown: str) -> list[str]:
    """按渲染时的编号顺序返回引用到的 source_id。

    刻意复刻两个渲染函数的两阶段遍历顺序（先 inline code 内，再正文），保证图例
    编号与正文上标编号严格一致；否则读者会顺着 `[3]` 查到错误的来源。
    """
    order: dict[str, int] = {}

    def record(match: re.Match[str]) -> str:
        order.setdefault(match.group(1), len(order) + 1)
        return match.group(0)

    def inline_code(match: re.Match[str]) -> str:
        _REPORT_CITATION.sub(record, match.group(1))
        return match.group(0)

    _INLINE_CODE.sub(inline_code, markdown)
    _REPORT_CITATION.sub(record, markdown)
    return [source_id for source_id, _ in sorted(order.items(), key=lambda kv: kv[1])]


def _source_legend_rows(
    project_id: str, ordered_source_ids: list[str]
) -> list[tuple[int, str, str, str]]:
    """按引用编号顺序取出来源元数据，供渲染阶段生成图例。

    只读 catalog 元数据（标题、发布者、抓取日期、URL），不碰 EvidenceRecord 正文。
    上一版在 Markdown 里生成的逐条证据附录有 59 条、26,608 字符（占报告 35%）；
    读者需要的是"[3] 指哪份材料"，而非每条证据的原文摘录。
    """
    try:
        from .sources.runtime import get_service

        service = get_service(config.SOURCE_DATA_DIR)
        sources = {
            source.source_id: source
            for source in service.list_sources(project_id, include_superseded=True)
        }
    except Exception:
        # 图例是可读性增强，catalog 不可用时不能阻断交付。
        return []

    rows: list[tuple[int, str, str, str]] = []
    for number, source_id in enumerate(ordered_source_ids, 1):
        source = sources.get(source_id)
        if source is None:
            rows.append((number, source_id, "", ""))
            continue
        title = (source.title or source.original_filename or source_id).strip()
        publisher = (source.publisher or "").strip()
        retrieved = ""
        if source.retrieved_at is not None:
            retrieved = str(source.retrieved_at)[:10]
        rows.append((number, title, publisher, retrieved))
    return rows


def build_source_legend_markdown(
    project_id: str, ordered_source_ids: list[str]
) -> str:
    """生成正文末尾的紧凑来源图例（Markdown 表格）。

    正文里的引用被压缩成 `[N]` 上标后，读者失去了"N 指哪份材料"的线索。图例把
    编号映射回可识别的来源，一源一行；完整 locator 与证据原文仍可在材料中心按
    source_id 查阅，不必印进报告。
    """
    rows = _source_legend_rows(project_id, ordered_source_ids)
    if not rows:
        return ""
    lines = [
        "",
        "---",
        "",
        "## 引用来源对照",
        "",
        "| 编号 | 来源 | 发布方 | 取得日期 |",
        "|---|---|---|---|",
    ]
    for number, title, publisher, retrieved in rows:
        safe_title = title.replace("|", r"\|")
        lines.append(f"| [{number}] | {safe_title} | {publisher or '—'} | {retrieved or '—'} |")
    lines.append("")
    return "\n".join(lines)


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


# ═════════════════════════════════════════════════════════════════
# 确定性字形覆盖检查（白名单，取代逐字枚举黑名单）
# ═════════════════════════════════════════════════════════════════

#: 判断式：码点 > 0x7F，且不属于 CJK 相关区段，且不在西文 lmroman10 的 cmap 里。
#: 这样的字符要么命中替换表被替换，要么抛错列出 U+XXXX 与字符名。
#:
#: 注意：刻意**不**纳入 3200-33FF（Enclosed CJK / CJK Compatibility）——它们装的是
#: ㈠㈡㊙㊗、㎡ ㎢ ㎏ ㏄ 这类非表意符号，FandolSong 并不覆盖（实测 ㎡/⑪ 都不在 cmap），
#: 归入"CJK 区段"会把它们静默漏成丢字。真正的表意区段是 3400-9FFF 与扩展平面。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),  # CJK Radicals Supplement
    (0x2F00, 0x2FDF),  # Kangxi Radicals
    (0x2FF0, 0x2FFF),  # Ideographic Description Characters
    (0x3000, 0x303F),  # CJK Symbols and Punctuation（、。「」《》等，Fandol 覆盖）
    (0x3040, 0x30FF),  # Hiragana / Katakana
    (0x3100, 0x312F),  # Bopomofo
    (0x31A0, 0x31BF),  # Bopomofo Extended
    (0x31C0, 0x31EF),  # CJK Strokes
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFE30, 0xFE4F),  # CJK Compatibility Forms（竖排括号等，Fandol 覆盖）
    (0xFF00, 0xFFEF),  # Halfwidth and Fullwidth Forms（，、：（）｜等）
    (0x20000, 0x2FA1F),  # CJK Extensions B+
)


def _is_cjk(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in _CJK_RANGES)


#: 西文字体默认 lmroman10-regular.otf；找不到时退回 ``fontTools`` 的定位接口。
_LATIN_FONT_FALLBACK = "lmroman10-regular.otf"


def _kpsewhich(font_name: str) -> str | None:
    """用 ``kpsewhich`` 取字体绝对路径；失败返回 None（不阻断渲染）。"""
    try:
        proc = subprocess.run(
            ["kpsewhich", font_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    path = proc.stdout.strip()
    return path if path and Path(path).is_file() else None


_cmap_cache: dict[str, frozenset[int]] = {}


def _load_cmap(font_path: str) -> frozenset[int]:
    """读取字体 cmap，按路径缓存（frozenset 便于命中测试）。"""
    cached = _cmap_cache.get(font_path)
    if cached is not None:
        return cached
    try:
        from fontTools.ttLib import TTFont
    except ImportError as exc:  # pragma: no cover - fontTools 是固定依赖
        raise RuntimeError("缺少 fontTools，无法做字形覆盖检查") from exc
    font = TTFont(font_path, lazy=True, fontNumber=0)
    codepoints: set[int] = set()
    try:
        for table in font["cmap"].tables:
            codepoints.update(table.cmap.keys())
    finally:
        font.close()
    result = frozenset(codepoints)
    _cmap_cache[font_path] = result
    return result


def _latin_cmap() -> frozenset[int] | None:
    """取西文字体 cmap；找不到字体时返回 None（表示字体不可用，检查应跳过）。

    无 TeX 环境的机器根本不会走 PDF 编译（``compile_report_pdf`` 在无引擎时直接
    ``return None``），此时若返回空集会把 ×−÷± 这类西文本来就覆盖的字符误判为
    越界、误阻断交付。因此"字体不可用"与"字体存在但无该码点"必须区分开。
    """
    path = _kpsewhich(_LATIN_FONT_FALLBACK)
    if path is None:
        return None
    return _load_cmap(path)


def check_pdf_glyph_coverage(markdown: str) -> None:
    """替换表之外的非 CJK、非西文 cmap 字符一律抛 ``DeterministicContentError``。

    与 ``normalize_markdown_for_pdf`` 组合使用：先跑替换表（把已知的 ①⑥⁴≈≥↔ 等
    换成 ASCII），再对剩余文本做确定性判定。命中替换表的字符已经被换掉，不会再
    出现在这里；漏网的非 CJK 特殊符号（如 ⑪、㎡）会触发报错，错误信息列出每个
    字符的 ``U+XXXX`` 与 ``unicodedata`` 名字，供修正上游产物而不是静默丢字。

    西文字体不可用（``_latin_cmap()`` 返回 None）时跳过检查并告警——该机器本就不
    会编译 PDF，用空 cmap 强行判定只会误阻断交付。
    """
    latin = _latin_cmap()
    if latin is None:
        logger.warning(
            "跳过字形覆盖检查：找不到西文字体 %s（无 TeX 环境，不会编译 PDF）",
            _LATIN_FONT_FALLBACK,
        )
        return
    offenders: dict[str, int] = {}
    for char in markdown:
        codepoint = ord(char)
        if codepoint <= 0x7F:
            continue
        if _is_cjk(codepoint):
            continue
        if codepoint in latin:
            continue
        offenders[char] = offenders.get(char, 0) + 1
    if not offenders:
        return
    details = ", ".join(
        f"U+{ord(char):04X} {unicodedata.name(char, '<未知字符>')} ×{count}"
        for char, count in sorted(offenders.items(), key=lambda item: -item[1])
    )
    raise DeterministicContentError(
        f"PDF 模板字体覆盖之外的字形：{details}"
        f"（{DETERMINISTIC_CONTENT_HINT}）"
    )


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
            # 无渲染资产（含数值溯源失败的降级图）：一律降级为数据表，不阻断交付。
            # 图表数值溯源防的是「编造数字」，但 SOTP/DCF 等自建测算图的数值是派生
            # 估算，逐字溯源本就不适用；为这类图作废整份交付得不偿失。
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
    prepared = canonicalize_report_text(prepared)
    prepared = render_report_citations_for_html(prepared, project_id)
    prepared = strip_analysis_annotations(prepared)
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
    # 颜色单一来源：brokerage-report.sty 现在 \input{brokerage-tokens.tex}，本步
    # 从 design-tokens.json 生成该文件并拷贝到项目目录，颜色命令名保持不变。
    from .design_tokens import latex_tokens_tex

    (project_dir / "brokerage-tokens.tex").write_text(
        latex_tokens_tex(), encoding="utf-8"
    )
    # 合并式引用（[事实｜src:...]）必须先拆回标准 [src:...] 形式，否则
    # citation_source_order 与 render_report_citations_for_latex 都无法识别，
    # 既漏进图例编号，又把整段内部 ID 泄入 PDF。
    markdown = canonicalize_report_text(markdown)
    # 图例必须在引用被压成 [N] 之前算好顺序，否则拿不到 source_id。
    # HTML 路径不需要：那里的上标是指向材料中心的可点击链接。
    legend = build_source_legend_markdown(
        project_dir.name, citation_source_order(markdown)
    )
    pdf_normalized = normalize_markdown_for_pdf(
        replace_chart_placeholders(
            # 图例放在免责声明之前：免责声明按惯例是报告最后一节。
            _ensure_disclaimer(markdown.rstrip() + legend),
            manifest,
            assets,
            target="latex",
        )
    )
    # 替换表之外的非 CJK、非西文 cmap 字符必须在此阻断，而不是让 XeLaTeX 丢字
    # 后再由 compile_report_pdf 阶段级失败——那会白烧整条渲染链。
    check_pdf_glyph_coverage(pdf_normalized)
    prepared = normalize_heading_numbers_for_pdf(pdf_normalized)
    prepared = render_report_citations_for_latex(prepared)
    prepared = strip_analysis_annotations(prepared)
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
    # 免责声明文字在 PDF 提取时可能被换行拆散（如「不构成任何投\n资建议」），
    # 子串匹配会误报缺失。这里与标题检查一致，先去掉全部空白再比对。
    if "不构成任何投资建议" not in re.sub(r"\s+", "", text):
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
    # P1：图表数值证据溯源门禁。有 04_claims.json 的项目走严格路径（必需图失败
    # 阻断、可选图降级）；无 claims 的历史项目走 v1 兼容路径（不做 claim 反查，
    # 仅回填 publisher、不阻断）。
    from .chart_provenance import apply_provenance_gate, validate_chart_provenance, write_provenance_report
    from .sources.runtime import get_service as _get_service

    repository = _get_service(config.SOURCE_DATA_DIR).repository
    claims_path = project_dir / config.FILE_CLAIMS
    strict = manifest.version == 2
    provenance_report = validate_chart_provenance(
        manifest,
        project_dir=project_dir,
        repository=repository,
        analysis_path=project_dir / config.FILE_ANALYSIS,
        claims_path=claims_path if strict else None,
    )
    write_provenance_report(provenance_report, project_dir / "05_chart_provenance.json")
    fallback_ids = apply_provenance_gate(
        provenance_report, manifest, strict=strict
    )
    # 可选图降级：移除其渲染资产，让 replace_chart_placeholders 走 _fallback_table。
    for chart_id in fallback_ids:
        assets.pop(chart_id, None)
    markdown = final_report_path.read_text(encoding="utf-8")
    # 交付渲染前统一归一化：拆分 [事实｜src:...]，并把 Agent4 只写的 [ev=ev_xxx]
    # 展开为标准引用。审计（orchestrator / Agent5）与渲染必须看到同一份文本，否则
    # 新格式下 ev=ev_ / chunk=chk 会原样泄入 PDF 与 HTML。
    #
    # 归一化失败（残留融合式引用）是正文引用真实性问题，仍须 fail-closed，不降级。
    markdown = canonicalize_report_text(
        markdown,
        repository=repository,
        project_id=project_dir.name,
    )

    # 多格式独立交付：Markdown 恒为首要产物，HTML 与 PDF 各自独立降级。
    # 任一格式失败都不阻断其它格式，只有 Markdown（final_report_path 已在 formatter
    # 写好）缺失才算交付失败。降级原因统一收集，供状态与前端展示。
    degradation: list[str] = []
    html_path = None
    try:
        html_path = build_report_html(
            topic=topic,
            project_id=project_dir.name,
            project_dir=project_dir,
            markdown=markdown,
            manifest=manifest,
            assets=assets,
        )
    except Exception as exc:
        degradation.append(f"HTML 生成失败，已交付 Markdown：{exc}")

    tex_path = None
    pdf_path = None
    qa = None
    if html_path is not None or config.REPORT_PDF_ENGINE != "chrome":
        # latex 引擎独立于 HTML；chrome 引擎则依赖 HTML，HTML 失败时 PDF 也降级。
        try:
            if config.REPORT_PDF_ENGINE == "chrome":
                # Chrome 打印管线：HTML 与图表仍由上面的 report_formatting 链路产出，
                # 这里只替换「HTML → PDF」这一段。不生成 .tex，tex_path 置 None。
                # generate_print_pdf 用 Playwright 同步 API，必须脱离 asyncio 事件循环
                # 在独立线程跑，否则 sync_playwright 会拒绝在事件循环内启动。
                from .report_print import generate_print_pdf

                print_result = await asyncio.to_thread(
                    generate_print_pdf, project_dir=project_dir, html_path=html_path
                )
                pdf_path = print_result.pdf_path
                tex_path = None
                # Chrome 封面标题取自 HTML 的 h1（报告自身标题），不是 topic；QA 的
                # 标题检查要用同一口径，否则「topic 调研报告」与 h1 对不上会误判 PDF
                # 缺标题。
                html_text = html_path.read_text(encoding="utf-8")
                title_match = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S)
                qa_topic = (
                    re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
                    if title_match
                    else topic
                )
            else:
                tex_path = build_report_latex(
                    topic=topic,
                    project_dir=project_dir,
                    markdown=markdown,
                    manifest=manifest,
                    assets=assets,
                )
                pdf_path = compile_report_pdf(tex_path)
                qa_topic = topic
            if pdf_path:
                try:
                    qa = inspect_pdf(pdf_path, topic=qa_topic)
                except Exception as exc:
                    degradation.append(f"PDF QA 检查失败，已交付 PDF：{exc}")
        except Exception as exc:
            degradation.append(f"PDF 生成失败，已交付 Markdown/HTML：{exc}")
    else:
        degradation.append("HTML 生成失败，chrome 引擎无法生成 PDF")

    return {
        "manifest_path": manifest_path,
        "charts_dir": charts_dir,
        "html_path": html_path,
        "tex_path": tex_path,
        "pdf_path": pdf_path,
        "engine": find_latex_engine(),
        "engine_used": config.REPORT_PDF_ENGINE,
        "pandoc": find_pandoc(),
        "qa": qa,
        "degradation": degradation,
    }
