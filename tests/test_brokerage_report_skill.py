from pathlib import Path

import pytest

from research_agent import config
from research_agent.agent_skills import load_project_skill


def test_brokerage_skill_loads_only_chart_specific_references() -> None:
    skill = load_project_skill("brokerage-report-formatting")
    assert skill.root == config.PROJECT_SKILLS_DIR / "brokerage-report-formatting"
    assert [name for name, _content in skill.references] == [
        "chart-rules.md",
        "quality-checklist.md",
    ]
    context = skill.prompt_context()
    assert "chart-rules.md" in context
    assert "Delivery Checklist" in context
    assert "{{chart:<id>}}" in context


def test_analyst_role_loads_writing_references_not_chart_rules() -> None:
    """Agent4 是唯一撰写正文的 Agent，写作规范必须注入它而不是 Agent5。

    Agent5 只输出图表清单、正文被逐字复制，所以给它注入表格与中文行文规范纯属
    白烧 token；反过来，这些规范如果谁都不注入，就等于整条流水线丢失了排版约束。
    """
    skill = load_project_skill("brokerage-report-formatting", role="analyst")
    assert [name for name, _content in skill.references] == [
        "report-structure.md",
        "table-rules.md",
        "china-style.md",
    ]
    context = skill.prompt_context()
    assert "Report Structure" in context
    assert "Table Rules" in context
    assert "Chinese Brokerage Style" in context
    # 图表清单 schema 只对 Agent5 有意义
    assert "chart-rules.md" not in context


def test_every_skill_reference_is_injected_into_exactly_one_role() -> None:
    """references/ 下的每份文件都必须被某个角色注入，且不重复注入。

    这条测试防止再次出现"文件还在、SKILL.md 也提到，但没有任何 Agent 收到"的
    孤儿规范——上一版有三份 reference 处于该状态。
    """
    from research_agent.agent_skills import _SKILL_REFERENCES

    root = config.PROJECT_SKILLS_DIR / "brokerage-report-formatting" / "references"
    on_disk = {path.name for path in root.iterdir() if path.suffix == ".md"}
    injected = [name for names in _SKILL_REFERENCES.values() for name in names]

    assert set(injected) == on_disk, "存在未被任何角色注入的 Skill reference"
    assert len(injected) == len(set(injected)), "同一份 reference 被多个角色重复注入"


def test_project_skill_loader_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="未知的 Skill 角色"):
        load_project_skill("brokerage-report-formatting", role="collector")


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
        "design-tokens.json",
    }
