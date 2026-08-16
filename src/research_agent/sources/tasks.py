"""R3：结构化补研任务的加载、校验与确定性门禁。

`03_tasks.json` 是 Agent2↔3 之间补研缺口的结构化台账。相比自由文本 gap，
任务有稳定 task_id 与持久化状态，Agent2 逐条执行，Agent3 验收回填，
Orchestrator 用确定性门禁阻断未完成的 critical 任务——即使 Agent3 声明
`converged=true`，只要还有 critical 且 pending 的任务，也不能收敛。
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .. import config
from .models import ResearchTask, ResearchTasksFile


class TasksError(RuntimeError):
    """`03_tasks.json` 缺失、损坏或校验失败。"""


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
    """返回阻断收敛的 critical 且 pending 的任务。"""
    return [
        item
        for item in tasks.tasks
        if item.priority == "critical" and item.status == "pending"
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
            f"{item.priority} | {item.description} |"
        )
    return "\n".join(lines) + "\n"


def config_tasks_path(project_dir: Path) -> Path:
    return project_dir / config.FILE_TASKS
