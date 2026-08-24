"""R3：结构化补研任务的加载、校验与确定性门禁。

`03_tasks.json` 是 Agent2↔3 之间补研缺口的结构化台账。相比自由文本 gap，
任务有稳定 task_id 与持久化状态，Agent2 逐条执行，Agent3 验收回填，
Orchestrator 用确定性门禁阻断未完成的 critical 任务——即使 Agent3 声明
`converged=true`，只要还有 critical 且 pending 的任务，也不能收敛。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .. import config
from ..pipeline_errors import DeterministicContentError
from ..research_plan import ResearchPlanError, require_plan
from .enums import SourceStatus, VerificationStatus
from .models import (
    EvidenceRecord,
    ResearchTask,
    ResearchTaskExecutionReport,
    ResearchTasksFile,
    ResearchTaskUpdate,
)
from .runtime import get_service

if TYPE_CHECKING:
    from .repository import SQLiteRepository
    from ..state import ProjectState


_TIER_RANK = {"S": 4, "A": 3, "B": 2, "D": 1, "unclassified": 0}
_SPACE_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


class TasksError(DeterministicContentError):
    """`03_tasks.json` 缺失、损坏或校验失败。

    台账损坏、ID 抄错、回填多字段这类错误是确定性内容错误：重试 Agent 不会改变
    已经写出的上游产物，因此继承 ``DeterministicContentError``，让 orchestrator
    走 ``except PipelineError`` 直接落状态、不再无意义重跑。
    """


def load_tasks_file(path: Path) -> ResearchTasksFile:
    """读取并校验 `03_tasks.json`；缺失时返回空台账（旧项目兼容）。"""
    if not path.is_file():
        return ResearchTasksFile(tasks=[])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TasksError(f"任务台账 {path.name} 无法解析：{exc}") from exc
    if not isinstance(raw, dict):
        raise TasksError(f"任务台账 {path.name} 必须是 JSON 对象")
    try:
        return ResearchTasksFile.model_validate(raw)
    except ValidationError as exc:
        raise TasksError(
            f"任务台账 {path.name} 校验失败：{_format_validation_error(exc)}"
        ) from exc


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "tasks"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def write_tasks_file(path: Path, tasks: ResearchTasksFile) -> None:
    """原子化写入任务台账（Agent3 每轮重写完整清单）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(tasks.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def blocking_pending_tasks(tasks: ResearchTasksFile) -> list[ResearchTask]:
    """返回未完成且未获人工豁免的 critical 任务。"""
    return [
        item
        for item in tasks.tasks
        if item.priority == "critical"
        and item.status not in {"completed", "waived"}
    ]


def pending_tasks(tasks: ResearchTasksFile) -> list[ResearchTask]:
    """返回全部待执行任务（按 critical 优先、task_id 排序）。"""
    order = {"critical": 0, "normal": 1}
    return sorted(
        (item for item in tasks.tasks if item.status == "pending"),
        key=lambda item: (order.get(item.priority, 1), item.task_id),
    )


def tasks_prompt_context(tasks: ResearchTasksFile) -> str:
    """Agent2 采集轮 prompt 注入的待执行任务清单。"""
    pending = pending_tasks(tasks)
    if not pending:
        return "\n\n## 结构化补研任务\n- 当前无待执行的补研任务。\n"
    lines = [
        "\n\n## 结构化补研任务（必须逐条执行）",
        "以下任务由 Agent3 验收缺口后产生，每条都有稳定 task_id。请逐条采集并在本轮输出中回应：",
        "",
        "| task_id | question_id | 优先级 | 任务描述 |",
        "|---|---|---|---|",
    ]
    for item in pending:
        lines.append(
            f"| `{item.task_id}` | `{item.question_id}` | "
            f"{item.priority} | {item.description}"
            + (f"；验收：{item.completion_criteria}" if item.completion_criteria else "")
            + " |"
        )
    return "\n".join(lines) + "\n"


def config_tasks_path(project_dir: Path) -> Path:
    return project_dir / config.FILE_TASKS


def task_results_path(state: "ProjectState", round_idx: int) -> Path:
    return (
        state.project_dir
        / config.FILE_RAW_DATA_DIR
        / config.FILE_TASK_RESULTS_ROUND.format(n=round_idx)
    )


def _normalize_identity(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip().casefold()


def _period_matches(evidence_period: str | None, target_period: str | None) -> bool:
    """允许目标年份范围匹配范围内的具体报告日期。"""
    if not target_period:
        return True
    evidence_value = _normalize_identity(evidence_period)
    target_value = _normalize_identity(target_period)
    if evidence_value == target_value:
        return True
    evidence_years = set(_YEAR_RE.findall(evidence_value))
    target_years = set(_YEAR_RE.findall(target_value))
    if not evidence_years or not target_years:
        return False
    if len(target_years) >= 2:
        start, end = min(map(int, target_years)), max(map(int, target_years))
        target_years = {str(year) for year in range(start, end + 1)}
    return bool(evidence_years & target_years)


def _completion_identity(task: ResearchTask | ResearchTaskUpdate) -> str:
    return _normalize_identity(task.completion_criteria or task.description)


def _identity_tuple(task: ResearchTask | ResearchTaskUpdate) -> tuple[Any, ...]:
    return (
        task.question_id,
        task.task_type,
        task.priority,
        _normalize_identity(task.target_period),
        task.min_source_tier,
        task.required_independent_sources,
        _completion_identity(task),
    )


def _preserve_existing_identity(
    update: ResearchTaskUpdate,
    existing: ResearchTask,
) -> ResearchTaskUpdate:
    """已有任务只允许更新执行状态，身份字段始终以持久化台账为准。"""
    return update.model_copy(
        update={
            "question_id": existing.question_id,
            "description": existing.description,
            "task_type": existing.task_type,
            "priority": existing.priority,
            "target_period": existing.target_period,
            "min_source_tier": existing.min_source_tier,
            "required_independent_sources": existing.required_independent_sources,
            "completion_criteria": existing.completion_criteria,
        }
    )


def derive_task_id(update: ResearchTaskUpdate) -> str:
    """由不可变任务身份生成稳定 ID，避免依赖模型生成编号。"""
    identity = json.dumps(
        list(_identity_tuple(update)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "rt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _known_question_ids(state: "ProjectState") -> set[str]:
    try:
        return {item.question_id for item in require_plan(state).requirements}
    except ResearchPlanError as exc:
        raise TasksError(str(exc)) from exc


def _resolve_evidence(
    evidence_id: str,
    evidence_lookup: dict[str, EvidenceRecord],
) -> EvidenceRecord | None:
    """按 ``evidence_id`` 精确查找；找不到时对疑似被 LLM 截断的 ID 做保守前缀解析。

    evidence_id 形如 ``ev_`` + 32 位十六进制。Agent3 偶发会把末尾字符截掉
    （如 ``ev_...5469cc53`` 写成 ``ev_...5469cc``），导致严格查找失败。仅当输入是
    **恰好一个** 真实 evidence_id 的严格前缀时才自愈返回该记录；否则返回 None，
    由调用方按未知证据 fail-closed，绝不把歧义 ID 静默解析成错误证据。
    """
    evidence = evidence_lookup.get(evidence_id)
    if evidence is not None:
        return evidence
    matches = [real for real in evidence_lookup if real.startswith(evidence_id)]
    if len(matches) == 1:
        return evidence_lookup[matches[0]]
    return None


def _validated_evidence_sources(
    update: ResearchTaskUpdate,
    evidence_lookup: dict[str, EvidenceRecord],
    repository: "SQLiteRepository",
    project_id: str,
) -> tuple[list[str], list[str]]:
    source_ids: list[str] = []
    evidence_ids: list[str] = []
    for raw_evidence_id in update.completed_evidence_ids:
        evidence = _resolve_evidence(raw_evidence_id, evidence_lookup)
        if evidence is None:
            raise TasksError(f"task references unknown EvidenceRecord: {raw_evidence_id}")
        evidence_id = evidence.evidence_id
        if evidence.research_question_id != update.question_id:
            continue
        if evidence.verification_status != VerificationStatus.SUPPORTED:
            raise TasksError(f"task evidence is not SUPPORTED: {evidence_id}")

        source = repository.get_source(evidence.source_id, project_id)
        chunk = repository.get_chunk(evidence.chunk_id, project_id)
        latest_version = (
            repository.latest_version(project_id, source.logical_source_id)
            if source is not None
            else 0
        )
        if (
            source is None
            or chunk is None
            or source.status == SourceStatus.SUPERSEDED
            or source.version != evidence.source_version
            or latest_version != evidence.source_version
            or evidence.excerpt not in chunk.text
        ):
            raise TasksError(
                f"task evidence has invalid source/version/chunk/excerpt: {evidence_id}"
            )
        if update.min_source_tier and (
            _TIER_RANK.get(evidence.source_tier, 0)
            < _TIER_RANK[update.min_source_tier]
        ):
            continue
        if not _period_matches(evidence.period, update.target_period):
            continue
        source_ids.append(evidence.source_id)
        evidence_ids.append(evidence_id)
    return sorted(set(source_ids)), evidence_ids


def apply_feedback_tasks(
    state: "ProjectState",
    feedback: Any,
) -> ResearchTasksFile:
    """合并 Agent3 更新，并由程序校验身份、证据与状态转换。"""
    updates = list(getattr(feedback, "tasks", []))
    unresolved_conflicts = [
        item for item in getattr(feedback, "conflicts", []) if not item.resolution
    ]
    free_text_gaps = [
        *getattr(feedback, "gap_list", []),
        *getattr(feedback, "need_rework_topics", []),
        *getattr(feedback, "next_round_focus", []),
    ]
    if (free_text_gaps or unresolved_conflicts) and not updates:
        raise TasksError("Agent3 reported an unresolved gap without a structured task")

    path = config_tasks_path(state.project_dir)
    ledger = load_tasks_file(path)
    tasks_by_id = {task.task_id: task for task in ledger.tasks}
    known_question_ids = _known_question_ids(state)
    service = get_service(config.SOURCE_DATA_DIR)
    repository = service.repository
    project_id = state.project_dir.name
    evidence_lookup = {
        item.evidence_id: item for item in repository.list_evidence(project_id)
    }
    round_idx = int(getattr(feedback, "round", state.collect_round or 1))

    normalized_identity_updates: list[str] = []
    rejected_completion_updates: list[dict[str, int | str]] = []
    for update in updates:
        if update.task_id is None:
            task_id = derive_task_id(update)
            existing = tasks_by_id.get(task_id)
        else:
            task_id = update.task_id
            existing = tasks_by_id.get(task_id)
            if existing is None:
                raise TasksError(f"research task update references unknown task_id: {task_id}")

        if existing is not None and _identity_tuple(existing) != _identity_tuple(update):
            update = _preserve_existing_identity(update, existing)
            normalized_identity_updates.append(task_id)
        if update.question_id not in known_question_ids:
            raise TasksError(
                f"research task references unknown question_id: {update.question_id}"
            )
        if update.status == "waived" and (
            existing is None or existing.status != "waived"
        ):
            raise TasksError(
                f"research task waiver requires explicit human approval: {task_id}"
            )

        source_ids, completed_evidence_ids = _validated_evidence_sources(
            update,
            evidence_lookup,
            repository,
            project_id,
        )
        effective_status = update.status
        if (
            update.status == "completed"
            and len(source_ids) < update.required_independent_sources
        ):
            effective_status = "pending"
            rejected_completion_updates.append(
                {
                    "task_id": task_id,
                    "qualifying_independent_sources": len(source_ids),
                    "required_independent_sources": update.required_independent_sources,
                }
            )

        created_round = existing.created_round if existing else round_idx
        tasks_by_id[task_id] = ResearchTask(
            task_id=task_id,
            question_id=update.question_id,
            description=update.description,
            task_type=update.task_type,
            priority=update.priority,
            target_period=update.target_period,
            min_source_tier=update.min_source_tier,
            required_independent_sources=update.required_independent_sources,
            completion_criteria=update.completion_criteria,
            status=effective_status,
            source_ids=source_ids,
            completed_evidence_ids=completed_evidence_ids,
            created_round=created_round,
            updated_round=round_idx,
            completed_round=round_idx if effective_status == "completed" else None,
            blocked_reason=update.blocked_reason,
        )

    merged = ResearchTasksFile(
        tasks=sorted(tasks_by_id.values(), key=lambda item: item.task_id)
    )
    write_tasks_file(path, merged)
    state.notes["research_task_counts"] = {
        status: sum(task.status == status for task in merged.tasks)
        for status in ("pending", "completed", "blocked", "waived")
    }
    state.notes["pending_critical_tasks"] = [
        task.task_id for task in blocking_pending_tasks(merged)
    ]
    if normalized_identity_updates:
        state.notes["normalized_research_task_updates"] = sorted(
            set(normalized_identity_updates)
        )
    if rejected_completion_updates:
        state.notes["rejected_research_task_completions"] = (
            rejected_completion_updates
        )
    state.save()
    return merged


def ensure_analysis_gap_tasks(
    state: "ProjectState",
    gaps: list[Any],
    *,
    round_idx: int,
) -> ResearchTasksFile:
    """把 Agent4 的 needs_more_research 请求写入同一份 ``03_tasks.json``。"""
    updates = []
    for gap in gaps:
        question_id = gap.get("question_id") if isinstance(gap, dict) else gap.question_id
        reason = gap.get("reason") if isinstance(gap, dict) else gap.reason
        needed = (
            gap.get("needed_evidence")
            if isinstance(gap, dict)
            else gap.needed_evidence
        )
        updates.append(
            ResearchTaskUpdate(
                question_id=question_id,
                description=reason,
                task_type="analysis_gap",
                priority="critical",
                completion_criteria=needed,
                status="pending",
            )
        )
    feedback = SimpleNamespace(
        round=round_idx,
        tasks=updates,
        gap_list=[],
        need_rework_topics=[],
        next_round_focus=[],
        conflicts=[],
    )
    return apply_feedback_tasks(state, feedback)


def load_task_execution_report(
    state: "ProjectState",
    round_idx: int,
    path: Path | None = None,
) -> ResearchTaskExecutionReport:
    """校验 Agent2 的逐任务回填、任务覆盖和真实项目来源。"""
    report_path = path or task_results_path(state, round_idx)
    if not report_path.is_file():
        raise TasksError(f"Agent2 未生成任务回填文件：{report_path}")
    try:
        report = ResearchTaskExecutionReport.model_validate_json(
            report_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise TasksError(f"Agent2 任务回填文件损坏：{report_path}: {exc}") from exc
    if report.round != round_idx:
        raise TasksError(
            f"Agent2 任务回填轮次不匹配：expected {round_idx}, got {report.round}"
        )

    ledger = load_tasks_file(config_tasks_path(state.project_dir))
    tasks_by_id = {task.task_id: task for task in ledger.tasks}
    reported_ids = {item.task_id for item in report.results}
    unknown_ids = sorted(reported_ids - set(tasks_by_id))
    if unknown_ids:
        raise TasksError(
            "Agent2 task results reference unknown task_id: " + ", ".join(unknown_ids)
        )
    required_ids = {task.task_id for task in blocking_pending_tasks(ledger)}
    missing_ids = sorted(required_ids - reported_ids)
    if missing_ids:
        raise TasksError(
            "Agent2 task results omitted critical task_id: " + ", ".join(missing_ids)
        )

    known_source_ids = {
        source.source_id
        for source in get_service(config.SOURCE_DATA_DIR).list_sources(
            state.project_dir.name
        )
    }
    fabricated = sorted(
        {
            source_id
            for result in report.results
            for source_id in result.source_ids
            if source_id not in known_source_ids
        }
    )
    if fabricated:
        raise TasksError(
            "Agent2 task results reference unknown source_id: " + ", ".join(fabricated)
        )
    return report
