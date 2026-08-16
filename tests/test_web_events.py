"""SSE 事件流与订阅机制测试：替代前端全量轮询的增量推送。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from research_agent import config, web_app
from research_agent.state import ProjectState


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    state = ProjectState(topic="SSE 测试项目", date_str="20260101_120000")
    state.save()
    return state.project_dir.name


@pytest.mark.anyio
async def test_notify_subscribers_pushes_update(project: str) -> None:
    """_notify_subscribers 向已订阅队列投递 update 事件。"""
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    web_app.SUBSCRIBERS[project] = {queue}
    try:
        web_app._notify_subscribers(project)
        assert await queue.get() == "update"
    finally:
        web_app.SUBSCRIBERS.pop(project, None)


@pytest.mark.anyio
async def test_notify_subscribers_replaces_stale_update(project: str) -> None:
    """队列容量为 1：消费者未消费时，用最新事件替换旧事件，避免堆积。"""
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    web_app.SUBSCRIBERS[project] = {queue}
    try:
        web_app._notify_subscribers(project)  # 填满队列
        web_app._notify_subscribers(project)  # 再投：替换而非堆积
        assert queue.qsize() == 1
        assert await queue.get() == "update"
        # 队列已空，确认没有第二条残留
        assert queue.empty()
    finally:
        web_app.SUBSCRIBERS.pop(project, None)


@pytest.mark.anyio
async def test_log_triggers_notification(project: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_log 写入日志后应触发订阅者通知。"""
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    web_app.SUBSCRIBERS[project] = {queue}
    notified: list[str] = []
    original = web_app._notify_subscribers

    def spy(pid: str) -> None:
        notified.append(pid)
        original(pid)

    monkeypatch.setattr(web_app, "_notify_subscribers", spy)
    try:
        web_app._log(project, "测试日志")
        assert notified == [project]
        assert await queue.get() == "update"
    finally:
        web_app.SUBSCRIBERS.pop(project, None)


def test_events_route_registered() -> None:
    """SSE 端点在应用路由表中存在。"""
    routes = [getattr(route, "path", None) for route in web_app.app.routes]
    assert "/api/projects/{project_id}/events" in routes
