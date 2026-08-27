"""R3 回归测试：结构化补研任务协议与确定性门禁。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.sources.models import ResearchTask, ResearchTasksFile
from research_agent.sources.tasks import (
    TasksError,
    _period_matches,
    apply_feedback_tasks,
    blocking_pending_tasks,
    ensure_analysis_gap_tasks,
    load_task_execution_report,
    load_tasks_file,
    pending_tasks,
    task_results_path,
    tasks_prompt_context,
    write_tasks_file,
)


# ═══════════════════════════════════════════════════════════════
# 模型层
# ═══════════════════════════════════════════════════════════════


def test_completed_task_requires_evidence() -> None:
    with pytest.raises(ValueError, match="completed 任务必须回填"):
        ResearchTask(
            task_id="t1",
            question_id="q1",
            description="补政策文件",
            status="completed",
            completed_evidence_ids=[],
            created_round=1,
        )


def test_duplicate_task_ids_rejected() -> None:
    with pytest.raises(ValueError, match="task_id 重复"):
        ResearchTasksFile.model_validate(
            {
                "tasks": [
                    {
                        "task_id": "t1",
                        "question_id": "q1",
                        "description": "a",
                        "created_round": 1,
                    },
                    {
                        "task_id": "t1",
                        "question_id": "q2",
                        "description": "b",
                        "created_round": 1,
                    },
                ]
            }
        )


def test_empty_description_rejected() -> None:
    with pytest.raises(ValueError, match="任务描述不能为空"):
        ResearchTask(
            task_id="t1",
            question_id="q1",
            description="   ",
            created_round=1,
        )


# ═══════════════════════════════════════════════════════════════
# 加载与持久化
# ═══════════════════════════════════════════════════════════════


def test_load_tasks_file_missing_returns_empty(tmp_path: Path) -> None:
    tasks = load_tasks_file(tmp_path / "03_tasks.json")
    assert tasks.tasks == []


def test_write_and_reload_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "03_tasks.json"
    tasks = ResearchTasksFile(
        tasks=[
            ResearchTask(
                task_id="t1",
                question_id="q1",
                description="补 2025 政策文件",
                priority="critical",
                created_round=1,
            )
        ]
    )
    write_tasks_file(path, tasks)
    reloaded = load_tasks_file(path)
    assert reloaded.tasks[0].task_id == "t1"
    assert reloaded.tasks[0].status == "pending"


def test_load_tasks_file_damaged(tmp_path: Path) -> None:
    path = tmp_path / "03_tasks.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(TasksError, match="无法解析"):
        load_tasks_file(path)


def test_task_target_year_range_accepts_concrete_report_date() -> None:
    assert _period_matches("2026-07-15", "2026-2027") is True
    assert _period_matches("2027 年年度报告", "2026-2027") is True
    assert _period_matches("2025-12-31", "2026-2027") is False


# ═══════════════════════════════════════════════════════════════
# 门禁逻辑
# ═══════════════════════════════════════════════════════════════


def _task(task_id: str, priority: str, status: str) -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        question_id="q1",
        description=f"任务 {task_id}",
        priority=priority,
        status=status,
        completed_evidence_ids=["ev-1"] if status == "completed" else [],
        created_round=1,
        blocked_reason="无可用源" if status == "blocked" else None,
    )


def test_blocking_pending_tasks_only_critical() -> None:
    tasks = ResearchTasksFile(
        tasks=[
            _task("t1", "critical", "pending"),
            _task("t2", "normal", "pending"),
            _task("t3", "critical", "completed"),
            _task("t4", "critical", "waived"),
        ]
    )
    blocking = blocking_pending_tasks(tasks)
    assert [item.task_id for item in blocking] == ["t1"]


def test_no_blocking_when_all_done() -> None:
    tasks = ResearchTasksFile(
        tasks=[
            _task("t1", "critical", "completed"),
            _task("t2", "normal", "pending"),
        ]
    )
    assert blocking_pending_tasks(tasks) == []


def test_pending_tasks_sorted_critical_first() -> None:
    tasks = ResearchTasksFile(
        tasks=[
            _task("t3", "normal", "pending"),
            _task("t1", "critical", "pending"),
            _task("t2", "critical", "pending"),
        ]
    )
    ordered = pending_tasks(tasks)
    assert [item.task_id for item in ordered] == ["t1", "t2", "t3"]


def test_tasks_prompt_context_lists_pending() -> None:
    tasks = ResearchTasksFile(
        tasks=[
            _task("t1", "critical", "pending"),
            _task("t2", "normal", "completed"),
        ]
    )
    context = tasks_prompt_context(tasks)
    assert "t1" in context
    assert "t2" not in context  # 已完成的 task 不再注入给 Agent2
    assert "结构化补研任务" in context


def test_tasks_prompt_context_empty() -> None:
    context = tasks_prompt_context(ResearchTasksFile(tasks=[]))
    assert "无待执行的补研任务" in context


# ═══════════════════════════════════════════════════════════════
# 与 validator 的集成（feedback.tasks 落盘）
# ═══════════════════════════════════════════════════════════════


def test_validation_feedback_accepts_tasks() -> None:
    from research_agent.agents.validator import ValidationFeedback

    feedback = ValidationFeedback(
        round=1,
        converged=False,
        tasks=[
            {
                "task_id": "t1",
                "question_id": "q1",
                "description": "补政策文件",
                "priority": "critical",
                "status": "pending",
                "created_round": 1,
            }
        ],
    )
    assert len(feedback.tasks) == 1
    assert feedback.tasks[0].task_id == "t1"


def test_validation_feedback_rejects_completed_without_evidence() -> None:
    from research_agent.agents.validator import ValidationFeedback

    with pytest.raises(ValueError, match="completed 任务必须回填"):
        ValidationFeedback(
            round=1,
            converged=True,
            tasks=[
                {
                    "task_id": "t1",
                    "question_id": "q1",
                    "description": "补政策文件",
                    "priority": "critical",
                    "status": "completed",
                    "completed_evidence_ids": [],
                    "created_round": 1,
                }
            ],
        )


def test_load_feedback_downgrades_completed_without_evidence(
    tmp_path: Path,
) -> None:
    from research_agent.agents.validator import load_feedback

    path = tmp_path / "feedback.json"
    path.write_text(
        json.dumps(
            {
                "round": 2,
                "converged": True,
                "tasks": [
                    {
                        "task_id": "t1",
                        "question_id": "q1",
                        "description": "补政策文件",
                        "priority": "critical",
                        "status": "completed",
                        "completed_evidence_ids": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    feedback = load_feedback(path)

    assert feedback.tasks[0].status == "pending"
    assert feedback.tasks[0].completed_evidence_ids == []
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["tasks"][0]["status"] == "pending"


def test_load_feedback_preserves_completed_with_evidence(tmp_path: Path) -> None:
    from research_agent.agents.validator import load_feedback

    path = tmp_path / "feedback.json"
    path.write_text(
        json.dumps(
            {
                "round": 2,
                "converged": True,
                "tasks": [
                    {
                        "task_id": "t1",
                        "question_id": "q1",
                        "description": "补政策文件",
                        "priority": "critical",
                        "status": "completed",
                        "completed_evidence_ids": ["ev-1"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    feedback = load_feedback(path)

    assert feedback.tasks[0].status == "completed"
    assert feedback.tasks[0].completed_evidence_ids == ["ev-1"]


# ═══════════════════════════════════════════════════════════════
# 与 Orchestrator 收敛门禁的集成
# ═══════════════════════════════════════════════════════════════


def _prepared_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from research_agent import config
    from research_agent.research_plan import derive_plan_from_outline, save_plan
    from research_agent.sources import (
        LocalObjectStore,
        SQLiteRepository,
        SourceService,
    )
    from research_agent.sources.enums import VerificationStatus
    from research_agent.sources.models import EvidenceRecord
    from research_agent.sources.runtime import reset_runtime
    from research_agent.state import ProjectState

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    reset_runtime()
    state = ProjectState(topic="r3", date_str="20260730")
    state.project_dir.mkdir(parents=True)
    outline_text = (
        f"# {state.topic}\n\n## 二、核心研究问题\n1. 研究问题 q1\n"
    )
    outline = state.project_dir / config.FILE_OUTLINE
    outline.write_text(outline_text, encoding="utf-8")
    state.outline_path = str(outline)
    plan, _ = derive_plan_from_outline(state.topic, outline_text)
    for item, question_id in zip(plan.requirements, ("q1",), strict=True):
        item.question_id = question_id
    save_plan(state, plan)

    repository = SQLiteRepository(config.SOURCE_DATA_DIR / "catalog.sqlite3")
    service = SourceService(
        repository, LocalObjectStore(config.SOURCE_DATA_DIR / "objects")
    )
    source = service.register_bytes(
        state.project_dir.name, "facts.txt", b"Revenue reached 42 million"
    ).source
    source.source_tier = "S"
    repository.update_source(source)
    service.parse_source(state.project_dir.name, source.source_id)
    chunk = service.index_source(state.project_dir.name, source.source_id)[0]
    service.activate(state.project_dir.name, source.source_id)
    service.record_evidence(
        EvidenceRecord(
            evidence_id="ev-1",
            project_id=state.project_dir.name,
            research_question_id="q1",
            claim="Revenue reached 42 million",
            source_id=source.source_id,
            source_version=source.version,
            chunk_id=chunk.chunk_id,
            locator=chunk.locators[0],
            excerpt="Revenue reached 42 million",
            source_tier="S",
            verification_status=VerificationStatus.SUPPORTED,
            confidence=1,
        )
    )
    service.record_evidence(
        EvidenceRecord(
            evidence_id="ev-other-question",
            project_id=state.project_dir.name,
            research_question_id="q2",
            claim="Revenue reached 42 million",
            source_id=source.source_id,
            source_version=source.version,
            chunk_id=chunk.chunk_id,
            locator=chunk.locators[0],
            excerpt="Revenue reached 42 million",
            source_tier="S",
            verification_status=VerificationStatus.SUPPORTED,
            confidence=1,
        )
    )
    return state, repository


def test_critical_pending_task_blocks_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback
    from research_agent.orchestrator import _deterministic_convergence

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        # 证据已足够、Agent3 声明 converged，但有一个 critical pending 任务
        tasks_path = state.project_dir / config.FILE_TASKS
        write_tasks_file(
            tasks_path,
            ResearchTasksFile(
                tasks=[
                    ResearchTask(
                        task_id="t1",
                        question_id="q1",
                        description="补政策文件",
                        priority="critical",
                        status="pending",
                        created_round=1,
                    )
                ]
            ),
        )
        feedback = ValidationFeedback(round=1, converged=True)
        assert _deterministic_convergence(state, feedback) is False
        assert state.notes["pending_critical_tasks"] == ["t1"]
    finally:
        repository.close()


def test_completed_critical_task_allows_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback
    from research_agent.orchestrator import _deterministic_convergence

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        tasks_path = state.project_dir / config.FILE_TASKS
        write_tasks_file(
            tasks_path,
            ResearchTasksFile(
                tasks=[
                    ResearchTask(
                        task_id="t1",
                        question_id="q1",
                        description="补政策文件",
                        priority="critical",
                        status="completed",
                        completed_evidence_ids=["ev-1"],
                        created_round=1,
                        completed_round=2,
                    )
                ]
            ),
        )
        feedback = ValidationFeedback(round=2, converged=True)
        assert _deterministic_convergence(state, feedback) is True
        assert state.notes["pending_critical_tasks"] == []
    finally:
        repository.close()


def test_normal_pending_task_does_not_block_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback
    from research_agent.orchestrator import _deterministic_convergence

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        tasks_path = state.project_dir / config.FILE_TASKS
        write_tasks_file(
            tasks_path,
            ResearchTasksFile(
                tasks=[
                    ResearchTask(
                        task_id="t1",
                        question_id="q1",
                        description="补充行业背景",
                        priority="normal",
                        status="pending",
                        created_round=1,
                    )
                ]
            ),
        )
        feedback = ValidationFeedback(round=1, converged=True)
        assert _deterministic_convergence(state, feedback) is True
        assert state.notes["pending_critical_tasks"] == []
    finally:
        repository.close()


def test_load_feedback_repairs_zero_independent_source_count(tmp_path: Path) -> None:
    from research_agent.agents.validator import load_feedback

    feedback_path = tmp_path / "feedback_round_2.json"
    payload = {
        "round": 2,
        "converged": False,
        "tasks": [
            {
                "task_id": None,
                "question_id": "q1",
                "description": "完成 DCF 参数化与敏感性分析",
                "task_type": "analysis_gap",
                "priority": "normal",
                "required_independent_sources": 0,
                "status": "pending",
            }
        ],
    }
    feedback_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    feedback = load_feedback(feedback_path)

    assert feedback.tasks[0].required_independent_sources == 1
    persisted = json.loads(feedback_path.read_text(encoding="utf-8"))
    assert persisted["tasks"][0]["required_independent_sources"] == 1


# ═══════════════════════════════════════════════════════════════
# 远端 R3 基线上的强化契约
# ═══════════════════════════════════════════════════════════════


def _task_update(
    *,
    task_id: str | None = None,
    status: str = "pending",
    priority: str = "critical",
    completed_evidence_ids: list[str] | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "question_id": "q1",
        "description": "补齐 2025 年市场规模",
        "task_type": "coverage_gap",
        "priority": priority,
        "target_period": None,
        "min_source_tier": "A",
        "required_independent_sources": 1,
        "completion_criteria": "形成一条 A 级及以上的 SUPPORTED EvidenceRecord",
        "status": status,
        "completed_evidence_ids": completed_evidence_ids or [],
    }


def test_feedback_retry_reuses_system_derived_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        first = apply_feedback_tasks(
            state,
            ValidationFeedback(round=1, converged=False, tasks=[_task_update()]),
        )
        second = apply_feedback_tasks(
            state,
            ValidationFeedback(round=2, converged=False, tasks=[_task_update()]),
        )
        assert len(second.tasks) == 1
        assert second.tasks[0].task_id == first.tasks[0].task_id
        assert second.tasks[0].task_id.startswith("rt_")
        assert second.tasks[0].created_round == 1
        assert second.tasks[0].updated_round == 2
    finally:
        repository.close()


def test_task_completion_requires_real_supported_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        pending = apply_feedback_tasks(
            state,
            ValidationFeedback(round=1, converged=False, tasks=[_task_update()]),
        )
        task_id = pending.tasks[0].task_id

        with pytest.raises(TasksError, match="unknown EvidenceRecord"):
            apply_feedback_tasks(
                state,
                ValidationFeedback(
                    round=2,
                    converged=True,
                    tasks=[
                        _task_update(
                            task_id=task_id,
                            status="completed",
                            completed_evidence_ids=["fabricated"],
                        )
                    ],
                ),
            )

        completed = apply_feedback_tasks(
            state,
            ValidationFeedback(
                round=2,
                converged=True,
                tasks=[
                    _task_update(
                        task_id=task_id,
                        status="completed",
                        completed_evidence_ids=["ev-1"],
                    )
                ],
            ),
        )
        assert completed.tasks[0].status == "completed"
        assert completed.tasks[0].source_ids
    finally:
        repository.close()


def test_task_completion_ignores_evidence_from_another_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        pending = apply_feedback_tasks(
            state,
            ValidationFeedback(round=1, converged=False, tasks=[_task_update()]),
        )
        completed = apply_feedback_tasks(
            state,
            ValidationFeedback(
                round=2,
                converged=True,
                tasks=[
                    _task_update(
                        task_id=pending.tasks[0].task_id,
                        status="completed",
                        completed_evidence_ids=["ev-1", "ev-other-question"],
                    )
                ],
            ),
        )

        assert completed.tasks[0].completed_evidence_ids == ["ev-1"]
    finally:
        repository.close()


def test_task_completion_without_enough_sources_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        update = _task_update()
        update["required_independent_sources"] = 2
        pending = apply_feedback_tasks(
            state,
            ValidationFeedback(round=1, converged=False, tasks=[update]),
        )
        merged = apply_feedback_tasks(
            state,
            ValidationFeedback(
                round=2,
                converged=True,
                tasks=[
                    _task_update(
                        task_id=pending.tasks[0].task_id,
                        status="completed",
                        completed_evidence_ids=["ev-1"],
                    )
                ],
            ),
        )

        assert merged.tasks[0].status == "pending"
        assert state.notes["rejected_research_task_completions"] == [
            {
                "task_id": pending.tasks[0].task_id,
                "qualifying_independent_sources": 1,
                "required_independent_sources": 2,
            }
        ]
    finally:
        repository.close()


def test_existing_task_identity_changes_are_ignored_without_weakening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        ledger = apply_feedback_tasks(
            state,
            ValidationFeedback(round=1, converged=False, tasks=[_task_update()]),
        )
        update = _task_update(
            task_id=ledger.tasks[0].task_id,
            status="completed",
            priority="normal",
            completed_evidence_ids=["ev-1"],
        )
        update["completion_criteria"] = "已完成：本轮找到一条证据"

        merged = apply_feedback_tasks(
            state,
            ValidationFeedback(round=2, converged=False, tasks=[update]),
        )

        task = merged.tasks[0]
        assert task.status == "completed"
        assert task.priority == "critical"
        assert task.completion_criteria == _task_update()["completion_criteria"]
        assert task.completed_evidence_ids == ["ev-1"]
        assert state.notes["normalized_research_task_updates"] == [task.task_id]
    finally:
        repository.close()


def test_agent2_execution_report_requires_real_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        ledger = apply_feedback_tasks(
            state,
            ValidationFeedback(round=1, converged=False, tasks=[_task_update()]),
        )
        result_path = task_results_path(state, 2)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "round": 2,
                    "results": [
                        {
                            "task_id": ledger.tasks[0].task_id,
                            "status": "sourced",
                            "source_ids": ["fabricated"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(TasksError, match="unknown source_id"):
            load_task_execution_report(state, 2)
    finally:
        repository.close()


def test_agent2_execution_report_cannot_omit_critical_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        apply_feedback_tasks(
            state,
            ValidationFeedback(round=1, converged=False, tasks=[_task_update()]),
        )
        result_path = task_results_path(state, 2)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"schema_version": "1.0", "round": 2, "results": []}),
            encoding="utf-8",
        )
        with pytest.raises(TasksError, match="omitted critical task_id"):
            load_task_execution_report(state, 2)
    finally:
        repository.close()


def test_free_text_gap_requires_structured_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.agents.validator import ValidationFeedback

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        with pytest.raises(TasksError, match="without a structured task"):
            apply_feedback_tasks(
                state,
                ValidationFeedback(
                    round=1,
                    converged=False,
                    gap_list=["缺少关键数据"],
                    tasks=[],
                ),
            )
    finally:
        repository.close()


def test_agent4_gap_enters_same_canonical_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        first = ensure_analysis_gap_tasks(
            state,
            [
                {
                    "question_id": "q1",
                    "reason": "缺少 2026 年数据",
                    "needed_evidence": "一条可验证的 2026 年数据",
                }
            ],
            round_idx=2,
        )
        second = ensure_analysis_gap_tasks(
            state,
            [
                {
                    "question_id": "q1",
                    "reason": "缺少 2026 年数据",
                    "needed_evidence": "一条可验证的 2026 年数据",
                }
            ],
            round_idx=3,
        )
        assert len(second.tasks) == 1
        assert second.tasks[0].task_id == first.tasks[0].task_id
        assert second.tasks[0].task_type == "analysis_gap"
        assert second.tasks[0].priority == "critical"
    finally:
        repository.close()


def test_delivery_gate_blocks_unfinished_critical_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent.orchestrator import DeliveryBlockedError, _assert_delivery_ready

    state, repository = _prepared_state(tmp_path, monkeypatch)
    try:
        write_tasks_file(
            state.project_dir / config.FILE_TASKS,
            ResearchTasksFile(
                tasks=[
                    ResearchTask(
                        task_id="rt_blocking",
                        question_id="q1",
                        description="补齐关键证据",
                        priority="critical",
                        status="pending",
                        created_round=1,
                    )
                ]
            ),
        )
        with pytest.raises(DeliveryBlockedError, match="rt_blocking"):
            _assert_delivery_ready(state)
        assert state.notes["delivery_blocked_stage"] == "research_tasks"
    finally:
        repository.close()
