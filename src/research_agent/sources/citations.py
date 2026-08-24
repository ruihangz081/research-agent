"""Deterministic citation rendering from EvidenceRecord only."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..pipeline_errors import DETERMINISTIC_CONTENT_HINT, DeterministicContentError
from .enums import SourceStatus, VerificationStatus
from .models import EvidenceRecord, SourceAsset
from .repository import SQLiteRepository


_STANDARD_CITATION_RE = re.compile(
    r"\[src:(?P<source_id>[^\]:,\s]+):v(?P<version>\d+),\s*(?P<locator>[^\]]+)\]"
)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\])]+")
#: 独立方括号的短引用 `[ev=ev_xxx]`。evidence_id 形如 ``ev_`` + 32 位十六进制；
#: Agent4 偶发截断末尾（如 ``ev_...5469cc53`` 抄成 ``ev_...5469cc``），因此这里
#: 匹配 ``ev_`` 后任意字母/数字/下划线（含截断与非十六进制笔误），由下方
#: ``expand_evidence_citations`` 精确反查——未知/截断一律报错而非静默解析或跳过。
_SHORT_EVIDENCE_CITATION_RE = re.compile(r"\[ev=(ev_[A-Za-z0-9_]+)\]")


@dataclass(frozen=True)
class StandardCitation:
    source_id: str
    source_version: int
    locator: str


def render_citation(evidence: EvidenceRecord, source: SourceAsset) -> str:
    locator = evidence.locator
    details = [f"ev={evidence.evidence_id}", f"chunk={evidence.chunk_id}"]
    if locator.page_number:
        details.append(f"p.{locator.page_number}")
    if locator.paragraph_index is not None:
        details.append(f"paragraph={locator.paragraph_index}")
    if locator.table_id:
        details.append(f"table={locator.table_id}")
    if locator.sheet_name:
        details.append(f"sheet={locator.sheet_name}")
    if locator.cell_range:
        details.append(f"range={locator.cell_range}")
    if locator.row and locator.column:
        details.append(f"cell=R{locator.row}C{locator.column}")
    if locator.slide_number:
        details.append(f"slide={locator.slide_number}")
    if locator.char_start is not None:
        char_end = "" if locator.char_end is None else locator.char_end
        details.append(f"char={locator.char_start}-{char_end}")
    if locator.zip_member:
        details.append(f"member={locator.zip_member}")
    suffix = ", ".join(details)
    return f"[src:{source.source_id}:v{evidence.source_version}, {suffix}]"


def validate_report_citations(citations: list[EvidenceRecord], source_lookup: dict[str, SourceAsset]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for evidence in citations:
        source = source_lookup.get(evidence.source_id)
        if not source:
            errors.append(f"missing source: {evidence.evidence_id}")
        elif source.version != evidence.source_version:
            errors.append(f"stale source version: {evidence.evidence_id}")
        if not evidence.excerpt.strip():
            errors.append(f"empty excerpt: {evidence.evidence_id}")
    return not errors, errors


def extract_standard_citations(text: str) -> list[StandardCitation]:
    """Extract standard citations without interpreting surrounding claims."""
    return [
        StandardCitation(
            source_id=match.group("source_id"),
            source_version=int(match.group("version")),
            locator=match.group("locator").strip(),
        )
        for match in _STANDARD_CITATION_RE.finditer(text)
    ]


def _resolve_evidence_id(
    raw_evidence_id: str, evidence_lookup: dict[str, EvidenceRecord]
) -> EvidenceRecord:
    """把 ``[ev=...]`` 里的 id 精确解析到 EvidenceRecord，非精确匹配一律报错。

    Agent4 只写 `[ev=ev_xxx]`，`ev_xxx` 必须是证据目录里逐字复制的完整
    ``evidence_id``（``ev_`` + 32 位十六进制）。截断、改写或未知 id 都是确定性
    内容错误——绝不静默前缀解析成某条证据，也绝不静默跳过。之所以不加"唯一前缀
    自愈"，是因为 P2 的整个目标就是消灭手抄漂移：再对截断 id 做前缀猜测，等于把
    漂移从 `[src:...]` 四段搬到了 `ev_` 一段，仍可能解析到错误证据。
    """
    evidence = evidence_lookup.get(raw_evidence_id)
    if evidence is not None:
        return evidence
    raise DeterministicContentError(
        f"未知 evidence_id：{raw_evidence_id}（{DETERMINISTIC_CONTENT_HINT}）"
    )


def expand_evidence_citations(
    text: str, repository: SQLiteRepository, project_id: str
) -> str:
    """把 Agent4 只写的 ``[ev=ev_xxx]`` 用 ``render_citation()`` 展开为标准引用。

    Analyst 只需在结论后写 ``[事实] [ev=ev_xxx]`` 两个独立方括号，引用文本由
    程序从 EvidenceRecord 确定性展开——不再要求模型手抄 source_id / 版本 / chunk /
    locator 四段，从根上消除手抄漂移。未知/截断 ev id 抛 ``DeterministicContentError``。
    """
    matches = list(_SHORT_EVIDENCE_CITATION_RE.finditer(text))
    if not matches:
        return text

    evidence_lookup = {
        item.evidence_id: item for item in repository.list_evidence(project_id)
    }
    source_lookup = {
        item.source_id: item
        for item in repository.list_sources(project_id, include_superseded=True)
    }

    def replacement(match: re.Match[str]) -> str:
        raw_id = match.group(1)
        evidence = _resolve_evidence_id(raw_id, evidence_lookup)
        source = source_lookup.get(evidence.source_id)
        if source is None:
            raise DeterministicContentError(
                f"证据 {evidence.evidence_id} 的来源 {evidence.source_id} 不存在"
                f"（{DETERMINISTIC_CONTENT_HINT}）"
            )
        return render_citation(evidence, source)

    return _SHORT_EVIDENCE_CITATION_RE.sub(replacement, text)


def audit_analysis_citations(
    text: str,
    project_id: str,
    repository: SQLiteRepository,
) -> list[str]:
    """Validate every written standard citation against supported evidence."""
    errors: list[str] = []
    citations = extract_standard_citations(text)
    bare_urls = sorted(set(_BARE_URL_RE.findall(text)))
    if bare_urls:
        errors.append(
            "bare URL is not an EvidenceRecord citation: " + ", ".join(bare_urls)
        )
    if "[src:" in _STANDARD_CITATION_RE.sub("", text):
        errors.append(
            "malformed analysis citation; expected [src:source_id:vN, locator]"
        )
    if not citations:
        errors.append(
            "completed analysis has no standard EvidenceRecord citation"
        )

    evidence = repository.list_evidence(project_id)
    supported_citations: set[StandardCitation] = set()
    for item in evidence:
        if item.verification_status != VerificationStatus.SUPPORTED:
            continue
        source = repository.get_source(item.source_id, project_id)
        if source is None:
            continue
        supported_citations.update(
            extract_standard_citations(render_citation(item, source))
        )

    for citation in citations:
        source = repository.get_source(citation.source_id, project_id)
        reference = f"{citation.source_id}:v{citation.source_version}"
        if source is None:
            errors.append(f"unknown source_id in analysis citation: {reference}")
            continue
        latest_version = repository.latest_version(
            project_id,
            source.logical_source_id,
        )
        if (
            source.version != citation.source_version
            or source.status == SourceStatus.SUPERSEDED
            or citation.source_version != latest_version
        ):
            errors.append(
                "stale source_version in analysis citation: "
                f"{reference} (latest v{latest_version})"
            )
            continue
        if citation not in supported_citations:
            errors.append(
                "analysis citation has no SUPPORTED EvidenceRecord matching locator: "
                f"{reference}, {citation.locator}"
            )
    return errors


_REPORT_CITATION = re.compile(r"\[src:[^\]\r\n]+\]", re.IGNORECASE)


def validate_report_text_citations(
    report: str,
    evidence: list[EvidenceRecord],
    source_lookup: dict[str, SourceAsset],
) -> tuple[bool, list[str]]:
    """Reject report citations that are not exact supported EvidenceRecords."""
    valid, errors = validate_report_citations(evidence, source_lookup)
    if not valid:
        return False, errors

    allowed = {
        render_citation(item, source_lookup[item.source_id])
        for item in evidence
        if item.source_id in source_lookup
    }
    found = _REPORT_CITATION.findall(report)
    if not found:
        errors.append("report contains no evidence citations")
    for marker in sorted(set(found)):
        if marker not in allowed:
            errors.append(f"citation is not an exact supported EvidenceRecord: {marker}")
    return not errors, errors
