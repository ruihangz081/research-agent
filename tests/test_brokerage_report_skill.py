from pathlib import Path

import pytest

from research_agent import config
from research_agent.agent_skills import load_project_skill


def test_brokerage_skill_loads_all_required_references() -> None:
    skill = load_project_skill("brokerage-report-formatting")
    assert skill.root == config.PROJECT_SKILLS_DIR / "brokerage-report-formatting"
    assert len(skill.references) == 5
    context = skill.prompt_context()
    assert "chart-rules.md" in context
    assert "Delivery Checklist" in context
    assert "{{chart:<id>}}" in context


@pytest.mark.parametrize("name", ["../brokerage-report-formatting", "unknown", "/tmp/skill"])
def test_project_skill_loader_rejects_non_allowlisted_paths(name: str) -> None:
    with pytest.raises(ValueError):
        load_project_skill(name)


def test_brokerage_skill_assets_are_complete() -> None:
    assets = load_project_skill("brokerage-report-formatting").assets_dir
    assert {path.name for path in assets.iterdir()} == {
        "brokerage-report-tables.lua",
        "brokerage-report.sty",
        "brokerage-report.tex",
        "theme.json",
    }
