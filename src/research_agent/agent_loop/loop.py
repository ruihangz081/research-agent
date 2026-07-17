"""Agent Loop 核心实现。

run_agent()     — 单次执行（collector/validator/analyst/formatter 用）
AgentSession    — 多轮对话会话（strategist 用）

内部循环：prompt → LLM → tool_calls → 执行 → 回传 → 直到无 tool_call 或达到 max_turns。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from ..llm.client import LLMClient
from ..llm.types import (
    ChatMessage,
    FunctionCallInfo,
    LLMResponse,
    ToolCallInfo,
)
from ..tools.registry import ToolRegistry
from .types import AgentOptions

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 单次执行
# ═══════════════════════════════════════════════════════════════


async def run_agent(
    user_prompt: str,
    options: AgentOptions,
    llm_client: LLMClient,
    tool_registry: ToolRegistry,
    on_assistant_text: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """运行单次 agent 任务至完成。

    循环：user_prompt → LLM → [tool_calls → 执行 → 回传]* → 最终文本

    返回最终的 assistant 文本响应。
    """
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=options.system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]
    tool_schemas = tool_registry.get_schemas(options.allowed_tools or None)

    for turn in range(options.max_turns):
        # 1. 调用 LLM
        if options.stream and on_assistant_text:
            response = await _stream_call(
                llm_client, messages, tool_schemas, on_assistant_text,
                temperature=options.temperature,
                max_tokens=options.max_tokens,
            )
        else:
            response = await llm_client.chat(
                messages,
                tools=tool_schemas or None,
                temperature=options.temperature,
                max_tokens=options.max_tokens,
            )

        # 2. 追加 assistant 消息
        assistant_msg = ChatMessage(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
        )
        messages.append(assistant_msg)

        # 3. 无 tool_call → 完成
        if not response.tool_calls:
            return response.content or ""

        # 4. 执行每个 tool_call
        for tc in response.tool_calls:
            result = await _execute_tool_call(tc, tool_registry, options.cwd)
            messages.append(ChatMessage(
                role="tool",
                content=result,
                tool_call_id=tc.id,
                name=tc.function.name,
            ))

        logger.debug("Turn %d: executed %d tool calls", turn + 1, len(response.tool_calls))

    # 达到 max_turns，返回最后一条 assistant 文本
    for msg in reversed(messages):
        if msg.role == "assistant" and msg.content:
            return msg.content
    return ""


# ═══════════════════════════════════════════════════════════════
# 多轮对话会话
# ═══════════════════════════════════════════════════════════════


class AgentSession:
    """多轮对话会话（用于 Agent1 战略规划的 human-in-the-loop 对话）。

    用法：
        session = AgentSession(options, client, registry, on_text)
        await session.query("用户消息")
        response = await session.get_response()  # 执行工具循环直到返回文本
        # ... 用户看到 response，输入下一轮 ...
        await session.query("用户下一轮输入")
        response = await session.get_response()
    """

    def __init__(
        self,
        options: AgentOptions,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        on_assistant_text: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.options = options
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.on_assistant_text = on_assistant_text
        self.messages: list[ChatMessage] = [
            ChatMessage(role="system", content=options.system_prompt),
        ]
        self._tool_schemas = tool_registry.get_schemas(options.allowed_tools or None)
        self._turns_used = 0

    async def query(self, user_input: str) -> None:
        """添加用户消息到历史。"""
        self.messages.append(ChatMessage(role="user", content=user_input))

    async def get_response(self) -> str:
        """获取 LLM 响应，执行工具调用循环直到产出最终文本。

        返回本轮的 assistant 文本。
        """
        for _ in range(self.options.max_turns):
            if self.options.stream and self.on_assistant_text:
                response = await _stream_call(
                    self.llm_client, self.messages,
                    self._tool_schemas, self.on_assistant_text,
                    temperature=self.options.temperature,
                    max_tokens=self.options.max_tokens,
                )
            else:
                response = await self.llm_client.chat(
                    self.messages,
                    tools=self._tool_schemas or None,
                    temperature=self.options.temperature,
                    max_tokens=self.options.max_tokens,
                )

            assistant_msg = ChatMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
            self.messages.append(assistant_msg)
            self._turns_used += 1

            if not response.tool_calls:
                return response.content or ""

            for tc in response.tool_calls:
                result = await _execute_tool_call(
                    tc, self.tool_registry, self.options.cwd
                )
                self.messages.append(ChatMessage(
                    role="tool",
                    content=result,
                    tool_call_id=tc.id,
                    name=tc.function.name,
                ))

        return ""


# ═══════════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════════


async def _execute_tool_call(
    tc: ToolCallInfo,
    registry: ToolRegistry,
    cwd: str,
) -> str:
    """解析并执行一个 tool_call。"""
    try:
        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON arguments for tool '{tc.function.name}': {e}"

    # 对文件工具解析相对路径
    if cwd and tc.function.name in ("Read", "Write", "read_file", "write_file"):
        args = _resolve_relative_paths(args, cwd)

    return await registry.execute(tc.function.name, args)


def _resolve_relative_paths(args: dict[str, Any], cwd: str) -> dict[str, Any]:
    """将 file_path 参数中的相对路径转为绝对路径。"""
    import os
    for key in ("file_path", "path"):
        if key in args and not os.path.isabs(args[key]):
            args[key] = os.path.join(cwd, args[key])
    return args


async def _stream_call(
    client: LLMClient,
    messages: list[ChatMessage],
    tool_schemas: list,
    on_text: Callable[[str], Awaitable[None]],
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> LLMResponse:
    """流式调用 LLM，实时回调文本 delta，最终返回完整 LLMResponse。"""
    full_content = ""
    tool_calls_acc: dict[int, dict[str, Any]] = {}  # index → accumulated data
    finish_reason = None

    async for chunk in client.chat_stream(
        messages,
        tools=tool_schemas or None,
        temperature=temperature,
        max_tokens=max_tokens,
    ):
        # 文本 delta
        if chunk.delta_content:
            full_content += chunk.delta_content
            await on_text(chunk.delta_content)

        # tool_call delta（增量拼接）
        if chunk.delta_tool_calls:
            for tc_delta in chunk.delta_tool_calls:
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls_acc:
                    tool_calls_acc[idx] = {
                        "id": tc_delta.get("id", ""),
                        "type": tc_delta.get("type", "function"),
                        "function": {"name": "", "arguments": ""},
                    }
                acc = tool_calls_acc[idx]
                if tc_delta.get("id"):
                    acc["id"] = tc_delta["id"]
                fn_delta = tc_delta.get("function", {})
                if fn_delta.get("name"):
                    acc["function"]["name"] += fn_delta["name"]
                if fn_delta.get("arguments"):
                    acc["function"]["arguments"] += fn_delta["arguments"]

        if chunk.finish_reason:
            finish_reason = chunk.finish_reason

    # 组装 tool_calls
    parsed_tcs = None
    if tool_calls_acc:
        parsed_tcs = [
            ToolCallInfo(
                id=v["id"],
                type=v["type"],
                function=FunctionCallInfo(
                    name=v["function"]["name"],
                    arguments=v["function"]["arguments"],
                ),
            )
            for v in sorted(tool_calls_acc.values(), key=lambda x: x.get("id", ""))
        ]

    return LLMResponse(
        content=full_content or None,
        tool_calls=parsed_tcs,
        finish_reason=finish_reason,
    )
