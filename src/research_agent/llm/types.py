"""LLM 消息与响应的数据类型。

遵循 OpenAI Chat Completions API 格式，
所有支持该协议的模型（OpenAI/DeepSeek/Qwen/Ollama/vLLM 等）均可使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class FunctionCallInfo:
    """函数调用信息（tool_call 内部）。"""

    name: str = ""
    arguments: str = ""  # JSON 字符串


@dataclass
class ToolCallInfo:
    """单个 tool_call。"""

    id: str = ""
    type: str = "function"
    function: FunctionCallInfo = field(default_factory=FunctionCallInfo)


@dataclass
class ChatMessage:
    """OpenAI Chat Completions 消息格式。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCallInfo] | None = None  # 仅 assistant
    tool_call_id: str | None = None  # 仅 tool
    name: str | None = None  # 仅 tool

    def to_dict(self) -> dict[str, Any]:
        """转为 API 请求体格式。"""
        d: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        return d


@dataclass
class ToolDefinition:
    """OpenAI function calling 工具定义。"""

    type: str = "function"
    function: dict[str, Any] = field(default_factory=dict)
    # function = {"name": ..., "description": ..., "parameters": {...}}

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "function": self.function}


@dataclass
class LLMResponse:
    """解析后的 LLM 响应。"""

    content: str | None = None
    tool_calls: list[ToolCallInfo] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class StreamChunk:
    """SSE 流式响应的单个 chunk。"""

    delta_content: str | None = None
    delta_tool_calls: list[dict[str, Any]] | None = None  # 原始 delta
    finish_reason: str | None = None
    # 开启 stream_options.include_usage 后，最后一个 chunk 携带用量统计
    usage: dict[str, int] | None = None
