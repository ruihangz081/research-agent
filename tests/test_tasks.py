"""R3 回归测试：结构化补研任务协议与确定性门禁。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.sources.models import ResearchTask, ResearchTasksFile
from research_agent.sources.tasks import (
    TasksError,
    blocking_pending_tasks,
    load_tasks_file,
    pending_tasks,
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
