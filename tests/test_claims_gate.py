"""R4 回归测试：Agent4 结构化结论台账与证据覆盖门禁。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.agents import formatter
from research_agent.research_plan import derive_plan_from_outline, save_plan
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.claims import (
    ClaimsError,
    load_claims_file,
    validate_claims,
)
from research_agent.sources.enums import VerificationStatus
from research_agent.sources.models import ClaimsFile, EvidenceRecord
from research_agent.sources.runtime import reset_runtime
from research_agent.state import ProjectState


def _fix_plan(state: ProjectState, *question_ids: str) -> None:
    outline_text = (
        f"# {state.topic}\n\n## 二、核心研究问题\n"
        + "\n".join(
            f"{index}. 研究问题 {question_id}"
            for index, question_id in enumerate(question_ids, 1)
        )
        + "\n"
    )
    outline_path = state.project_dir / config.FILE_OUTLINE
    outline_path.parent.mkdir(parents=True, exist_ok=True)
    outline_path.write_text(outline_text, encoding="utf-8")
    state.outline_path = str(outline_path)
    plan, _ = derive_plan_from_outline(state.topic, outline_text)
    for requirement, question_id in zip(plan.requirements, question_ids, strict=True):
        requirement.question_id = question_id
    save_plan(state, plan)


def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectState:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    reset_runtime()
    state = ProjectState(topic="R4 claims", date_str="20260730")
    _fix_plan(state, "q1", "q2")
    state.save()
    return state


def _record_evidence(
    state: ProjectState,
    *,
    evidence_id: str,
    question_id: str,
    status: VerificationStatus,
    excerpt: str = "Revenue reached 42 million",
) -> None:
    repository = SQLiteRepository(config.SOURCE_DATA_DIR / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(config.SOURCE_DATA_DIR / "objects"))
    source = service.register_bytes(state.project_dir.name, "facts.txt", excerpt.encode()).source
    source.source_tier = "S"
    repository.update_source(source)
    service.parse_source(state.project_dir.name, source.source_id)
    chunk = service.index_source(state.project_dir.name, source.source_id)[0]
    service.activate(state.project_dir.name, source.source_id)
    service.record_evidence(
        EvidenceRecord(
            evidence_id=evidence_id,
            project_id=state.project_dir.name,
            research_question_id=question_id,
            claim=excerpt,
            source_id=source.source_id,
            source_version=source.version,
            chunk_id=chunk.chunk_id,
            locator=chunk.locators[0],
            excerpt=excerpt,
            source_tier="S",
            verification_status=status,
            confidence=1,
        )
    )
    repository.close()


def _repository() -> SQLiteRepository:
    return SQLiteRepository(config.SOURCE_DATA_DIR / "catalog.sqlite3")


def _claims_file(path: Path, claims: list[dict]) -> Path:
    path.write_text(
        json.dumps({"schema_version": "1.0", "claims": claims}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# ═══════════════════════════════════════════════════════════════
# 模型层
# ═══════════════════════════════════════════════════════════════


def test_critical_claim_requires_supporting_evidence() -> None:
    with pytest.raises(ValueError, match="critical 结论必须至少有一条支持证据"):
        ClaimsFile.model_validate(
            {
                "claims": [
                    {
                        "claim_id": "c1",
                        "question_id": "q1",
                        "kind": "fact",
                        "importance": "critical",
                        "text": "营收达 4200 万",
                        "supporting_evidence_ids": [],
                    }
                ]
            }
        )


def test_duplicate_claim_ids_rejected() -> None:
    with pytest.raises(ValueError, match="claim_id 重复"):
        ClaimsFile.model_validate(
            {
                "claims": [
                    {
                        "claim_id": "c1",
                        "question_id": "q1",
                        "kind": "fact",
                        "text": "a",
                    },
                    {
                        "claim_id": "c1",
                        "question_id": "q2",
                        "kind": "fact",
                        "text": "b",
                    },
                ]
            }
        )


def test_major_claim_without_evidence_is_allowed() -> None:
    claims = ClaimsFile.model_validate(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "question_id": "q1",
                    "kind": "judgment",
                    "importance": "major",
                    "text": "行业集中度正在提升",
                }
            ]
        }
    )
    assert claims.claims[0].importance == "major"


# ═══════════════════════════════════════════════════════════════
# 门禁①：validate_claims
# ═══════════════════════════════════════════════════════════════


def test_valid_claims_pass_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(tmp_path, monkeypatch)
    _record_evidence(state, evidence_id="ev-1", question_id="q1", status=VerificationStatus.SUPPORTED)
    _record_evidence(state, evidence_id="ev-2", question_id="q2", status=VerificationStatus.SUPPORTED)

    claims = ClaimsFile(
        claims=[
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "critical",
                "text": "营收 4200 万",
                "supporting_evidence_ids": ["ev-1"],
            },
            {
                "claim_id": "c2",
                "question_id": "q2",
                "kind": "judgment",
                "importance": "major",
                "text": "增长由价格驱动",
            },
        ]
    )

    assert validate_claims(claims, state.project_dir, _repository()) == []


def test_critical_claim_with_unknown_evidence_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    claims = ClaimsFile(
        claims=[
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "critical",
                "text": "营收 4200 万",
                "supporting_evidence_ids": ["ev-does-not-exist"],
            }
        ]
    )
    errors = validate_claims(claims, state.project_dir, _repository())
    assert any("不存在的证据" in error for error in errors)


def test_critical_claim_with_unverified_evidence_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    _record_evidence(state, evidence_id="ev-1", question_id="q1", status=VerificationStatus.UNVERIFIED)
    claims = ClaimsFile(
        claims=[
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "critical",
                "text": "营收 4200 万",
                "supporting_evidence_ids": ["ev-1"],
            }
        ]
    )
    errors = validate_claims(claims, state.project_dir, _repository())
    assert any("非 SUPPORTED" in error for error in errors)


def test_claim_question_id_must_match_evidence_question_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    _record_evidence(state, evidence_id="ev-1", question_id="q2", status=VerificationStatus.SUPPORTED)
    claims = ClaimsFile(
        claims=[
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "critical",
                "text": "营收 4200 万",
                "supporting_evidence_ids": ["ev-1"],
            }
        ]
    )
    errors = validate_claims(claims, state.project_dir, _repository())
    assert any("不一致" in error for error in errors)


def test_missing_required_question_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)  # 固定 q1, q2 两个必答问题
    _record_evidence(state, evidence_id="ev-1", question_id="q1", status=VerificationStatus.SUPPORTED)
    claims = ClaimsFile(
        claims=[
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "major",
                "text": "营收 4200 万",
            }
        ]
    )
    errors = validate_claims(claims, state.project_dir, _repository())
    assert any("缺少结论" in error and "q2" in error for error in errors)


def test_claim_with_unknown_question_id_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    claims = ClaimsFile(
        claims=[
            {
                "claim_id": "c1",
                "question_id": "unknown-q",
                "kind": "fact",
                "importance": "major",
                "text": "营收 4200 万",
            }
        ]
    )
    errors = validate_claims(claims, state.project_dir, _repository())
    assert any("清单外" in error for error in errors)


# ═══════════════════════════════════════════════════════════════
# load_claims_file
# ═══════════════════════════════════════════════════════════════


def test_load_claims_file_missing(tmp_path: Path) -> None:
    with pytest.raises(ClaimsError, match="缺少结论台账"):
        load_claims_file(tmp_path / "04_claims.json")


def test_load_claims_file_damaged(tmp_path: Path) -> None:
    path = tmp_path / "04_claims.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ClaimsError, match="无法解析"):
        load_claims_file(path)


# ═══════════════════════════════════════════════════════════════
# 门禁③：终稿保留 critical 结论
# ═══════════════════════════════════════════════════════════════


def test_final_report_missing_critical_claim_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    _claims_file(
        state.project_dir / config.FILE_CLAIMS,
        [
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "critical",
                "text": "2026 年 NAND 资本开支仅 +5% 至 $222 亿",
                "supporting_evidence_ids": ["ev-1"],
            }
        ],
    )
    report_path = state.project_dir / config.FILE_FINAL_REPORT
    report_path.write_text("这是一份没有包含关键结论的报告。", encoding="utf-8")

    with pytest.raises(RuntimeError, match="critical 结论"):
        formatter._audit_final_report_claims(state, report_path)


def test_final_report_preserving_critical_claim_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    _claims_file(
        state.project_dir / config.FILE_CLAIMS,
        [
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "critical",
                "text": "2026 年 NAND 资本开支仅 +5% 至 $222 亿",
                "supporting_evidence_ids": ["ev-1"],
            }
        ],
    )
    report_path = state.project_dir / config.FILE_FINAL_REPORT
    report_path.write_text(
        "结论：2026 年 NAND 资本开支仅 +5% 至 $222 亿。", encoding="utf-8"
    )

    formatter._audit_final_report_claims(state, report_path)


def test_final_report_missing_claims_file_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    report_path = state.project_dir / config.FILE_FINAL_REPORT
    report_path.write_text("报告正文", encoding="utf-8")

    with pytest.raises(RuntimeError, match="结论保留审计失败"):
        formatter._audit_final_report_claims(state, report_path)
