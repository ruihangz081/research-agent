"""R4：Agent4 结构化结论台账的加载与确定性门禁。

`04_claims.json` 是分析报告结论的机器可读表达。它让系统能回答一个此前无法
确定性回答的问题：报告里的重要结论是否都有 SUPPORTED 证据支撑。此前只有
"引用了的必然真实"（citations.py 的方向性校验），这里补上反向的
"重要结论必然被引用"。
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .. import config
from ..research_plan import known_question_ids
from .enums import VerificationStatus
from .models import ClaimsFile
from .repository import SQLiteRepository


class ClaimsError(RuntimeError):
    """`04_claims.json` 缺失、损坏或未通过确定性门禁。"""


def load_claims_file(path: Path) -> ClaimsFile:
    """读取并校验 `04_claims.json`；任何问题都抛 `ClaimsError`。"""
    if not path.is_file():
        raise ClaimsError(f"缺少结论台账 {path.name}，Agent4 未产出或产出不完整")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimsError(f"结论台账 {path.name} 无法解析：{exc}") from exc
    if not isinstance(raw, dict):
        raise ClaimsError(f"结论台账 {path.name} 必须是 JSON 对象")
    try:
        return ClaimsFile.model_validate(raw)
    except ValidationError as exc:
        raise ClaimsError(
            f"结论台账 {path.name} 校验失败：{_format_validation_error(exc)}"
        ) from exc


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "claims"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def validate_claims(
    claims: ClaimsFile,
    project_dir: Path,
    repository: SQLiteRepository,
) -> list[str]:
    """对结论台账做确定性门禁，返回错误列表（空列表表示通过）。

    校验内容：
    1. 每条 claim 的 `question_id` 必须在固定需求清单内；
    2. `supporting_evidence_ids` 引用的证据必须存在、状态为 SUPPORTED，
       且其 `research_question_id` 与 claim 的 `question_id` 一致；
    3. 每个必答 `question_id` 至少有一条 claim（结论覆盖研究问题全集）。
    """
    errors: list[str] = []

    known = known_question_ids(project_dir)
    if known is None:
        errors.append("缺少研究需求清单，无法校验结论的问题归属")
        return errors
    known_set = set(known)

    # 读取全部证据，按 evidence_id 建索引
    evidence_by_id = {
        item.evidence_id: item
        for item in repository.list_evidence(project_dir.name)
    }

    covered_questions: set[str] = set()
    for claim in claims.claims:
        covered_questions.add(claim.question_id)
        if claim.question_id not in known_set:
            errors.append(
                f"claim {claim.claim_id} 引用了清单外的 question_id：{claim.question_id}"
            )
            continue
        for evidence_id in claim.supporting_evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                errors.append(
                    f"claim {claim.claim_id} 引用了不存在的证据：{evidence_id}"
                )
                continue
            if evidence.verification_status != VerificationStatus.SUPPORTED:
                errors.append(
                    f"claim {claim.claim_id} 引用了非 SUPPORTED 证据："
                    f"{evidence_id}（{evidence.verification_status.value}）"
                )
                continue
            if evidence.research_question_id != claim.question_id:
                errors.append(
                    f"claim {claim.claim_id} 的 question_id={claim.question_id} "
                    f"与其支持证据 {evidence_id} 的 "
                    f"research_question_id={evidence.research_question_id} 不一致"
                )

    # 必答问题必须全部被 claim 覆盖
    missing_required = known_set - covered_questions
    if missing_required:
        errors.append(
            "以下研究问题缺少结论（每条必答问题至少需要一条 claim）："
            + ", ".join(sorted(missing_required))
        )

    return errors
