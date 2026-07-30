"""Token 用量统计：采集、按阶段聚合、按日聚合。

`LLMResponse.usage` 早就解析了，但没人聚合。这里提供：

- `UsageCollector` —— 一次 agent 调用期间累加 usage（`run_agent` 内部使用）
- `record_stage_usage()` —— 把一个阶段的用量写入 state.json 与项目日用量文件
- `aggregate()` —— 跨项目汇总，供首页展示

日粒度数据单独存 `token_usage.jsonl`（每行一天），避免 state.json 无限膨胀；
热力图需要按天回看，而 state.json 是每次保存整体重写的。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

FILE_TOKEN_USAGE = "token_usage.jsonl"

_PROMPT_KEYS = ("prompt_tokens", "input_tokens")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens")
_TOTAL_KEYS = ("total_tokens",)


def _pick(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    """兼容不同服务商的字段命名（OpenAI 用 prompt/completion，部分用 input/output）。"""
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return 0


@dataclass
class TokenUsage:
    """一段调用的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add_raw(self, usage: dict[str, Any] | None) -> None:
        """累加一次 API 返回的 usage。"""
        if not usage:
            return
        prompt = _pick(usage, _PROMPT_KEYS)
        completion = _pick(usage, _COMPLETION_KEYS)
        if not prompt and not completion:
            # 只给了 total 时归入 completion，避免整条记录丢失
            completion = _pick(usage, _TOTAL_KEYS)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.calls += 1

    def merge(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }

    @property
    def is_empty(self) -> bool:
        return self.calls == 0 and self.total_tokens == 0


class UsageCollector:
    """在一次 agent 调用期间累加 usage。"""

    def __init__(self) -> None:
        self.usage = TokenUsage()

    def observe(self, usage: dict[str, Any] | None) -> None:
        self.usage.add_raw(usage)


# ═══════════════════════════════════════════════════════════════
# 项目级持久化
# ═══════════════════════════════════════════════════════════════


def usage_path(project_dir: Path) -> Path:
    return project_dir / FILE_TOKEN_USAGE


def _stage_group(stage_name: str) -> str:
    """把 "Agent2·采集第3轮" 这类阶段名归并到 "Agent2·采集"。

    否则轮次一多，阶段明细会被同一个 Agent 的多轮记录淹没。
    """
    for separator in ("第",):
        index = stage_name.find(separator)
        if index > 0:
            return stage_name[:index].rstrip("·").rstrip()
    return stage_name


def record_stage_usage(state: Any, stage_name: str, usage: TokenUsage) -> None:
    """把一个阶段的用量累加到 state 与项目日用量文件。

    state 用 Any 标注以避免与 state 模块循环导入。
    """
    if usage.is_empty:
        return

    group = _stage_group(stage_name)
    totals = state.token_usage or {}
    totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + usage.prompt_tokens
    totals["completion_tokens"] = (
        totals.get("completion_tokens", 0) + usage.completion_tokens
    )
    totals["total_tokens"] = totals.get("total_tokens", 0) + usage.total_tokens
    totals["calls"] = totals.get("calls", 0) + usage.calls

    stages = dict(totals.get("stages") or {})
    entry = dict(stages.get(group) or {})
    entry["prompt_tokens"] = entry.get("prompt_tokens", 0) + usage.prompt_tokens
    entry["completion_tokens"] = (
        entry.get("completion_tokens", 0) + usage.completion_tokens
    )
    entry["total_tokens"] = entry.get("total_tokens", 0) + usage.total_tokens
    entry["calls"] = entry.get("calls", 0) + usage.calls
    stages[group] = entry
    totals["stages"] = stages
    state.token_usage = totals
    state.save()

    _append_daily(state.project_dir, usage)


def _append_daily(project_dir: Path, usage: TokenUsage) -> None:
    """把用量并入当天记录。同一天多次调用合并为一行。"""
    today = date.today().isoformat()
    path = usage_path(project_dir)
    rows = _read_daily_rows(path)
    row = rows.get(today) or {"date": today, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    row["prompt_tokens"] += usage.prompt_tokens
    row["completion_tokens"] += usage.completion_tokens
    row["calls"] += usage.calls
    rows[today] = row
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".jsonl.tmp")
        temp.write_text(
            "\n".join(
                json.dumps(rows[key], ensure_ascii=False) for key in sorted(rows)
            )
            + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    except OSError:
        # 统计是辅助信息，写盘失败不应中断调研
        pass


def _read_daily_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            day = str(value.get("date", ""))
            if not day:
                continue
            rows[day] = {
                "date": day,
                "prompt_tokens": int(value.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(value.get("completion_tokens", 0) or 0),
                "calls": int(value.get("calls", 0) or 0),
            }
    except OSError:
        return rows
    return rows


def read_daily(project_dir: Path) -> list[dict[str, Any]]:
    rows = _read_daily_rows(usage_path(project_dir))
    return [rows[key] for key in sorted(rows)]


# ═══════════════════════════════════════════════════════════════
# 跨项目汇总（首页展示）
# ═══════════════════════════════════════════════════════════════


HEATMAP_DAYS = 364  # 52 周 × 7 天，与首页热力图列数对齐


@dataclass
class UsageSummary:
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    peak_daily_tokens: int = 0
    peak_project_tokens: int = 0
    peak_project_topic: str = ""
    active_days: int = 0
    current_streak: int = 0
    longest_streak: int = 0
    stages: dict[str, int] = field(default_factory=dict)
    projects: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "calls": self.calls,
            "peak_daily_tokens": self.peak_daily_tokens,
            "peak_project_tokens": self.peak_project_tokens,
            "peak_project_topic": self.peak_project_topic,
            "active_days": self.active_days,
            "current_streak": self.current_streak,
            "longest_streak": self.longest_streak,
            "stages": [
                {"stage": name, "total_tokens": value}
                for name, value in sorted(
                    self.stages.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "projects": self.projects,
            "daily": self.daily,
        }


def _streaks(active: set[str], today: date) -> tuple[int, int]:
    """返回 (当前连续天数, 最长连续天数)。

    当前连续天数从今天或昨天起算——调研常跨夜运行，若今天还没开始就归零，
    展示上会显得跳变。
    """
    if not active:
        return 0, 0

    days = sorted(date.fromisoformat(value) for value in active)
    longest = 1
    run = 1
    for previous, current in zip(days, days[1:]):
        if (current - previous).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1

    anchor = today if today.isoformat() in active else today - timedelta(days=1)
    current_streak = 0
    cursor = anchor
    while cursor.isoformat() in active:
        current_streak += 1
        cursor -= timedelta(days=1)
    return current_streak, longest


def aggregate(
    project_dirs: Iterable[Path], *, days: int = HEATMAP_DAYS, today: date | None = None
) -> UsageSummary:
    """汇总多个项目的 token 用量。

    project_dirs 应为项目目录列表；缺少统计文件的项目会被安静跳过（例如本次
    功能上线前创建的老项目）。
    """
    from .state import ProjectState  # 局部导入避免循环依赖

    today = today or date.today()
    window_start = today - timedelta(days=days - 1)
    summary = UsageSummary()
    per_day: dict[str, dict[str, int]] = {}
    active: set[str] = set()

    for project_dir in project_dirs:
        try:
            state = ProjectState.load(project_dir)
        except Exception:
            continue

        totals = state.token_usage or {}
        project_total = int(totals.get("total_tokens", 0) or 0)
        summary.total_tokens += project_total
        summary.prompt_tokens += int(totals.get("prompt_tokens", 0) or 0)
        summary.completion_tokens += int(totals.get("completion_tokens", 0) or 0)
        summary.calls += int(totals.get("calls", 0) or 0)

        for name, entry in (totals.get("stages") or {}).items():
            value = int((entry or {}).get("total_tokens", 0) or 0)
            summary.stages[name] = summary.stages.get(name, 0) + value

        if project_total:
            summary.projects.append(
                {
                    "id": project_dir.name,
                    "topic": state.topic,
                    "total_tokens": project_total,
                    "calls": int(totals.get("calls", 0) or 0),
                }
            )
            if project_total > summary.peak_project_tokens:
                summary.peak_project_tokens = project_total
                summary.peak_project_topic = state.topic

        for row in read_daily(project_dir):
            day = row["date"]
            total = row["prompt_tokens"] + row["completion_tokens"]
            if total <= 0:
                continue
            active.add(day)
            try:
                parsed = date.fromisoformat(day)
            except ValueError:
                continue
            if parsed < window_start or parsed > today:
                continue
            bucket = per_day.setdefault(day, {"total_tokens": 0, "calls": 0})
            bucket["total_tokens"] += total
            bucket["calls"] += row["calls"]

    summary.projects.sort(key=lambda item: item["total_tokens"], reverse=True)
    summary.active_days = len(active)
    summary.peak_daily_tokens = max(
        (value["total_tokens"] for value in per_day.values()), default=0
    )
    summary.current_streak, summary.longest_streak = _streaks(active, today)
    summary.daily = [
        {"date": day, **per_day[day]} for day in sorted(per_day)
    ]
    return summary


# ═══════════════════════════════════════════════════════════════
# 阶段内隐式采集（避免每个 Agent 都改签名）
# ═══════════════════════════════════════════════════════════════

_current: ContextVar[UsageCollector | None] = ContextVar(
    "research_agent_usage_collector", default=None
)


def report(usage: dict[str, Any] | None) -> None:
    """把一次 LLM 调用的 usage 上报给当前阶段的采集器。

    由 `agent_loop.run_agent` / `AgentSession` 自动调用。没有活动采集器时是空操作，
    因此单元测试直接调用 agent 也不会报错。
    """
    collector = _current.get()
    if collector is not None:
        collector.observe(usage)


@contextmanager
def collect_stage(state: Any, stage_name: str):
    """在该上下文内采集 token 用量，退出时写入 state 与日用量文件。

    嵌套时内层独立采集，避免同一次调用被计两次。
    """
    collector = UsageCollector()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)
        try:
            record_stage_usage(state, stage_name, collector.usage)
        except Exception:
            # 统计失败绝不能影响调研主流程
            pass
