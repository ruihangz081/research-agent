"""Explicit migration helpers for legacy source files, reports, and citations."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import EvidenceRecord, SourceLocator
from .security import SourceSecurityError, ensure_within, safe_filename
from .service import SourceService
from .enums import LocatorType, VerificationStatus


LEGACY_CITATION = re.compile(r"\[src:\s*([^\],]+)(?:,\s*([^\]]+))?\]")


@dataclass
class MigrationResult:
    imported_sources: list[str] = field(default_factory=list)
    migrated_evidence: list[str] = field(default_factory=list)
    unresolved_citations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_legacy_citations(text: str) -> list[tuple[str, str | None]]:
    return [(match.group(1).strip(), match.group(2).strip() if match.group(2) else None) for match in LEGACY_CITATION.finditer(text)]


def migrate_legacy_report(
    service: SourceService,
    project_id: str,
    report_text: str,
    citation_map: dict[str, tuple[str, str, str]] | None = None,
) -> MigrationResult:
    """Map only citations with an exact source/chunk/excerpt tuple; preserve others as unresolved."""
    result = MigrationResult()
    citation_map = citation_map or {}
    for index, (legacy_id, legacy_locator) in enumerate(find_legacy_citations(report_text)):
        mapped = citation_map.get(legacy_id)
        if not mapped:
            result.unresolved_citations.append(legacy_id)
            continue
        source_id, chunk_id, excerpt = mapped
        try:
            source = service.get_source(project_id, source_id)
            chunk = service.repository.get_chunk(chunk_id, project_id)
            if not chunk or excerpt not in chunk.text:
                result.unresolved_citations.append(legacy_id)
                continue
            locator = chunk.locators[0] if chunk.locators else SourceLocator(locator_type=LocatorType.OFFSET)
            evidence = EvidenceRecord(evidence_id=f"migration_{index}_{legacy_id}", project_id=project_id,
                                      research_question_id=f"legacy:{legacy_id}", claim=f"Migrated legacy citation {legacy_id}",
                                      source_id=source_id, source_version=source.version, chunk_id=chunk_id, locator=locator,
                                      excerpt=excerpt, source_tier=source.source_tier, verification_status=VerificationStatus.UNVERIFIED)
            service.record_evidence(evidence, actor="migration")
            result.migrated_evidence.append(evidence.evidence_id)
        except (KeyError, ValueError):
            result.unresolved_citations.append(legacy_id)
    if result.unresolved_citations:
        result.warnings.append("unresolved legacy citations block automatic report cutover")
    return result


def import_legacy_raw_directory(service: SourceService, project_id: str, project_dir: str | Path) -> MigrationResult:
    root = Path(project_dir).expanduser().resolve()
    raw_dir = ensure_within(root, root / "03_raw_data")
    result = MigrationResult()
    if not raw_dir.exists():
        result.warnings.append("legacy raw data directory does not exist")
        return result
    for path in sorted(raw_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            name = safe_filename(path.name)
            registered = service.register_bytes(project_id, name, path.read_bytes(), actor="migration")
            result.imported_sources.append(registered.source.source_id)
        except (SourceSecurityError, ValueError) as exc:
            result.warnings.append(f"skipped {path.name}: {exc}")
    return result
