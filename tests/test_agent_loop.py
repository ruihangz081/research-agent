import pytest

from research_agent.agent_loop import AgentLoopStuckError, AgentOptions, run_agent
from research_agent.llm.types import FunctionCallInfo, LLMResponse, ToolCallInfo
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
