"""Agent Loop 核心实现。

run_agent()     — 单次执行（collector/validator/analyst/formatter 用）
AgentSession    — 多轮对话会话（strategist 用）

内部循环：prompt → LLM → tool_calls → 执行 → 回传 → 直到无 tool_call 或达到 max_turns。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from .. import token_usage
from ..llm.client import LLMClient
from ..llm.types import (
    ChatMessage,
    FunctionCallInfo,
    LLMResponse,
    ToolCallInfo,
)
from ..tools.registry import ToolRegistry
from .types import AgentLoopStuckError, AgentOptions

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 单次执行
# ═══════════════════════════════════════════════════════════════


class _ToolErrorTracker:
    """跟踪同一工具重复返回相同错误的次数。

    模型有时会用同样的错误参数反复调用同一个工具。没有这个保护，循环会一路
    烧到 `max_turns`，白花 token 且最终仍然失败。`run_agent` 与 `AgentSession`
    共用这份逻辑，避免两个入口的保护强度不一致。
    """

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self._counts: dict[tuple[str, str, str], int] = {}

    def record(self, tool_name: str, result: str, arguments: str = "") -> None:
        """登记一次工具结果；相同参数的同一错误达到阈值时停止。"""
        if not result.startswith("Error executing tool '"):
            # 该工具本轮成功，清掉它此前累积的错误计数
            self._counts = {
                key: count for key, count in self._counts.items() if key[0] != tool_name
            }
            return

        try:
            normalized_arguments = json.dumps(
                json.loads(arguments or "{}"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (json.JSONDecodeError, TypeError):
            normalized_arguments = arguments

        key = (tool_name, normalized_arguments, result)
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] >= self.threshold:
            raise AgentLoopStuckError(
                f"tool {tool_name!r} returned the same error "
                f"{self._counts[key]} times for identical arguments; "
                f"agent execution stopped. Last tool error: {result}"
            )


async def _run_tool_calls(
    response: LLMResponse,
    messages: list[ChatMessage],
    tool_registry: ToolRegistry,
    cwd: str,
    tracker: _ToolErrorTracker,
) -> None:
    """执行本轮全部 tool_call，把结果作为 tool 消息追加进历史。"""
    for tc in response.tool_calls or []:
        result = await _execute_tool_call(tc, tool_registry, cwd)
        tracker.record(tc.function.name, result, tc.function.arguments)
        messages.append(
            ChatMessage(
                role="tool",
                content=result,
                tool_call_id=tc.id,
                name=tc.function.name,
            )
        )


async def run_agent(
    user_prompt: str,
    options: AgentOptions,
    llm_client: LLMClient,
    tool_registry: ToolRegistry,
    on_assistant_text: Callable[[str], Awaitable[None]] | None = None,
    on_usage: Callable[[dict[str, int] | None], None] | None = None,
) -> str:
    """运行单次 agent 任务至完成。

    循环：user_prompt → LLM → [tool_calls → 执行 → 回传]* → 最终文本

    `on_usage` 会在每次 LLM 响应后收到该次调用的 usage（可能为 None），
    用于累计 token 消耗。

    返回最终的 assistant 文本响应。
    """
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=options.system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]
    tool_schemas = tool_registry.get_schemas(options.allowed_tools or None)
    tracker = _ToolErrorTracker(options.max_repeated_tool_errors)

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
        if on_usage is not None:
            on_usage(response.usage)
        else:
            token_usage.report(response.usage)

        # 2. 追加 assistant 消息
        messages.append(
            ChatMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

        # 3. 无 tool_call → 完成
        if not response.tool_calls:
            return response.content or ""

        # 4. 执行本轮 tool_call
        await _run_tool_calls(
            response, messages, tool_registry, options.cwd, tracker
        )

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
        on_usage: Callable[[dict[str, int] | None], None] | None = None,
    ):
        self.options = options
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.on_assistant_text = on_assistant_text
        self.on_usage = on_usage
        self.messages: list[ChatMessage] = [
            ChatMessage(role="system", content=options.system_prompt),
        ]
        self._tool_schemas = tool_registry.get_schemas(options.allowed_tools or None)
        self._turns_used = 0
        # 与 run_agent 共用同一份重复错误保护：跨轮累计，避免模型在多轮对话里
        # 反复用同样的错误参数调同一个工具，一路烧到 max_turns
        self._tracker = _ToolErrorTracker(options.max_repeated_tool_errors)

    async def query(self, user_input: str) -> None:
        """添加用户消息到历史。"""
        self.messages.append(ChatMessage(role="user", content=user_input))

    async def get_response(self) -> str:
        """获取 LLM 响应，执行工具调用循环直到产出最终文本。

        返回本轮的 assistant 文本。同一工具重复返回相同错误达到阈值时抛出
        AgentLoopStuckError。
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
            if self.on_usage is not None:
                self.on_usage(response.usage)
            else:
                token_usage.report(response.usage)

            self.messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            self._turns_used += 1

            if not response.tool_calls:
                return response.content or ""

            await _run_tool_calls(
                response,
                self.messages,
                self.tool_registry,
                self.options.cwd,
                self._tracker,
            )

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
    usage: dict[str, int] | None = None

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

        # usage 通常只在最后一个 chunk 出现（stream_options.include_usage）
        if chunk.usage:
            usage = chunk.usage

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
    # 按 index 排序：OpenAI 流式协议用 index 标识 tool_call 顺序，id 只是随机串，
    # 按 id 排序会在并行 tool_call 时打乱顺序。
    parsed_tcs = None
    if tool_calls_acc:
        parsed_tcs = [
            ToolCallInfo(
                id=value["id"],
                type=value["type"],
                function=FunctionCallInfo(
                    name=value["function"]["name"],
                    arguments=value["function"]["arguments"],
                ),
            )
            for _, value in sorted(tool_calls_acc.items(), key=lambda item: item[0])
        ]

    return LLMResponse(
        content=full_content or None,
        tool_calls=parsed_tcs,
        finish_reason=finish_reason,
        usage=usage,
    )
