"""Agent1 · 战略规划（调研提纲设计师）。

多轮对话，由用户与 LLM 交替发言。
上限 5 轮（可配置）；LLM 自主决定何时收敛并写出 outline.md。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.prompt import Prompt

from .. import config
from ..agent_loop import AgentOptions, AgentSession
from ..llm import LLMClient
from ..tools import default_registry

if TYPE_CHECKING:
    from ..state import ProjectState

console = Console()

_PROMPT_FILE = Path(__file__).parent / "prompts" / "strategist.md"


def _load_system_prompt() -> str:
    return _PROMPT_FILE.read_text(encoding="utf-8")


async def run_strategist(state: "ProjectState", feedback: str | None = None) -> Path:
    """运行 Agent1 多轮对话，最终写出 outline.md。"""
    outline_path = state.project_dir / config.FILE_OUTLINE
    project_dir = state.project_dir
    project_dir.mkdir(parents=True, exist_ok=True)

    system_prompt = _load_system_prompt()
    system_prompt += (
        f"\n\n## 当前调研项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 提纲写入路径（必须严格使用）：`{outline_path}`\n"
        f"- 对话轮次上限：{config.STRATEGIST_MAX_ROUNDS} 轮\n"
    )
    if feedback:
        system_prompt += (
            f"\n## 重要：用户驳回了上一版提纲\n"
            f"修改意见：{feedback}\n"
            f"请在本轮对话中针对此意见重点澄清，然后重新生成提纲。\n"
        )

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write"],
        cwd=str(project_dir),
        max_turns=40,
    )

    console.print(
        f"\n[bold magenta]═══ Agent1 · 战略规划启动 ═══[/bold magenta]\n"
        f"[dim]调研主题：{state.topic}[/dim]\n"
        f"[dim]对话上限：{config.STRATEGIST_MAX_ROUNDS} 轮（Agent 可自主提前收敛）[/dim]\n"
    )

    first_prompt = (
        f"我要做一次针对「{state.topic}」的行业调研。\n"
        f"请按你的工作流引导我明确调研细节，并最终生成提纲到 `{outline_path}`。"
    )
    if feedback:
        first_prompt += f"\n\n【上一版提纲被驳回，意见】{feedback}"

    async def _on_text(text: str) -> None:
        console.print(text, style="bright_cyan", end="")

    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        session = AgentSession(options, client, default_registry, _on_text)

        await session.query(first_prompt)

        for round_idx in range(1, config.STRATEGIST_MAX_ROUNDS + 1):
            console.print(f"\n[dim]── 第 {round_idx} 轮 ──[/dim]")
            response_text = await session.get_response()
            console.print()  # 流式输出后换行

            # 如果 LLM 已生成 outline 文件，视为收敛
            if outline_path.exists():
                console.print(
                    f"\n[green]✓ Agent1 已生成调研提纲：{outline_path.name}[/green]"
                )
                return outline_path

            if round_idx == config.STRATEGIST_MAX_ROUNDS:
                console.print(
                    "\n[yellow]已达轮次上限，要求 Agent1 立即收敛输出提纲。[/yellow]"
                )
                await session.query(
                    "已达到本次对话的轮次上限。请立即基于当前信息生成调研提纲并写入指定路径，"
                    "对于不确定的维度使用合理默认值并标注『（默认值，可调整）』。"
                )
                await session.get_response()
                console.print()
                break

            # 用户输入下一轮
            user_input = Prompt.ask(
                "\n[bold yellow]你[/bold yellow]",
                default="（直接回车表示：按你的建议继续）",
            )
            if user_input.strip() in ("", "（直接回车表示：按你的建议继续）"):
                user_input = "按你的建议继续。如信息已足够，请直接输出提纲。"
            await session.query(user_input)

    if not outline_path.exists():
        raise RuntimeError(
            "Agent1 在轮次上限内未能生成提纲。请检查 prompt 或手动创建 outline。"
        )
    return outline_path
