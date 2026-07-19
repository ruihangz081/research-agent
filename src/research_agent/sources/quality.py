"""Deterministic research quality gate; no model output can override the result."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .enums import SourceStatus, VerificationStatus
from .models import EvidenceRecord
from .repository import SQLiteRepository


class QualityStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_LIMITATIONS = "passed_with_limitations"
    NEEDS_MORE_RESEARCH = "needs_more_research"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ResearchRequirement:
    question_id: str
    required: bool = True
    min_supported: int = 1
    require_numeric: bool = False
    min_source_tier: str | None = None


@dataclass
class QualityGateResult:
    status: QualityStatus
    reasons: list[str] = field(default_factory=list)
    coverage: dict[str, float] = field(default_factory=dict)
    checked_evidence_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status in {QualityStatus.PASSED, QualityStatus.PASSED_WITH_LIMITATIONS}


_TIER_RANK = {"S": 4, "A": 3, "B": 2, "D": 1, "unclassified": 0}


class QualityGate:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def evaluate(self, project_id: str, requirements: list[ResearchRequirement]) -> QualityGateResult:
        sources = {source.source_id: source for source in self.repository.list_sources(project_id, include_superseded=True)}
        evidence = self.repository.list_evidence(project_id)
        reasons: list[str] = []
        valid: list[EvidenceRecord] = []
        invalid_count = 0
        for item in evidence:
            source = sources.get(item.source_id)
            chunk = self.repository.get_chunk(item.chunk_id, project_id)
            if not source or not chunk or source.version != item.source_version or item.excerpt not in chunk.text:
                invalid_count += 1
                continue
            if item.verification_status == VerificationStatus.SUPPORTED:
                valid.append(item)
        if invalid_count:
            reasons.append(f"{invalid_count} evidence records have invalid source/version/chunk/excerpt")
        has_contradiction = any(item.verification_status == VerificationStatus.CONTRADICTED for item in evidence)
        if has_contradiction:
            reasons.append("contradicted evidence remains unresolved")
        if any(source.status == SourceStatus.NEEDS_REVIEW for source in sources.values()):
            reasons.append("high-severity extraction warning requires human review")
        coverage: dict[str, float] = {}
        missing_required = False
        for requirement in requirements:
            candidates = [item for item in valid if item.research_question_id == requirement.question_id]
            if requirement.min_source_tier:
                candidates = [item for item in candidates if _TIER_RANK.get(item.source_tier, 0) >= _TIER_RANK.get(requirement.min_source_tier, 0)]
            if requirement.require_numeric:
                candidates = [item for item in candidates if item.normalized_value is not None]
            ratio = min(1.0, len(candidates) / max(requirement.min_supported, 1))
            coverage[requirement.question_id] = ratio
            if requirement.required and ratio < 1:
                missing_required = True
                reasons.append(f"research question {requirement.question_id} coverage {ratio:.0%} below threshold")
        for item in valid:
            if item.source_tier == "S":
                continue
            independent = {other.source_id for other in valid if other.research_question_id == item.research_question_id and other.source_id != item.source_id}
            if not independent and item.source_tier not in {"S", "A"}:
                reasons.append(f"evidence {item.evidence_id} lacks direct S or independent corroboration")
        if not evidence:
            reasons.append("no evidence records are available")
        if invalid_count or has_contradiction or not evidence:
            status = QualityStatus.BLOCKED
        elif any(source.status == SourceStatus.NEEDS_REVIEW for source in sources.values()):
            status = QualityStatus.NEEDS_HUMAN_REVIEW
        elif missing_required:
            status = QualityStatus.NEEDS_MORE_RESEARCH
        elif reasons:
            status = QualityStatus.PASSED_WITH_LIMITATIONS
        else:
            status = QualityStatus.PASSED
        return QualityGateResult(status=status, reasons=sorted(set(reasons)), coverage=coverage,
                                 checked_evidence_ids=[item.evidence_id for item in valid])
