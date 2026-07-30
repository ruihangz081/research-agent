"""工具注册表。

支持 @registry.tool 装饰器注册和 registry.register() 手动注册。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from ..llm.types import ToolDefinition
from .schemas import generate_schema

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册与执行。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def tool(
        self,
        name: str | None = None,
        description: str = "",
    ) -> Callable:
        """装饰器：注册一个工具函数。

        用法：
            @registry.tool(name="Read", description="读取文件")
            async def read_file(file_path: str) -> str:
                ...
        """

        def decorator(func: Callable) -> Callable:
            tool_name = name or func.__name__
            schema = generate_schema(func, tool_name, description)
            self._handlers[tool_name] = func
            self._schemas[tool_name] = schema
            return func

        return decorator

    def register(self, name: str, handler: Callable, schema: dict[str, Any]) -> None:
        """手动注册一个工具。"""
        self._handlers[name] = handler
        self._schemas[name] = schema

    def subset(self, names: list[str]) -> "ToolRegistry":
        """派生一个只含指定工具的新注册表。

        用于给单次 agent 调用挂载临时工具（例如向用户提问），而不污染全局注册表。
        未找到的名字会被跳过并记录告警，与 `get_schemas` 行为一致。
        """
        derived = ToolRegistry()
        for name in names:
            handler = self._handlers.get(name)
            schema = self._schemas.get(name)
            if handler is None or schema is None:
                logger.warning("Tool '%s' not found in registry, skipping", name)
                continue
            derived.register(name, handler, schema)
        return derived

    def get_schemas(
        self, tool_names: list[str] | None = None
    ) -> list[ToolDefinition]:
        """返回 OpenAI 格式的工具定义列表。

        如果 tool_names 为 None，返回全部注册工具。
        """
        names = tool_names or list(self._schemas.keys())
        result = []
        for n in names:
            if n in self._schemas:
                result.append(
                    ToolDefinition(type="function", function=self._schemas[n])
                )
            else:
                logger.warning("Tool '%s' not found in registry, skipping", n)
        return result

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """执行一个工具，返回结果字符串。"""
        handler = self._handlers.get(name)
        if not handler:
            return f"Error: Unknown tool '{name}'. Available tools: {list(self._handlers.keys())}"
        try:
            import asyncio
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**arguments)
            else:
                result = handler(**arguments)
            return str(result) if result is not None else "Done."
        except Exception as e:
            logger.exception("Tool '%s' execution failed", name)
            return f"Error executing tool '{name}': {e}"

    @property
    def available_tools(self) -> list[str]:
        return list(self._handlers.keys())


# 全局默认注册表
default_registry = ToolRegistry()
