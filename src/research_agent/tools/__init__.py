"""Tool 系统。

提供工具注册、schema 生成、执行器。
内置 4 个工具：Read / Write / WebSearch / WebFetch。
"""
from .registry import ToolRegistry, default_registry

__all__ = ["ToolRegistry", "default_registry"]

# 导入 builtins 触发工具注册
from .builtins import _register_all as _  # noqa: F401, E402
