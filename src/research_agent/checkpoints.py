"""人机确认检查点（CLI 交互）。

三个硬性检查点：
- 调研提纲（outline.md）
- 信息源分层清单（sources_draft.md）
- 最终数据源清单（sources_final.md）
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()


def _render_file(path: Path, title: str) -> None:
    if not path.exists():
        console.print(f"[red]文件不存在：{path}[/red]")
        return
    content = path.read_text(encoding="utf-8")
    console.print(
        Panel(
            Markdown(content),
            title=title,
            border_style="cyan",
            padding=(1, 2),
        )
    )


def ask_approval(
    file_path: Path,
    title: str,
    *,
    allow_edit_hint: bool = True,
) -> tuple[bool, str]:
    """展示待审阅文件 → 询问用户是否通过。

    返回
    ----
    (approved, feedback)
        approved: True 表示通过，可进入下一阶段
        feedback: 未通过时的修改意见（字符串），供下游 agent 重跑使用
    """
    _render_file(file_path, title)
    if allow_edit_hint:
        console.print(
            f"\n[dim]提示：如需直接修改，可编辑 {file_path}，保存后再回到这里选择。[/dim]"
        )

    choice = Prompt.ask(
        "\n[bold]请选择[/bold]",
        choices=["approve", "reject", "view"],
        default="approve",
    )

    if choice == "view":
        # 再看一次
        _render_file(file_path, title)
        return ask_approval(file_path, title, allow_edit_hint=False)

    if choice == "approve":
        console.print("[green]✓ 已通过，继续下一阶段[/green]")
        return True, ""

    feedback = Prompt.ask(
        "\n[yellow]请描述需要修改的地方（一行）[/yellow]",
        default="请重新优化",
    )
    console.print("[yellow]✗ 已驳回，稍后会回到对应阶段重跑[/yellow]")
    return False, feedback
