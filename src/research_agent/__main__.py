"""CLI 入口。

用法
----
    # 启动新调研
    python -m research_agent new "新能源汽车行业"

    # 断点续跑（传入项目目录路径）
    python -m research_agent resume projects/新能源汽车行业_20260423

    # 查看当前状态
    python -m research_agent status projects/新能源汽车行业_20260423
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import anyio
from rich.console import Console

from . import config, orchestrator
from .orchestrator import PipelineError
from .state import ProjectState
from .sources.cli import configure_parser as configure_source_parser

console = Console()


def _cmd_new(args: argparse.Namespace) -> int:
    topic = args.topic.strip()
    if not topic:
        console.print("[red]主题不能为空[/red]")
        return 2

    state = ProjectState(
        topic=topic,
        date_str=datetime.now().strftime("%Y%m%d"),
    )
    state.project_dir.mkdir(parents=True, exist_ok=True)
    state.save()

    console.print(
        f"\n[bold green]✓ 新调研项目已创建[/bold green]\n"
        f"[dim]主题：{topic}[/dim]\n"
        f"[dim]目录：{state.project_dir}[/dim]\n"
    )

    anyio.run(orchestrator.run_pipeline, state)
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.exists():
        console.print(f"[red]项目目录不存在：{project_dir}[/red]")
        return 2

    try:
        state = ProjectState.load(project_dir)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    console.print(
        f"\n[bold cyan]▶ 续跑调研项目[/bold cyan]\n"
        f"[dim]主题：{state.topic}[/dim]\n"
        f"[dim]当前阶段：{state.stage.value}[/dim]\n"
    )

    if orchestrator.recover_blocked_delivery(state):
        console.print(
            "[yellow]检测到旧的无证据交付状态；"
            "已回到采集验证阶段。[/yellow]\n"
        )

    anyio.run(orchestrator.run_pipeline, state)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    try:
        state = ProjectState.load(project_dir)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    console.print(
        f"\n[bold]项目状态[/bold]\n"
        f"  主题：{state.topic}\n"
        f"  创建于：{state.created_at}\n"
        f"  最后更新：{state.updated_at}\n"
        f"  当前阶段：[cyan]{state.stage.value}[/cyan]\n"
        f"  提纲：{state.outline_path or '—'}\n"
        f"  源草稿：{state.sources_draft_path or '—'}\n"
        f"  源终稿：{state.sources_final_path or '—'}\n"
        f"  分析：{state.analysis_path or '—'}\n"
        f"  终稿：{state.final_report_path or '—'}\n"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-agent",
        description="通用行业调研 Multi-Agent（基于 Claude Agent SDK）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    configure_source_parser(sub)

    p_new = sub.add_parser("new", help="启动一次新调研")
    p_new.add_argument("topic", type=str, help="调研主题，例如 '新能源汽车行业'")
    p_new.set_defaults(func=_cmd_new)

    p_resume = sub.add_parser("resume", help="对现有项目断点续跑")
    p_resume.add_argument("project_dir", type=str, help="项目目录路径")
    p_resume.set_defaults(func=_cmd_resume)

    p_status = sub.add_parser("status", help="查看项目当前状态")
    p_status.add_argument("project_dir", type=str, help="项目目录路径")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # 确保 projects 根目录存在
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。状态已保存，可用 resume 续跑。[/yellow]")
        return 130
    except PipelineError as e:
        console.print(f"\n[red]{e}[/red]")
        return 1
    except Exception as e:
        console.print(f"\n[red]未预期的错误：{e}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
