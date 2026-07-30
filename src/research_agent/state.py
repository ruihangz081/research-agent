"""状态模型与持久化。

每个调研项目的生命周期由 Stage 枚举驱动，状态写入 `state.json` 支持断点续跑。
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import config


class Stage(str, Enum):
    """调研流程阶段。"""

    INIT = "init"
    PLANNING = "planning"  # Agent1 运行中
    AWAIT_CLARIFICATION = "await_clarification"  # ⏸ Agent1 追问，等用户回答（Web）
    AWAIT_OUTLINE_APPROVAL = "await_outline_approval"  # ⏸ 检查点 1
    SOURCING = "sourcing"  # Agent2 产出源清单
    AWAIT_SOURCE_APPROVAL = "await_source_approval"  # ⏸ 检查点 2
    COLLECTING_AND_VALIDATING = "collecting_and_validating"  # Agent2↔3 循环
    AWAIT_FINAL_SOURCE_APPROVAL = "await_final_source_approval"  # ⏸ 检查点 3
    ANALYZING = "analyzing"  # Agent4
    FORMATTING = "formatting"  # Agent5
    DONE = "done"

    @property
    def is_checkpoint(self) -> bool:
        return self in {
            Stage.AWAIT_OUTLINE_APPROVAL,
            Stage.AWAIT_SOURCE_APPROVAL,
            Stage.AWAIT_FINAL_SOURCE_APPROVAL,
        }

    @property
    def is_agent_running(self) -> bool:
        """该阶段是否代表"某个 Agent 正在执行"。

        用于服务重启后识别被中断的项目：停在这些阶段且没有活动任务，
        说明进程在 Agent 执行途中退出了。
        """
        return self in {
            Stage.PLANNING,
            Stage.SOURCING,
            Stage.COLLECTING_AND_VALIDATING,
            Stage.ANALYZING,
            Stage.FORMATTING,
        }


class ProjectState(BaseModel):
    """一次调研的全量状态。"""

    topic: str
    date_str: str = Field(default_factory=lambda: datetime.now().strftime("%Y%m%d"))
    stage: Stage = Stage.INIT
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    # Agent1 产出（供后续 agent 读取）
    outline_path: str | None = None

    # Agent1 需求澄清对话（Web 交互模式；CLI 在 AgentSession 内部完成对话）
    clarification: list[dict[str, str]] = Field(default_factory=list)

    # Agent2/3 产出
    sources_draft_path: str | None = None
    sources_final_path: str | None = None
    validation_report_path: str | None = None
    collect_round: int = 0  # 已完成的采集-验证轮次（从 0 开始）
    max_collect_rounds: int = 3  # 最大轮次上限
    last_feedback_path: str | None = None  # Agent3 最近一次结构化反馈 JSON
    converged: bool = False  # Agent3 是否判定收敛

    # Agent4/5 产出
    analysis_path: str | None = None
    final_report_path: str | None = None
    chart_manifest_path: str | None = None
    final_report_html_path: str | None = None
    final_report_tex_path: str | None = None
    final_report_pdf_path: str | None = None
    final_report_typeset_pdf_path: str | None = None

    # 失败与重试（支持工作台显式重试，重启后仍可见）
    failed_stage: str | None = None  # 失败发生的阶段名
    last_error: str | None = None  # 最近一次失败原因
    retry_count: int = 0  # 用户触发重试的次数

    # Token 用量累计（明细按天存 token_usage.jsonl）
    token_usage: dict[str, Any] = Field(default_factory=dict)

    # 附加元数据
    notes: dict[str, Any] = Field(default_factory=dict)

    # === 便捷属性 ===

    @property
    def project_dir(self) -> Path:
        return config.project_dir_for(self.topic, self.date_str)

    @property
    def state_file(self) -> Path:
        return self.project_dir / config.FILE_STATE

    # === 持久化 ===

    def save(self) -> None:
        """原子化写入 state.json。"""
        self.updated_at = datetime.now().isoformat()
        self.project_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    @classmethod
    def load(cls, project_dir: Path) -> "ProjectState":
        state_file = project_dir / config.FILE_STATE
        if not state_file.exists():
            raise FileNotFoundError(f"State file not found: {state_file}")
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return cls(**data)

    # === 状态推进 ===

    def advance_to(self, new_stage: Stage) -> None:
        """切换到新阶段并立即持久化。"""
        self.stage = new_stage
        self.save()

    # === 失败标记（供工作台重试使用） ===

    def mark_failure(self, stage_name: str, error: str) -> None:
        """记录失败阶段与原因，持久化后可在重启后继续重试。"""
        self.failed_stage = stage_name
        self.last_error = str(error)[:2000]
        self.save()

    def clear_failure(self) -> None:
        """清除失败标记（成功推进或用户触发重试时调用）。"""
        if self.failed_stage is None and self.last_error is None:
            return
        self.failed_stage = None
        self.last_error = None
        self.save()
