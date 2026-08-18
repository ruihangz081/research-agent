"""R4：Agent4 结构化结论台账的加载与确定性门禁。

`04_claims.json` 是分析报告结论的机器可读表达。它让系统能回答一个此前无法
确定性回答的问题：报告里的重要结论是否都有 SUPPORTED 证据支撑。此前只有
"引用了的必然真实"（citations.py 的方向性校验），这里补上反向的
"重要结论必然被引用"。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from .. import config
from ..research_plan import known_question_ids
from .enums import VerificationStatus
from .models import ClaimsFile
from .repository import SQLiteRepository


class ClaimsError(RuntimeError):
    """`04_claims.json` 缺失、损坏或未通过确定性门禁。"""


_CITATION_RE = re.compile(r"\[src:[^\]]+\]")
_ANNOTATION_RE = re.compile(r"\[[^\[\]]*(?:置信度|判断|推导|已验证事实|证据不足)[^\[\]]*\]")
_EMPHASIS_CHARS = str.maketrans({"*": None, "_": None, "`": None})
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_claim_text(text: str) -> str:
    """把结论文本归一化到可跨"台账 / 正文"比对的形式。

    台账里的 `text` 与分析报告正文表达的是同一句结论，但正文额外带有排版与
    标注：引用标记 `[src:...]`、`[判断｜置信度: 中]` 这类标注、`**` 强调、以及
    换行造成的空白差异。要求模型在写台账时逐字复现这些符号是不现实的——实测
    19 条 critical 结论只有 13 条能原样匹配，去掉引用后 14 条，而归一化后
    19 条全部命中。

    因此校验"对应关系"而非"字节相同"：剥掉引用、标注、强调符号和全部空白后
    必须能在正文中找到。这仍是确定性判定，只是把排版差异排除在外。
    """
    cleaned = _CITATION_RE.sub("", text)
    cleaned = _ANNOTATION_RE.sub("", cleaned)
    cleaned = cleaned.translate(_EMPHASIS_CHARS)
    return _WHITESPACE_RE.sub("", cleaned)


#: `importance` 与 `confidence` 是同一份 JSON 里相邻的两个枚举，取值域却不同
#: （`critical/major/minor` vs `high/medium/low`）。模型极易把 confidence 的取值
#: 写进 importance——实测一次 Agent4 运行里 8 条 claim 全部串错，整轮作废。
#: prompt 已明确禁止，但字段设计本身在诱导错误，因此在加载层做确定性归一化。
#:
#: 刻意**不**做位置映射（high→critical）：那会把 claim 升级进 critical 集合，
#: 而 critical 附带两条硬要求（必须有 SUPPORTED 证据、必须在终稿逐字保留），
#: 于是一个枚举笔误会从"整份台账校验失败"变成"交付被门禁阻断"，问题只是换了
#: 位置。归一化的目的是不让一次笔误废掉整轮，不是猜测作者的精确意图，因此
#: 一律落到不新增约束的档位。
_IMPORTANCE_ALIASES = {"high": "major", "medium": "major", "low": "minor"}


def load_claims_file(path: Path) -> ClaimsFile:
    """读取并校验 `04_claims.json`；修复已知的重要性枚举别名。"""
    if not path.is_file():
        raise ClaimsError(f"缺少结论台账 {path.name}，Agent4 未产出或产出不完整")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimsError(f"结论台账 {path.name} 无法解析：{exc}") from exc
    if not isinstance(raw, dict):
        raise ClaimsError(f"结论台账 {path.name} 必须是 JSON 对象")
    normalized = False
    claims = raw.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            alias = _IMPORTANCE_ALIASES.get(claim.get("importance"))
            if alias is not None:
                claim["importance"] = alias
                normalized = True
    try:
        result = ClaimsFile.model_validate(raw)
    except ValidationError as exc:
        raise ClaimsError(
            f"结论台账 {path.name} 校验失败：{_format_validation_error(exc)}"
        ) from exc
    if normalized:
        path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result


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
    analysis_text: str | None = None,
) -> list[str]:
    """对结论台账做确定性门禁，返回错误列表（空列表表示通过）。

    校验内容：
    1. 每条 claim 的 `question_id` 必须在固定需求清单内；
    2. `supporting_evidence_ids` 引用的证据必须存在、状态为 SUPPORTED，
       且其 `research_question_id` 与 claim 的 `question_id` 一致；
    3. 每个必答 `question_id` 至少有一条 claim（结论覆盖研究问题全集）；
    4. 传入 `analysis_text` 时，每条 claim 必须能对应到分析报告正文中的一句结论。

    第 4 条此前只是 prompt 里的期望（analyst.md 要求"逐字一致"），没有任何程序
    强制。结果台账可以包含正文里根本不存在的结论：Agent5 不写正文后，这类
    "台账独有"的结论既不会出现在交付物里，也不会被任何门禁发现，等于凭空多出
    一批无法追溯到报告的结论。
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

    normalized_analysis = (
        normalize_claim_text(analysis_text) if analysis_text is not None else None
    )

    covered_questions: set[str] = set()
    for claim in claims.claims:
        covered_questions.add(claim.question_id)
        if claim.question_id not in known_set:
            errors.append(
                f"claim {claim.claim_id} 引用了清单外的 question_id：{claim.question_id}"
            )
            continue
        if normalized_analysis is not None:
            normalized_claim = normalize_claim_text(claim.text)
            if normalized_claim and normalized_claim not in normalized_analysis:
                errors.append(
                    f"claim {claim.claim_id} 的结论文本在分析报告正文中找不到对应句子："
                    f"{claim.text[:60]}…（台账必须是正文结论的机读表达，不得新增结论）"
                )
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
