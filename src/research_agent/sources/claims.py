"""R4：Agent4 结构化结论台账的加载与确定性门禁。

`04_claims.json` 是分析报告结论的机器可读表达。它让系统能回答一个此前无法
确定性回答的问题：报告里的重要结论是否都有 SUPPORTED 证据支撑。此前只有
"引用了的必然真实"（citations.py 的方向性校验），这里补上反向的
"重要结论必然被引用"。
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import ValidationError

from .. import config
from ..research_plan import known_question_ids
from .enums import VerificationStatus
from .models import Claim, ClaimsFile
from .repository import SQLiteRepository


class ClaimsError(RuntimeError):
    """`04_claims.json` 缺失、损坏或未通过确定性门禁。"""


#: 引用标记：既匹配标准 ``[src:...]``，也匹配 Agent4 偶发把标注与引用合并进同一
#: 括号的 ``[事实｜src:...]`` 形式（拆分前）。若不兼容，融合式引用无法被剥除，
#: 会导致台账结论文本与正文归一无所产生的差异被误判为「结论不在正文中」。
_CITATION_RE = re.compile(
    r"\[(?:事实|已验证事实|推导|计算|判断|假设|证据不足)[｜|]src:[^\]]+\]"
    r"|\[src:[^\]]+\]"
    r"|\[ev\s*=\s*ev_[0-9a-fA-F]{32}\]"
)
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


#: 数值锚点：结论文本中的可量化事实（金额、增速、倍数、百分比等）。防幻觉门禁
#: 里用「关键数字是否都能在正文命中」替代「整句逐字匹配」——模型会把结论轻微
#: 改写（换连接词、调整语序、合并拆句），但数字通常原样保留。数字是结论的事实
#: 载荷，编造的结论不可能让一多半关键数字都恰好命中正文。
#:
#: 锚点必须是「数字 + 单位/符号」的组合：单位让 token 具有区分度，避免年份
#: （2026）、季度（Q2 里的 2）、序号等通用数字虚高命中率，从而放行凭空编造的新数字。
_NUMERIC_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?(?:%|倍|亿|万|港元|港币|美元|元|个|家|x|X)"
)

#: 无数字的定性/判断型结论（如"核心业务逼近零增长"）无法用数字锚点判定，退化为
#: 连续匹配块覆盖率：结论文本里有多少比例的字符落在正文的连续相同片段中。
#: 实测真实判断句约 0.73，而凭空编造的结论（正文完全不相关）约 0.0，区分度足够。
_MATCHING_BLOCK_MIN = 3
_NUMERIC_HIT_THRESHOLD = 0.5
_BLOCK_COVERAGE_THRESHOLD = 0.5


def _matching_block_coverage(
    claim: str, body: str, min_block: int = _MATCHING_BLOCK_MIN
) -> float:
    """返回 claim 中被 body 连续相同片段覆盖的字符占比（0~1）。"""
    if not claim:
        return 0.0
    matcher = SequenceMatcher(None, claim, body)
    covered = sum(
        block.size for block in matcher.get_matching_blocks() if block.size >= min_block
    )
    return covered / len(claim)


def _numeric_hit_ratio(claim: str, body: str) -> float | None:
    """返回 claim 中命中了 body 的数值锚点占比；claim 无数值锚点时返回 None。"""
    tokens = [token for token in _NUMERIC_TOKEN_RE.findall(claim) if token.strip(".")]
    if not tokens:
        return None
    hits = sum(1 for token in tokens if token in body)
    return hits / len(tokens)


def claim_matches_body(claim_text: str, normalized_body: str) -> bool:
    """判断一条结论文本是否能在分析报告正文中找到对应内容。

    放宽后的判定顺序（由严到松）：
    1. 归一化后逐字子串命中——最理想，直接通过；
    2. 数值锚点命中率 ≥ 0.5——数字是事实载荷，编造结论难以命中过半关键数字；
    3. 连续匹配块覆盖率 ≥ 0.5——覆盖无数字的定性/判断型结论。

    这替代了早期「必须是正文逐字子串」的过严约束：模型会轻微改写结论措辞，
    但只要关键事实（数字）与核心语义仍来自正文，就不应判定为"新增结论"。
    """
    normalized_claim = normalize_claim_text(claim_text)
    if not normalized_claim:
        return True
    if normalized_claim in normalized_body:
        return True
    numeric_ratio = _numeric_hit_ratio(normalized_claim, normalized_body)
    if numeric_ratio is not None and numeric_ratio >= _NUMERIC_HIT_THRESHOLD:
        return True
    return (
        _matching_block_coverage(normalized_claim, normalized_body)
        >= _BLOCK_COVERAGE_THRESHOLD
    )


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
    *,
    warnings: list[str] | None = None,
) -> list[str]:
    """对结论台账做确定性门禁，返回错误列表（空列表表示通过）。

    校验内容：
    1. 每条 claim 的 `question_id` 必须在固定需求清单内；
    2. `supporting_evidence_ids` 引用的证据必须存在、状态为 SUPPORTED；
       证据的 `research_question_id` 与 claim 的 `question_id` 不一致时只记入
       ``warnings``（不阻断）——模型对"该证据回答哪个问题"的归类常与 R1 固化
       清单有偏差，但证据本身真实、SUPPORTED，硬阻断会反复把整轮作废；
    3. 每个必答 `question_id` 至少有一条 claim（结论覆盖研究问题全集）；
    4. 传入 `analysis_text` 时，每条 claim 必须能对应到分析报告正文中的一句结论。

    第 4 条此前要求 claim 文本是正文的逐字子串，实测模型轻微改写结论措辞就会
    让几乎所有 claim 被误判为"新增结论"，整轮作废。现放宽为「数值锚点命中或
    文本块覆盖」，只要关键事实与语义仍来自正文即视为对应；凭空编造的结论仍会
    被拦下（其数字与文本都无法命中正文）。
    """
    errors: list[str] = []
    if warnings is None:
        warnings = []

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
        if normalized_analysis is not None and not claim_matches_body(
            claim.text, normalized_analysis
        ):
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
            # question_id 归类偏差不再阻断：证据存在且 SUPPORTED 已满足防幻觉的
            # 核心要求，跨问题挂证据更多是模型归类的噪声，反复作废整轮得不偿失。
            if evidence.research_question_id != claim.question_id:
                warnings.append(
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


class ClaimsSanitizeResult:
    """结论台账确定性清洗的结果。

    - ``claims``：清洗后的有效台账；``None`` 表示清洗后仍不可用，应关闭 claims 能力。
    - ``warnings``：清洗过程中记录的问题（跨问题挂载、引用被移除、覆盖缺口等）。
    - ``dropped_claim_ids``：被整体删除的 claim（文本不在正文、question_id 不存在、
      critical 无有效证据等）。
    """

    def __init__(
        self,
        claims: ClaimsFile | None,
        warnings: list[str],
        dropped_claim_ids: list[str],
    ) -> None:
        self.claims = claims
        self.warnings = warnings
        self.dropped_claim_ids = dropped_claim_ids


def sanitize_claims(
    claims: ClaimsFile,
    project_dir: Path,
    repository: SQLiteRepository,
    analysis_text: str | None = None,
) -> ClaimsSanitizeResult:
    """把台账清洗到「可安全交付」的最小集合，绝不阻断。

    这是 §4.1 的确定性实现：把此前 ``validate_claims`` 里会让整轮作废的软性错误
    全部转成「删除该 claim / 移除该引用 + 记录 warning」。只有真正影响真实性、且
    无法在本层修复的问题才由调用方（orchestrator）走 hard error 阻断（正文引用
    审计），台账本身的问题一律降级。

    清洗规则（与 §4.1 表格一一对应）：
    - claim 文本不在正文 → 删除该 claim，记 warning；
    - ``question_id`` 不在清单 → 删除该 claim，记 warning；
    - evidence ID 不存在 / 非 SUPPORTED → 从 claim 删除该引用；critical claim 无
      有效证据时删除该 claim；
    - 证据跨问题挂载 → 保留 claim，记 warning；
    - claim_id 重复 → 保留首个，删除后续，记 warning；
    - 必答问题无 claim 覆盖 → 记 warning（不阻断）。

    清洗后无法通过 ``ClaimsFile`` 模型校验（例如剩余条目仍含 critical 无证据）时
    返回 ``claims=None``，由调用方关闭 claims 能力。
    """
    warnings: list[str] = []
    dropped: list[str] = []

    known = known_question_ids(project_dir)
    if known is None:
        return ClaimsSanitizeResult(
            None,
            ["缺少研究需求清单，无法校验结论的问题归属"],
            [],
        )
    known_set = set(known)

    evidence_by_id = {
        item.evidence_id: item
        for item in repository.list_evidence(project_dir.name)
    }

    normalized_analysis = (
        normalize_claim_text(analysis_text) if analysis_text is not None else None
    )

    kept: list[Claim] = []
    seen_ids: set[str] = set()
    for claim in claims.claims:
        # question_id 不在固定清单 → 删除
        if claim.question_id not in known_set:
            dropped.append(claim.claim_id)
            warnings.append(
                f"claim {claim.claim_id} 的 question_id={claim.question_id} "
                "不在固定需求清单，已移除"
            )
            continue
        # 结论文本不在正文 → 删除（正文是结论的唯一来源）
        if normalized_analysis is not None and not claim_matches_body(
            claim.text, normalized_analysis
        ):
            dropped.append(claim.claim_id)
            warnings.append(
                f"claim {claim.claim_id} 的结论文本在分析报告正文中找不到对应，已移除"
            )
            continue
        # claim_id 重复 → 保留首个
        if claim.claim_id in seen_ids:
            dropped.append(claim.claim_id)
            warnings.append(f"claim {claim.claim_id} 的 claim_id 重复，已移除")
            continue
        seen_ids.add(claim.claim_id)

        # 过滤无效证据引用：不存在 / 非 SUPPORTED
        valid_evidence_ids: list[str] = []
        for evidence_id in claim.supporting_evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                warnings.append(
                    f"claim {claim.claim_id} 引用了不存在的证据 {evidence_id}，"
                    "已移除该引用"
                )
                continue
            if evidence.verification_status != VerificationStatus.SUPPORTED:
                warnings.append(
                    f"claim {claim.claim_id} 引用了非 SUPPORTED 证据 {evidence_id}"
                    f"（{evidence.verification_status.value}），已移除该引用"
                )
                continue
            if evidence.research_question_id != claim.question_id:
                warnings.append(
                    f"claim {claim.claim_id} 的 question_id={claim.question_id} "
                    f"与其支持证据 {evidence_id} 的 "
                    f"research_question_id={evidence.research_question_id} 不一致"
                )
            valid_evidence_ids.append(evidence_id)

        # critical claim 过滤后无有效证据 → 删除整条 claim
        if claim.importance == "critical" and not valid_evidence_ids:
            dropped.append(claim.claim_id)
            warnings.append(
                f"critical claim {claim.claim_id} 无有效 SUPPORTED 证据，已移除"
            )
            continue

        kept.append(
            claim.model_copy(update={"supporting_evidence_ids": valid_evidence_ids})
        )

    # 必答问题覆盖缺口只记 warning，不阻断
    covered = {claim.question_id for claim in kept}
    missing = known_set - covered
    for question_id in sorted(missing):
        warnings.append(
            f"研究问题 {question_id} 缺少结论覆盖（已降级，不阻断交付）"
        )

    try:
        sanitized = ClaimsFile(claims=kept)
    except ValidationError as exc:
        return ClaimsSanitizeResult(
            None,
            [f"结论台账清洗后仍不合法：{_format_validation_error(exc)}"],
            dropped,
        )
    return ClaimsSanitizeResult(sanitized, warnings, dropped)
