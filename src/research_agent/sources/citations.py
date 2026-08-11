"""Deterministic citation rendering from EvidenceRecord only."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .enums import SourceStatus, VerificationStatus
from .models import EvidenceRecord, SourceAsset
from .repository import SQLiteRepository


_STANDARD_CITATION_RE = re.compile(
    r"\[src:(?P<source_id>[^\]:,\s]+):v(?P<version>\d+),\s*(?P<locator>[^\]]+)\]"
)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\])]+")


@dataclass(frozen=True)
class StandardCitation:
    source_id: str
    source_version: int
    locator: str


def render_citation(evidence: EvidenceRecord, source: SourceAsset) -> str:
    locator = evidence.locator
    details = []
    if locator.page_number:
        details.append(f"p.{locator.page_number}")
    if locator.sheet_name:
        details.append(f"sheet={locator.sheet_name}")
    if locator.cell_range:
        details.append(f"range={locator.cell_range}")
    if locator.row and locator.column:
        details.append(f"cell=R{locator.row}C{locator.column}")
    if locator.slide_number:
        details.append(f"slide={locator.slide_number}")
    suffix = ", ".join(details) or "locator"
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
