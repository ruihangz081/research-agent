"""Token 用量统计测试（backlog 第 5 项）。"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from research_agent import config, token_usage
from research_agent.state import ProjectState
from research_agent.token_usage import TokenUsage, aggregate, read_daily


@pytest.fixture
def projects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    return tmp_path


def test_usage_accumulates_openai_field_names() -> None:
    usage = TokenUsage()
    usage.add_raw({"prompt_tokens": 100, "completion_tokens": 40})
    usage.add_raw({"prompt_tokens": 10, "completion_tokens": 5})

    assert usage.as_dict() == {
        "prompt_tokens": 110,
        "completion_tokens": 45,
        "total_tokens": 155,
        "calls": 2,
    }


def test_usage_accepts_alternate_field_names() -> None:
    """部分服务商用 input_tokens / output_tokens。"""
    usage = TokenUsage()
    usage.add_raw({"input_tokens": 70, "output_tokens": 30})

    assert usage.prompt_tokens == 70
    assert usage.completion_tokens == 30


def test_total_only_usage_is_not_lost() -> None:
    """只给 total_tokens 时也要计入，否则整条记录会丢。"""
    usage = TokenUsage()
    usage.add_raw({"total_tokens": 90})

    assert usage.total_tokens == 90
    assert usage.calls == 1


def test_none_and_empty_usage_are_ignored() -> None:
    usage = TokenUsage()
    usage.add_raw(None)
    usage.add_raw({})

    assert usage.is_empty is True
    assert usage.calls == 0


def test_record_stage_usage_groups_rounds(projects_dir: Path) -> None:
    """采集第 1/2/3 轮应归并为同一个阶段，否则明细被轮次淹没。"""
    state = ProjectState(topic="分组", date_str="20260728")
    state.save()

    for round_index in (1, 2, 3):
        usage = TokenUsage()
        usage.add_raw({"prompt_tokens": 100, "completion_tokens": 20})
        token_usage.record_stage_usage(state, f"Agent2·采集第{round_index}轮", usage)

    stages = ProjectState.load(state.project_dir).token_usage["stages"]
    assert list(stages) == ["Agent2·采集"]
    assert stages["Agent2·采集"]["total_tokens"] == 360
    assert stages["Agent2·采集"]["calls"] == 3


def test_record_stage_usage_persists_totals(projects_dir: Path) -> None:
    state = ProjectState(topic="累计", date_str="20260728")
    state.save()
    usage = TokenUsage()
    usage.add_raw({"prompt_tokens": 500, "completion_tokens": 100})

    token_usage.record_stage_usage(state, "Agent4·深度分析", usage)

    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.token_usage["total_tokens"] == 600
    assert reloaded.token_usage["prompt_tokens"] == 500
    assert read_daily(state.project_dir)[0]["prompt_tokens"] == 500


def test_empty_usage_is_not_recorded(projects_dir: Path) -> None:
    state = ProjectState(topic="空用量", date_str="20260728")
    state.save()

    token_usage.record_stage_usage(state, "Agent1·战略规划", TokenUsage())

    assert ProjectState.load(state.project_dir).token_usage == {}
    assert read_daily(state.project_dir) == []


def test_daily_rows_merge_same_day(projects_dir: Path) -> None:
    state = ProjectState(topic="同日合并", date_str="20260728")
    state.save()

    for _ in range(3):
        usage = TokenUsage()
        usage.add_raw({"prompt_tokens": 10, "completion_tokens": 5})
        token_usage.record_stage_usage(state, "Agent2·采集第1轮", usage)

    rows = read_daily(state.project_dir)
    assert len(rows) == 1
    assert rows[0]["calls"] == 3
    assert rows[0]["prompt_tokens"] == 30


def _seed(project_dir: Path, rows: list[tuple[str, int, int]]) -> None:
    """直接写日用量文件，便于构造跨日期场景。"""
    lines = [
        f'{{"date": "{day}", "prompt_tokens": {prompt}, "completion_tokens": {completion}, "calls": 1}}'
        for day, prompt, completion in rows
    ]
    project_dir.mkdir(parents=True, exist_ok=True)
    token_usage.usage_path(project_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_aggregate_sums_across_projects(projects_dir: Path) -> None:
    first = ProjectState(topic="项目甲", date_str="20260728")
    first.token_usage = {
        "prompt_tokens": 800,
        "completion_tokens": 200,
        "total_tokens": 1000,
        "calls": 4,
        "stages": {"Agent2·采集": {"total_tokens": 700, "calls": 3}},
    }
    first.save()
    second = ProjectState(topic="项目乙", date_str="20260728")
    second.token_usage = {
        "prompt_tokens": 300,
        "completion_tokens": 100,
        "total_tokens": 400,
        "calls": 2,
        "stages": {"Agent2·采集": {"total_tokens": 200, "calls": 1}},
    }
    second.save()

    summary = aggregate([first.project_dir, second.project_dir])

    assert summary.total_tokens == 1400
    assert summary.calls == 6
    assert summary.stages["Agent2·采集"] == 900
    assert summary.peak_project_topic == "项目甲"
    assert summary.peak_project_tokens == 1000
    assert [item["topic"] for item in summary.projects] == ["项目甲", "项目乙"]


def test_aggregate_skips_projects_without_usage(projects_dir: Path) -> None:
    """上线前的老项目没有统计数据，应安静跳过而不是报错。"""
    legacy = ProjectState(topic="老项目", date_str="20260701")
    legacy.save()

    summary = aggregate([legacy.project_dir])

    assert summary.total_tokens == 0
    assert summary.projects == []
    assert summary.daily == []


def test_aggregate_ignores_unreadable_project_dir(projects_dir: Path) -> None:
    summary = aggregate([projects_dir / "not-a-project"])
    assert summary.total_tokens == 0


def test_peak_daily_uses_cross_project_sum(projects_dir: Path) -> None:
    """同一天多个项目的消耗要合并后再取峰值。"""
    today = date(2026, 7, 28)
    first = ProjectState(topic="甲", date_str="20260728")
    first.save()
    _seed(first.project_dir, [(today.isoformat(), 600, 100)])
    second = ProjectState(topic="乙", date_str="20260728")
    second.save()
    _seed(second.project_dir, [(today.isoformat(), 300, 50)])

    summary = aggregate([first.project_dir, second.project_dir], today=today)

    assert summary.peak_daily_tokens == 1050
    assert summary.daily[0]["total_tokens"] == 1050


def test_streaks_count_consecutive_days(projects_dir: Path) -> None:
    today = date(2026, 7, 28)
    state = ProjectState(topic="连续", date_str="20260728")
    state.save()
    _seed(
        state.project_dir,
        [
            ((today - timedelta(days=index)).isoformat(), 100, 10)
            for index in range(3)
        ]
        + [((today - timedelta(days=10)).isoformat(), 100, 10)],
    )

    summary = aggregate([state.project_dir], today=today)

    assert summary.current_streak == 3
    assert summary.longest_streak == 3
    assert summary.active_days == 4


def test_current_streak_tolerates_missing_today(projects_dir: Path) -> None:
    """调研常跨夜运行；今天还没开始不应让连续天数归零。"""
    today = date(2026, 7, 28)
    state = ProjectState(topic="跨夜", date_str="20260728")
    state.save()
    _seed(
        state.project_dir,
        [
            ((today - timedelta(days=1)).isoformat(), 100, 10),
            ((today - timedelta(days=2)).isoformat(), 100, 10),
        ],
    )

    summary = aggregate([state.project_dir], today=today)

    assert summary.current_streak == 2


def test_daily_window_excludes_old_rows(projects_dir: Path) -> None:
    """热力图窗口外的历史数据不进 daily，但仍计入总量与活跃天数。"""
    today = date(2026, 7, 28)
    state = ProjectState(topic="窗口", date_str="20260728")
    state.token_usage = {"total_tokens": 500, "prompt_tokens": 400, "completion_tokens": 100, "calls": 2}
    state.save()
    _seed(
        state.project_dir,
        [
            ((today - timedelta(days=400)).isoformat(), 100, 20),
            (today.isoformat(), 300, 80),
        ],
    )

    summary = aggregate([state.project_dir], days=364, today=today)

    assert [row["date"] for row in summary.daily] == [today.isoformat()]
    assert summary.active_days == 2
    assert summary.total_tokens == 500


def test_corrupted_daily_lines_are_skipped(projects_dir: Path) -> None:
    state = ProjectState(topic="损坏行", date_str="20260728")
    state.save()
    path = token_usage.usage_path(state.project_dir)
    state.project_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"date": "2026-07-28", "prompt_tokens": 10, "completion_tokens": 2, "calls": 1}\n'
        '{"date": "broken"\n',
        encoding="utf-8",
    )

    rows = read_daily(state.project_dir)

    assert [row["date"] for row in rows] == ["2026-07-28"]


@pytest.mark.anyio
async def test_collect_stage_records_reported_usage(projects_dir: Path) -> None:
    """collect_stage 上下文内的 report() 会被归集到该阶段。"""
    state = ProjectState(topic="上下文采集", date_str="20260728")
    state.save()

    with token_usage.collect_stage(state, "Agent3·验证第1轮"):
        token_usage.report({"prompt_tokens": 200, "completion_tokens": 50})
        token_usage.report({"prompt_tokens": 100, "completion_tokens": 25})

    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.token_usage["total_tokens"] == 375
    assert "Agent3·验证" in reloaded.token_usage["stages"]


def test_report_outside_context_is_noop() -> None:
    """没有活动采集器时上报不应报错（单测直接调 agent 的场景）。"""
    token_usage.report({"prompt_tokens": 10, "completion_tokens": 1})


@pytest.mark.anyio
async def test_collect_stage_records_even_when_body_raises(projects_dir: Path) -> None:
    """阶段失败也要记账——失败的调用同样花了钱。"""
    state = ProjectState(topic="失败仍计费", date_str="20260728")
    state.save()

    with pytest.raises(RuntimeError):
        with token_usage.collect_stage(state, "Agent2·采集第1轮"):
            token_usage.report({"prompt_tokens": 400, "completion_tokens": 100})
            raise RuntimeError("boom")

    assert ProjectState.load(state.project_dir).token_usage["total_tokens"] == 500


@pytest.mark.anyio
async def test_safe_run_records_usage_per_stage(projects_dir: Path) -> None:
    """经由 _safe_run 的阶段自动记账，Agent 无需改签名。"""
    from research_agent import orchestrator

    state = ProjectState(topic="端到端", date_str="20260728")
    state.save()

    async def fake_agent() -> str:
        token_usage.report({"prompt_tokens": 1000, "completion_tokens": 200})
        return "done"

    await orchestrator._safe_run("Agent5·排版交付", state, fake_agent)

    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.token_usage["stages"]["Agent5·排版交付"]["total_tokens"] == 1200
