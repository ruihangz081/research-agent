"""Deterministic citation rendering from EvidenceRecord only."""
from __future__ import annotations

import re

from .models import EvidenceRecord, SourceAsset


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
