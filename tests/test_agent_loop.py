import pytest

from research_agent.agent_loop import (
    AgentLoopStuckError,
    AgentOptions,
    AgentSession,
    run_agent,
)
from research_agent.agent_loop.loop import _ToolErrorTracker, _stream_call
from research_agent.llm.types import (
    FunctionCallInfo,
    LLMResponse,
    StreamChunk,
    ToolCallInfo,
)
from research_agent.tools.registry import ToolRegistry


class RepeatingToolClient:
    async def chat(self, messages, **kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallInfo(
                    id="call-1",
                    function=FunctionCallInfo(
                        name="broken_tool",
                        arguments="{}",
                    ),
                )
            ],
        )


@pytest.mark.anyio
async def test_run_agent_stops_repeated_tool_errors() -> None:
    registry = ToolRegistry()

    @registry.tool(name="broken_tool")
    async def broken_tool() -> str:
        raise ValueError("chunk not found in project")

    with pytest.raises(AgentLoopStuckError, match="same error"):
        await run_agent(
            user_prompt="test",
            options=AgentOptions(
                system_prompt="test",
                allowed_tools=["broken_tool"],
                max_turns=10,
                stream=False,
                max_repeated_tool_errors=2,
            ),
            llm_client=RepeatingToolClient(),
            tool_registry=registry,
        )


class ScriptedClient:
    """按脚本返回响应；耗尽后返回纯文本收尾。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(content="done", tool_calls=None)


def _tool_call(name: str, call_id: str = "call-1", arguments: str = "{}"):
    return ToolCallInfo(
        id=call_id,
        function=FunctionCallInfo(name=name, arguments=arguments),
    )


def _broken_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool(name="broken_tool")
    async def broken_tool() -> str:
        raise ValueError("chunk not found in project")

    return registry


@pytest.mark.anyio
async def test_agent_session_stops_repeated_tool_errors() -> None:
    """AgentSession 必须与 run_agent 有同等的重复错误保护（backlog 第 6 项）。"""
    client = ScriptedClient(
        [LLMResponse(content="", tool_calls=[_tool_call("broken_tool")]) for _ in range(10)]
    )
    session = AgentSession(
        AgentOptions(
            system_prompt="test",
            allowed_tools=["broken_tool"],
            max_turns=10,
            stream=False,
            max_repeated_tool_errors=2,
        ),
        client,
        _broken_registry(),
    )
    await session.query("请调用工具")

    with pytest.raises(AgentLoopStuckError, match="same error"):
        await session.get_response()

    # 在第 2 次相同错误处停下，而不是烧到 max_turns=10
    assert client.calls == 2


@pytest.mark.anyio
async def test_agent_session_error_budget_spans_multiple_turns() -> None:
    """错误计数跨对话轮次累计，模型无法通过换轮次绕过保护。"""
    registry = _broken_registry()
    options = AgentOptions(
        system_prompt="test",
        allowed_tools=["broken_tool"],
        max_turns=5,
        stream=False,
        max_repeated_tool_errors=2,
    )
    # 每轮：一次失败工具调用，然后返回文本结束本轮
    client = ScriptedClient([
        LLMResponse(content="", tool_calls=[_tool_call("broken_tool")]),
        LLMResponse(content="第一轮结束", tool_calls=None),
        LLMResponse(content="", tool_calls=[_tool_call("broken_tool")]),
    ])
    session = AgentSession(options, client, registry)

    await session.query("第一轮")
    assert await session.get_response() == "第一轮结束"

    await session.query("第二轮")
    with pytest.raises(AgentLoopStuckError):
        await session.get_response()


def test_tracker_resets_budget_after_success() -> None:
    """工具成功一次后清空其错误计数，避免偶发错误累积成误杀。"""
    tracker = _ToolErrorTracker(threshold=2)
    failure = "Error executing tool 'flaky_tool': transient failure"

    tracker.record("flaky_tool", failure)   # 第 1 次失败
    tracker.record("flaky_tool", "ok")      # 成功，计数清零
    tracker.record("flaky_tool", failure)   # 重新从 1 开始，不应触发阈值

    with pytest.raises(AgentLoopStuckError):
        tracker.record("flaky_tool", failure)  # 这才是第 2 次


def test_tracker_distinguishes_different_errors() -> None:
    """不同错误分别计数：换了错误说明模型在调整，不该按同一错误累计。"""
    tracker = _ToolErrorTracker(threshold=2)

    tracker.record("tool", "Error executing tool 'tool': missing chunk_id")
    tracker.record("tool", "Error executing tool 'tool': invalid locator")

    # 两次都是错误但内容不同，未达任一错误的阈值
    with pytest.raises(AgentLoopStuckError):
        tracker.record("tool", "Error executing tool 'tool': invalid locator")


def test_tracker_scopes_reset_to_one_tool() -> None:
    """一个工具成功不应清掉另一个工具的错误计数。"""
    tracker = _ToolErrorTracker(threshold=2)
    error_a = "Error executing tool 'tool_a': boom"

    tracker.record("tool_a", error_a)
    tracker.record("tool_b", "ok")  # 与 tool_a 无关

    with pytest.raises(AgentLoopStuckError, match="tool_a"):
        tracker.record("tool_a", error_a)


@pytest.mark.anyio
async def test_stream_tool_calls_are_ordered_by_index() -> None:
    """流式拼接按 index 排序；id 是随机串，按 id 排会打乱并行调用顺序。"""

    class StreamingClient:
        async def chat_stream(self, messages, **kwargs):
            # 故意让 id 的字典序与 index 顺序相反
            yield StreamChunk(delta_tool_calls=[
                {"index": 0, "id": "zzz", "function": {"name": "first", "arguments": "{}"}}
            ])
            yield StreamChunk(delta_tool_calls=[
                {"index": 1, "id": "aaa", "function": {"name": "second", "arguments": "{}"}}
            ])
            yield StreamChunk(finish_reason="tool_calls")

    async def noop(_text: str) -> None:
        return None

    response = await _stream_call(StreamingClient(), [], [], noop)

    assert [tc.function.name for tc in response.tool_calls] == ["first", "second"]
    assert [tc.id for tc in response.tool_calls] == ["zzz", "aaa"]


@pytest.mark.anyio
async def test_stream_accumulates_split_arguments() -> None:
    """参数分片到达时按 index 正确拼接。"""

    class StreamingClient:
        async def chat_stream(self, messages, **kwargs):
            yield StreamChunk(delta_tool_calls=[
                {"index": 0, "id": "c0", "function": {"name": "Read", "arguments": '{"file'}}
            ])
            yield StreamChunk(delta_tool_calls=[
                {"index": 0, "function": {"arguments": '_path": "/tmp/a.md"}'}}
            ])
            yield StreamChunk(delta_content="思考中")

    captured: list[str] = []

    async def collect(text: str) -> None:
        captured.append(text)

    response = await _stream_call(StreamingClient(), [], [], collect)

    assert response.tool_calls[0].function.arguments == '{"file_path": "/tmp/a.md"}'
    assert response.content == "思考中"
    assert captured == ["思考中"]


def test_registry_subset_is_isolated_from_parent() -> None:
    """subset 派生的注册表可挂临时工具而不污染源注册表。"""
    parent = ToolRegistry()

    @parent.tool(name="Read")
    async def read_tool() -> str:
        return "ok"

    @parent.tool(name="Write")
    async def write_tool() -> str:
        return "ok"

    derived = parent.subset(["Read"])
    derived.register("AskUser", lambda: "asked", {"name": "AskUser", "parameters": {}})

    assert derived.available_tools == ["Read", "AskUser"]
    assert "AskUser" not in parent.available_tools
    assert "Write" not in derived.available_tools


def test_registry_subset_skips_unknown_names() -> None:
    parent = ToolRegistry()

    @parent.tool(name="Read")
    async def read_tool() -> str:
        return "ok"

    derived = parent.subset(["Read", "DoesNotExist"])

    assert derived.available_tools == ["Read"]


@pytest.mark.anyio
async def test_run_agent_reports_usage_per_call() -> None:
    """每次 LLM 响应的 usage 都要上报，含带 tool_call 的中间轮次。"""
    client = ScriptedClient([
        LLMResponse(
            content="",
            tool_calls=[_tool_call("Read")],
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        ),
        LLMResponse(
            content="完成",
            tool_calls=None,
            usage={"prompt_tokens": 150, "completion_tokens": 30},
        ),
    ])
    registry = ToolRegistry()

    @registry.tool(name="Read")
    async def read_tool() -> str:
        return "content"

    seen: list[dict | None] = []

    await run_agent(
        user_prompt="test",
        options=AgentOptions(
            system_prompt="test", allowed_tools=["Read"], max_turns=5, stream=False
        ),
        llm_client=client,
        tool_registry=registry,
        on_usage=seen.append,
    )

    assert seen == [
        {"prompt_tokens": 100, "completion_tokens": 20},
        {"prompt_tokens": 150, "completion_tokens": 30},
    ]


@pytest.mark.anyio
async def test_agent_session_reports_usage() -> None:
    client = ScriptedClient([
        LLMResponse(content="回答", tool_calls=None, usage={"prompt_tokens": 60, "completion_tokens": 12}),
    ])
    seen: list[dict | None] = []
    session = AgentSession(
        AgentOptions(system_prompt="test", max_turns=3, stream=False),
        client,
        ToolRegistry(),
        on_usage=seen.append,
    )
    await session.query("问题")

    await session.get_response()

    assert seen == [{"prompt_tokens": 60, "completion_tokens": 12}]


@pytest.mark.anyio
async def test_stream_call_captures_usage_from_final_chunk() -> None:
    """include_usage 时 usage 在最后一个 chunk，且该 chunk 的 choices 为空。"""

    class StreamingClient:
        async def chat_stream(self, messages, **kwargs):
            yield StreamChunk(delta_content="部分文本")
            yield StreamChunk(finish_reason="stop")
            yield StreamChunk(usage={"prompt_tokens": 900, "completion_tokens": 120})

    async def noop(_text: str) -> None:
        return None

    response = await _stream_call(StreamingClient(), [], [], noop)

    assert response.usage == {"prompt_tokens": 900, "completion_tokens": 120}
    assert response.content == "部分文本"
