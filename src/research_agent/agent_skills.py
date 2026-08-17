"""Small, allow-listed loader for project-local Agent skills."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import config

_SAFE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ALLOWED_SKILLS = {"brokerage-report-formatting"}
_FORMATTER_REFERENCES = (
    "chart-rules.md",
    "quality-checklist.md",
)


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
            self.instructions,
        ]
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


def load_project_skill(name: str) -> ProjectSkill:
    """Load one explicitly allow-listed skill from the repository skills folder."""
    if not _SAFE_NAME.fullmatch(name) or name not in _ALLOWED_SKILLS:
        raise ValueError(f"未允许的项目 Skill：{name}")

    skills_root = config.PROJECT_SKILLS_DIR.resolve()
    root = (skills_root / name).resolve()
    if root.parent != skills_root:
        raise ValueError("Skill 路径越界")

    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        raise FileNotFoundError(f"Skill 文件不存在：{skill_path}")

    references: list[tuple[str, str]] = []
    for filename in _FORMATTER_REFERENCES:
        path = root / "references" / filename
        if not path.is_file():
            raise FileNotFoundError(f"Skill reference 不存在：{path}")
        references.append((filename, path.read_text(encoding="utf-8").strip()))

    return ProjectSkill(
        name=name,
        root=root,
        instructions=_read_skill_body(skill_path),
        references=tuple(references),
    )
