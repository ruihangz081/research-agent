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
# 门禁①附加：台账必须对应分析报告正文
# ═══════════════════════════════════════════════════════════════


def _two_question_claims(extra: dict | None = None) -> ClaimsFile:
    claims = [
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
    if extra:
        claims.append(extra)
    return ClaimsFile(claims=claims)


def test_claim_absent_from_analysis_body_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """台账不得包含分析报告正文里不存在的结论。

    Agent5 不再撰写正文后，这类"台账独有"的结论既不会出现在交付物里，也不会被
    任何其他门禁发现——等于凭空多出一批无法追溯到报告的结论。
    """
    state = _state(tmp_path, monkeypatch)
    _record_evidence(state, evidence_id="ev-1", question_id="q1", status=VerificationStatus.SUPPORTED)
    analysis = "# 分析\n\n营收 4200 万。\n\n增长由价格驱动。\n"

    errors = validate_claims(
        _two_question_claims(
            {
                "claim_id": "c3",
                "question_id": "q2",
                "kind": "judgment",
                "importance": "major",
                "text": "公司将在三年内成为全球第一",
            }
        ),
        state.project_dir,
        _repository(),
        analysis_text=analysis,
    )

    assert any("c3" in error and "找不到对应句子" in error for error in errors)
    assert not any("c1" in error for error in errors)


def test_claim_matching_analysis_ignores_citations_and_emphasis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正文里的引用标记、强调符号和换行不算差异。

    正文会在同一句里插入 `[src:...]` 引用、`[判断｜置信度: 中]` 标注和 `**` 强调，
    要求模型在台账里逐字复现这些排版符号不现实（实测 19 条只有 13 条能原样匹配）。
    门禁校验的是对应关系，不是字节相同。
    """
    state = _state(tmp_path, monkeypatch)
    _record_evidence(state, evidence_id="ev-1", question_id="q1", status=VerificationStatus.SUPPORTED)
    analysis = (
        "# 分析\n\n"
        "**营收 4200 万**：同比高增。[判断｜置信度: 中] "
        "[src:src_a:v1, ev=ev-1, chunk=chk_a, p.1]\n\n"
        "增长由价格\n驱动。\n"
    )

    errors = validate_claims(
        _two_question_claims(),
        state.project_dir,
        _repository(),
        analysis_text=analysis,
    )

    assert errors == []


def test_claim_body_check_is_skipped_without_analysis_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 analysis_text 时跳过正文对应校验，保持既有调用方行为不变。"""
    state = _state(tmp_path, monkeypatch)
    _record_evidence(state, evidence_id="ev-1", question_id="q1", status=VerificationStatus.SUPPORTED)

    errors = validate_claims(
        _two_question_claims(
            {
                "claim_id": "c3",
                "question_id": "q2",
                "kind": "judgment",
                "importance": "major",
                "text": "正文里没有这句话",
            }
        ),
        state.project_dir,
        _repository(),
    )

    assert errors == []


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


def test_load_claims_file_normalizes_high_importance_to_major(
    tmp_path: Path,
) -> None:
    path = _claims_file(
        tmp_path / "04_claims.json",
        [
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "high",
                "text": "营收达 4200 万",
                "supporting_evidence_ids": ["ev-1"],
                "confidence": "high",
            }
        ],
    )

    claims = load_claims_file(path)

    assert claims.claims[0].importance == "major"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["claims"][0]["importance"] == "major"


def test_load_claims_file_normalizes_all_confidence_aliases(
    tmp_path: Path,
) -> None:
    """confidence 的三个取值写进 importance 时都要归一化，而非只修 `high`。

    实测失败里 `medium` / `low` 同样出现过；只修 `high` 会让同一类笔误继续
    整轮作废。归一化后一律落在不新增约束的档位，不会有 claim 被悄悄升级成
    critical 并因此触发终稿逐字保留门禁。
    """
    path = _claims_file(
        tmp_path / "04_claims.json",
        [
            {
                "claim_id": "c1",
                "question_id": "q1",
                "kind": "fact",
                "importance": "medium",
                "text": "营收达 4200 万",
                "supporting_evidence_ids": ["ev-1"],
                "confidence": "medium",
            },
            {
                "claim_id": "c2",
                "question_id": "q1",
                "kind": "judgment",
                "importance": "low",
                "text": "增速可能放缓",
                "supporting_evidence_ids": [],
                "confidence": "low",
            },
        ],
    )

    claims = load_claims_file(path)

    assert [item.importance for item in claims.claims] == ["major", "minor"]
    assert all(item.importance != "critical" for item in claims.claims)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert [item["importance"] for item in persisted["claims"]] == ["major", "minor"]


# ═══════════════════════════════════════════════════════════════
# 门禁③：终稿必须是 Agent4 正文逐字复制 + 图表占位符
# ═══════════════════════════════════════════════════════════════


def _manifest(*chart_ids: str):
    """构造只含 id 与 placement_after 的最小图表清单。"""
    from research_agent.report_charts import ChartManifest

    return ChartManifest.model_validate(
        {
            "charts": [
                {
                    "id": chart_id,
                    "type": "bar",
                    "title": f"图 {chart_id}",
                    "unit": "亿元",
                    "as_of_date": "2026-08-17",
                    "source": "公开资料整理",
                    "placement_after": "# 分析",
                    "labels": ["2025"],
                    "series": [
                        {
                            "name": "营收",
                            "values": [1],
                            "value_kind": ["actual"],
                        }
                    ],
                }
                for chart_id in chart_ids
            ]
        }
    )


def test_composed_report_matching_analysis_passes(tmp_path: Path) -> None:
    """终稿等于正文逐字加占位符时通过。"""
    analysis_path = tmp_path / config.FILE_ANALYSIS
    analysis_path.write_text("# 分析\n\n营收 4200 万。\n", encoding="utf-8")
    report_path = tmp_path / config.FILE_FINAL_REPORT
    report_path.write_text(
        "# 分析\n\n{{chart:c-1}}\n\n营收 4200 万。\n", encoding="utf-8"
    )

    formatter._audit_composed_report(analysis_path, report_path, _manifest("c-1"))


def test_composed_report_dropping_analysis_line_blocks(tmp_path: Path) -> None:
    """compose 丢行必须被发现。

    终稿由程序生成，一旦 compose 被改坏就会悄悄偏离已通过 Agent4 门禁的正文，
    而没有任何其他检查会察觉。
    """
    analysis_path = tmp_path / config.FILE_ANALYSIS
    analysis_path.write_text(
        "# 分析\n\n营收 4200 万。\n\n毛利率 38%。\n", encoding="utf-8"
    )
    report_path = tmp_path / config.FILE_FINAL_REPORT
    report_path.write_text("# 分析\n\n营收 4200 万。\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="与 Agent4 正文不一致"):
        formatter._audit_composed_report(analysis_path, report_path, _manifest())


def test_composed_report_rewriting_analysis_blocks(tmp_path: Path) -> None:
    """compose 改写正文措辞必须被发现。"""
    analysis_path = tmp_path / config.FILE_ANALYSIS
    analysis_path.write_text("# 分析\n\n营收 4200 万。\n", encoding="utf-8")
    report_path = tmp_path / config.FILE_FINAL_REPORT
    report_path.write_text("# 分析\n\n营收约 4200 万元。\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="与 Agent4 正文不一致"):
        formatter._audit_composed_report(analysis_path, report_path, _manifest())


def test_composed_report_missing_declared_placeholder_blocks(
    tmp_path: Path,
) -> None:
    """图表清单声明了占位符，终稿里却没有插入。"""
    analysis_path = tmp_path / config.FILE_ANALYSIS
    analysis_path.write_text("# 分析\n\n营收 4200 万。\n", encoding="utf-8")
    report_path = tmp_path / config.FILE_FINAL_REPORT
    report_path.write_text("# 分析\n\n营收 4200 万。\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="缺少图表清单声明的占位符"):
        formatter._audit_composed_report(analysis_path, report_path, _manifest("c-1"))


def test_composed_report_extra_text_appended_blocks(tmp_path: Path) -> None:
    """在正文之后追加内容（例如证据附录）必须被发现。

    上一版 Agent5 会追加自动生成的证据索引与结论补丁，占到报告 46%；新架构
    禁止任何追加，这条测试锁住该约束。
    """
    analysis_path = tmp_path / config.FILE_ANALYSIS
    analysis_path.write_text("# 分析\n\n营收 4200 万。\n", encoding="utf-8")
    report_path = tmp_path / config.FILE_FINAL_REPORT
    report_path.write_text(
        "# 分析\n\n营收 4200 万。\n\n## 可追溯证据索引\n\n- 证据一\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="与 Agent4 正文不一致"):
        formatter._audit_composed_report(analysis_path, report_path, _manifest())
