"""Project state persistence tests using real dependencies."""
from pathlib import Path

import pytest

from research_agent import config
from research_agent.state import ProjectState, Stage


@pytest.fixture
def projects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    return tmp_path


def test_create_save_and_load(projects_dir: Path) -> None:
    state = ProjectState(topic="测试行业", date_str="20260423")
    assert state.stage == Stage.INIT
    state.save()
    loaded = ProjectState.load(state.project_dir)
    assert loaded.topic == state.topic
    assert loaded.stage == Stage.INIT


def test_stage_advances_and_persists(projects_dir: Path) -> None:
    state = ProjectState(topic="状态测试", date_str="20260423")
    state.advance_to(Stage.PLANNING)
    assert ProjectState.load(state.project_dir).stage == Stage.PLANNING


def test_project_directory_stays_under_configured_root(projects_dir: Path) -> None:
    state = ProjectState(topic="路径测试", date_str="20260423")
    assert state.project_dir.resolve().is_relative_to(projects_dir.resolve())
