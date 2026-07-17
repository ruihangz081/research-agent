"""Agent Loop 类型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentOptions:
    """Agent 运行配置（替代 ClaudeAgentOptions）。

    兼容任何 OpenAI Chat Completions API 的模型。
    """

    system_prompt: str = ""
    model: str = ""  # 若为空，使用 config.LLM_MODEL
    allowed_tools: list[str] = field(default_factory=list)
    cwd: str = ""  # 文件工具的工作目录
    max_turns: int = 25  # 最大 LLM 交互轮次
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = True  # 是否流式输出
