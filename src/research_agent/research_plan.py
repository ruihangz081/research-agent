"""研究需求清单（R1）：研究开始前固定下来的必答问题集合。

为什么需要它
------------
在此之前，`orchestrator` 与 `formatter` 都是从"已经存在且状态为 SUPPORTED 的
EvidenceRecord"里提取 `research_question_id`，再据此构造 `ResearchRequirement`。
这形成循环定义——系统根据"已经找到了什么"决定"应该检查什么"。某个必答问题一条
证据都没找到时，它压根不会进入质量门的要求集合，于是被静默放过。

本模块把要求集合前移到研究启动阶段：Agent1 产出提纲的同一个阶段，确定性地把提纲
里的《核心研究问题》固化成 `research_requirements.json`。之后 Agent2/3/4/5、
Orchestrator、QualityGate 和交付检查全部读取这一份清单，与"找到了多少证据"无关。

设计取舍
--------
- 需求清单**由提纲派生**，而不是让模型另写一份 JSON。保存时记录提纲摘要，读取时
  再与项目内当前提纲比对；Agent1 重新生成提纲时清单同步重建，人工修改提纲后旧清单
  也不能继续生效。
- 每项要求的阈值（最低证据数、最低来源等级、是否要求数值）保留在文件里可被人工调整，
  但派生时使用保守默认值：必答、至少 1 条合格证据、不额外限制等级与数值。系统不去
  猜测哪个问题"应该"是数值型的，避免凭启发式规则造成误阻断。
- 缺失需求清单一律阻断交付，并给出明确的迁移命令，不做"空集合放行"或"按证据反推"。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import config
from .sources.quality import ResearchRequirement

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .state import ProjectState

# 需求清单文件的结构版本。字段含义发生不兼容变化时递增，旧文件会被显式拒绝而不是
# 按新语义解释——错误解释一份质量门输入比直接阻断更危险。
SCHEMA_VERSION: int = 1

MAX_QUESTION_TEXT = 400

_MIGRATION_HINT = (
    "请执行 `python -m research_agent migrate-plan <项目目录>` "
    "（或在工作台失败面板点击“生成研究需求清单”）从现有提纲重建需求清单；"
    "迁移完成前不会进入交付。"
)


class ResearchPlanError(RuntimeError):
    """需求清单缺失、为空、重复 ID 或格式损坏。

    这类错误一律阻断交付。`message` 必须说明用户应执行什么操作。
    """


class ResearchQuestion(BaseModel):
    """一个必须被回答的研究问题及其确定性通过条件。"""

    question_id: str
    text: str
    required: bool = True
    min_supported: int = Field(default=1, ge=1)
    min_source_tier: str | None = None
    require_numeric: bool = False

    @field_validator("question_id")
    @classmethod
    def _question_id_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question_id 不能为空")
        return cleaned

    @field_validator("text")
    @classmethod
    def _text_is_present(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("研究问题文本不能为空")
        return cleaned[:MAX_QUESTION_TEXT]

    @field_validator("min_source_tier")
    @classmethod
    def _tier_is_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if cleaned in {"", "NONE"}:
            return None
        if cleaned not in {"S", "A", "B", "D"}:
            raise ValueError(f"未知的来源等级：{value}")
        return cleaned

    def as_requirement(self) -> ResearchRequirement:
        return ResearchRequirement(
            question_id=self.question_id,
            required=self.required,
            min_supported=self.min_supported,
            require_numeric=self.require_numeric,
            min_source_tier=self.min_source_tier,
            text=self.text,
        )


class ResearchPlan(BaseModel):
    """研究启动阶段固定下来的完整需求集合。"""

    schema_version: int = SCHEMA_VERSION
    topic: str = ""
    source_outline: str
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    requirements: list[ResearchQuestion]

    @field_validator("schema_version")
    @classmethod
    def _schema_version_is_supported(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 schema_version={value}（当前实现只支持 {SCHEMA_VERSION}）"
            )
        return value

    @field_validator("source_outline")
    @classmethod
    def _source_outline_is_digest(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{16}", cleaned):
            raise ValueError("source_outline 必须是当前调研提纲的 16 位十六进制摘要")
        return cleaned

    @model_validator(mode="after")
    def _requirements_are_usable(self) -> "ResearchPlan":
        if not self.requirements:
            raise ValueError("需求清单为空：研究开始前必须至少固定一个研究问题")
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in self.requirements:
            if item.question_id in seen:
                duplicates.append(item.question_id)
            seen.add(item.question_id)
        if duplicates:
            raise ValueError(
                f"question_id 重复：{', '.join(sorted(set(duplicates)))}"
            )
        return self

    @property
    def question_ids(self) -> list[str]:
        return [item.question_id for item in self.requirements]

    @property
    def required_question_ids(self) -> list[str]:
        return [item.question_id for item in self.requirements if item.required]

    def as_requirements(self) -> list[ResearchRequirement]:
        return [item.as_requirement() for item in self.requirements]


# ═════════════════════════════════════════════════════════════════
# 持久化
# ═════════════════════════════════════════════════════════════════


def plan_path(state: "ProjectState") -> Path:
    return state.project_dir / config.FILE_RESEARCH_REQUIREMENTS


def save_plan(state: "ProjectState", plan: ResearchPlan) -> Path:
    path = plan_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_plan(state: "ProjectState") -> ResearchPlan:
    """读取并校验需求清单；任何问题都抛 `ResearchPlanError`。

    调用方不允许把异常降级为"空要求集合"——那正是 R1 要消除的静默放行路径。
    """
    path = plan_path(state)
    if not path.is_file():
        raise ResearchPlanError(
            f"项目缺少研究需求清单 {config.FILE_RESEARCH_REQUIREMENTS}："
            f"该项目创建于需求清单机制上线之前。{_MIGRATION_HINT}"
        )
    plan = _load_plan_file(path)
    _require_matching_outline(plan, state.project_dir / config.FILE_OUTLINE)
    return plan


def _load_plan_file(path: Path) -> ResearchPlan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchPlanError(
            f"研究需求清单 {config.FILE_RESEARCH_REQUIREMENTS} 无法解析：{exc}。"
            f"{_MIGRATION_HINT}"
        ) from exc
    if not isinstance(raw, dict):
        raise ResearchPlanError(
            f"研究需求清单 {config.FILE_RESEARCH_REQUIREMENTS} 必须是 JSON 对象。"
            f"{_MIGRATION_HINT}"
        )
    try:
        return ResearchPlan.model_validate(raw)
    except ValidationError as exc:
        raise ResearchPlanError(
            f"研究需求清单 {config.FILE_RESEARCH_REQUIREMENTS} 校验失败："
            f"{_format_validation_error(exc)}。{_MIGRATION_HINT}"
        ) from exc


def _require_matching_outline(plan: ResearchPlan, outline_path: Path) -> None:
    if not outline_path.is_file():
        raise ResearchPlanError(
            f"项目缺少调研提纲 {config.FILE_OUTLINE}，无法确认研究需求清单对应的研究范围。"
            f"{_MIGRATION_HINT}"
        )
    try:
        current_digest = _outline_digest(outline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ResearchPlanError(
            f"调研提纲 {config.FILE_OUTLINE} 无法读取：{exc}。{_MIGRATION_HINT}"
        ) from exc
    if plan.source_outline != current_digest:
        raise ResearchPlanError(
            f"调研提纲 {config.FILE_OUTLINE} 已在需求清单生成后发生变化，"
            f"{config.FILE_RESEARCH_REQUIREMENTS} 不再对应当前研究范围。{_MIGRATION_HINT}"
        )


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "requirements"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


# ═════════════════════════════════════════════════════════════════
# 从提纲派生
# ═════════════════════════════════════════════════════════════════

_QUESTION_HEADING = re.compile(
    r"^#{2,3}\s*.*(核心研究问题|研究问题|核心问题|Key Questions)", re.IGNORECASE
)
_ANY_HEADING = re.compile(r"^#{1,6}\s+")
_NUMBERED_ITEM = re.compile(r"^\s*(?:\d+[.)、]|[-*+])\s+(.*\S)\s*$")


def _clean_question_text(value: str) -> str:
    text = value.strip()
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text).strip(" -—:：")


def extract_question_texts(outline_text: str) -> list[str]:
    """从提纲的《核心研究问题》小节抽取问题文本。

    只认提纲里显式声明的研究问题；抽不到时返回空列表，由调用方决定回退策略。
    """
    lines = outline_text.splitlines()
    questions: list[str] = []
    inside = False
    for line in lines:
        if _QUESTION_HEADING.match(line):
            inside = True
            continue
        if inside and _ANY_HEADING.match(line):
            break
        if not inside:
            continue
        match = _NUMBERED_ITEM.match(line)
        if match:
            text = _clean_question_text(match.group(1))
            if text:
                questions.append(text)
    return questions


def derive_plan_from_outline(
    topic: str, outline_text: str
) -> tuple[ResearchPlan, str | None]:
    """把提纲固化为需求清单。返回 (plan, warning)。

    阈值使用保守默认值：必答、至少 1 条合格证据、不额外约束来源等级与数值。系统不去
    猜哪个问题"应该"是数值型的——猜错会造成无法通过的门禁；需要收紧时由人工编辑
    `research_requirements.json`，或后续阶段（R3/R4）引入更强的任务契约。
    """
    texts = extract_question_texts(outline_text)
    if not texts:
        raise ResearchPlanError(
            "调研提纲中没有可解析的《核心研究问题》有序列表，"
            "不能用章节标题或通用问题代替必答问题全集。"
            "请让 Agent1 使用 `1. 问题文本` 格式重新生成提纲后再继续。"
        )

    # 去重后编号，保证 question_id 唯一且与清单顺序一致
    seen: set[str] = set()
    unique_texts: list[str] = []
    for text in texts:
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_texts.append(text)

    requirements = [
        ResearchQuestion(question_id=f"q{index}", text=text)
        for index, text in enumerate(unique_texts, 1)
    ]
    plan = ResearchPlan(
        topic=topic,
        source_outline=_outline_digest(outline_text),
        requirements=requirements,
    )
    return plan, None


def _outline_digest(outline_text: str) -> str:
    import hashlib

    return hashlib.sha256(outline_text.encode("utf-8")).hexdigest()[:16]


# ═════════════════════════════════════════════════════════════════
# 供 Orchestrator / Agent 使用的入口
# ═════════════════════════════════════════════════════════════════


def rebuild_plan(state: "ProjectState") -> tuple[ResearchPlan, str | None]:
    """从当前提纲重建并持久化需求清单。

    Agent1 每次产出（或重新产出）提纲后调用，保证大纲与需求清单同步；
    旧项目迁移也走这条路径，不存在第二套生成逻辑。
    """
    outline = _outline_path(state)
    if outline is None:
        raise ResearchPlanError(
            "项目还没有调研提纲，无法生成研究需求清单："
            "请先运行 Agent1 生成提纲（CLI 用 resume，工作台点击“继续”）。"
        )
    plan, warning = derive_plan_from_outline(
        state.topic, outline.read_text(encoding="utf-8")
    )
    save_plan(state, plan)
    state.outline_path = str(outline)
    state.research_plan_path = str(plan_path(state))
    state.notes.pop("research_plan_migration_required", None)
    state.notes.pop("research_plan_error", None)
    state.notes["research_question_ids"] = plan.question_ids
    if warning:
        state.notes["research_plan_warning"] = warning
    else:
        state.notes.pop("research_plan_warning", None)
    state.save()
    return plan, warning


def _outline_path(state: "ProjectState") -> Path | None:
    """定位提纲文件。

    优先项目目录内的标准文件名：`state.outline_path` 存的是绝对路径，项目目录被复制
    或迁移后会指向旧位置，据此重建的清单会属于错误的项目。
    """
    candidates = [state.project_dir / config.FILE_OUTLINE]
    if state.outline_path:
        candidates.append(Path(state.outline_path))
    return next((item for item in candidates if item.is_file()), None)


def require_plan(state: "ProjectState") -> ResearchPlan:
    """读取需求清单；失败时在 state 上留下显式的迁移标记后抛出。

    标记写入 `notes` 而不是仅抛异常，是为了让 Web 工作台与 CLI status 都能显示
    "需要重新确认研究计划"，而不是只在一次请求的错误信息里出现。
    """
    try:
        plan = load_plan(state)
    except ResearchPlanError as exc:
        state.notes["research_plan_migration_required"] = True
        state.notes["research_plan_error"] = str(exc)
        state.notes["quality_gate"] = "blocked"
        state.notes["quality_gate_reasons"] = [str(exc)]
        state.save()
        raise
    changed = False
    if state.notes.pop("research_plan_migration_required", None) is not None:
        changed = True
    if state.notes.pop("research_plan_error", None) is not None:
        changed = True
    state.notes["research_question_ids"] = plan.question_ids
    if changed:
        state.save()
    return plan


def load_plan_or_none(state: "ProjectState") -> ResearchPlan | None:
    """只读场景（prompt 拼装、状态序列化）用：缺失时返回 None，不抛异常。"""
    try:
        return load_plan(state)
    except ResearchPlanError:
        return None


def known_question_ids(project_dir: Path) -> list[str] | None:
    """给工具层用的轻量读取：返回清单里的 question_id，缺失或损坏时返回 None。

    工具层不持有 `ProjectState`，只知道 project_id 对应的目录。
    """
    path = project_dir / config.FILE_RESEARCH_REQUIREMENTS
    if not path.is_file():
        return None
    try:
        plan = _load_plan_file(path)
        _require_matching_outline(plan, project_dir / config.FILE_OUTLINE)
    except ResearchPlanError:
        return None
    return plan.question_ids


def plan_prompt_context(state: "ProjectState") -> str:
    """所有 Agent 共享的需求清单说明段，保证使用同一组 question_id。"""
    plan = load_plan_or_none(state)
    if plan is None:
        return (
            "\n\n## 固定研究需求清单\n"
            f"- 本项目缺少 `{config.FILE_RESEARCH_REQUIREMENTS}`，需要先重建研究需求清单。\n"
            "- 在清单重建之前不要记录 EvidenceRecord，也不会通过交付门禁。\n"
        )
    lines = [
        "\n\n## 固定研究需求清单（question_id 是全流程唯一标识）",
        f"- 清单文件：`{plan_path(state)}`（schema_version={plan.schema_version}）",
        "- 记录 EvidenceRecord 时，`research_question_id` 必须取自下表，禁止自造 ID。",
        "- 必答问题未达到最低证据要求时，确定性质量门会阻断交付。",
        "",
        "| question_id | 研究问题 | 必答 | 最低证据数 | 最低来源等级 | 需数值 |",
        "|---|---|---|---|---|---|",
    ]
    for item in plan.requirements:
        lines.append(
            f"| `{item.question_id}` | {item.text} | "
            f"{'是' if item.required else '否'} | {item.min_supported} | "
            f"{item.min_source_tier or '不限'} | {'是' if item.require_numeric else '否'} |"
        )
    return "\n".join(lines) + "\n"
