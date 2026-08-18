"""Small, allow-listed loader for project-local Agent skills."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import config

_SAFE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ALLOWED_SKILLS = {"brokerage-report-formatting"}


@dataclass(frozen=True)
class _SkillRole:
    """一个角色能看到 Skill 的哪些部分。

    `SKILL.md` 正文是 Agent5 的输出契约（"只写 05_chart_manifest.json"、
    `placement_after` 规则等）。它对 Agent4 不仅无用还会主动误导——Agent4 是
    唯一撰写正文的 Agent，绝不能被告知"只写图表清单"。所以正文是否注入按角色区分。
    """

    include_instructions: bool
    references: tuple[str, ...]


#: 按角色划分 Skill：Agent5 只出图表清单，正文由 Agent4 撰写后被逐字复制，
#: 因此结构、表格与中文行文规范必须注入 Agent4，图表规范注入 Agent5。
#: 两组不重叠——给 Agent5 注入表格规范只是白烧 token（它不写正文），给 Agent4 注入
#: 图表清单 schema 也没用（它不写清单）。
_SKILL_ROLES: dict[str, _SkillRole] = {
    "formatter": _SkillRole(
        include_instructions=True,
        references=("chart-rules.md", "quality-checklist.md"),
    ),
    "analyst": _SkillRole(
        include_instructions=False,
        references=("report-structure.md", "table-rules.md", "china-style.md"),
    ),
}

#: 供测试断言"每份 reference 都被注入且不重复"使用。
_SKILL_REFERENCES: dict[str, tuple[str, ...]] = {
    role: spec.references for role, spec in _SKILL_ROLES.items()
}


@dataclass(frozen=True)
class ProjectSkill:
    name: str
    root: Path
    instructions: str
    references: tuple[tuple[str, str], ...]

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    def prompt_context(self) -> str:
        parts = [
            "\n\n# 已加载项目 Skill",
            f"Skill: `{self.name}`",
        ]
        if self.instructions:
            parts.append(self.instructions)
        for filename, content in self.references:
            parts.extend((f"\n## Skill reference: {filename}", content))
        return "\n".join(parts).strip() + "\n"


def _read_skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end < 0:
            raise ValueError(f"Skill frontmatter 未闭合：{path}")
        text = text[end + 5 :]
    return text.strip()


def load_project_skill(name: str, role: str = "formatter") -> ProjectSkill:
    """Load one explicitly allow-listed skill, injecting only what `role` needs.

    See `_SKILL_ROLES`: the manifest contract in SKILL.md goes to the formatter,
    the prose-writing references go to the analyst.
    """
    if not _SAFE_NAME.fullmatch(name) or name not in _ALLOWED_SKILLS:
        raise ValueError(f"未允许的项目 Skill：{name}")
    spec = _SKILL_ROLES.get(role)
    if spec is None:
        raise ValueError(f"未知的 Skill 角色：{role}")

    skills_root = config.PROJECT_SKILLS_DIR.resolve()
    root = (skills_root / name).resolve()
    if root.parent != skills_root:
        raise ValueError("Skill 路径越界")

    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        raise FileNotFoundError(f"Skill 文件不存在：{skill_path}")

    references: list[tuple[str, str]] = []
    for filename in spec.references:
        path = root / "references" / filename
        if not path.is_file():
            raise FileNotFoundError(f"Skill reference 不存在：{path}")
        references.append((filename, path.read_text(encoding="utf-8").strip()))

    return ProjectSkill(
        name=name,
        root=root,
        instructions=_read_skill_body(skill_path) if spec.include_instructions else "",
        references=tuple(references),
    )
