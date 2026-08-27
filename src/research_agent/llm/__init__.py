"""LLM Adapter 层。

纯 httpx 实现，兼容任何 OpenAI Chat Completions API 接口。
"""
from .client import LLMClient
from .errors import (
    AuthenticationError,
    ContextLengthExceededError,
    LLMError,
    ModelNotFoundError,
    QuotaExhaustedError,
    RateLimitError,
)
from .types import (
    ChatMessage,
    FunctionCallInfo,
    LLMResponse,
    StreamChunk,
    ToolCallInfo,
    ToolDefinition,
)

__all__ = [
    "LLMClient",
    "ChatMessage",
    "FunctionCallInfo",
    "LLMResponse",
    "StreamChunk",
    "ToolCallInfo",
    "ToolDefinition",
    "LLMError",
    "RateLimitError",
    "QuotaExhaustedError",
    "AuthenticationError",
    "ModelNotFoundError",
    "ContextLengthExceededError",
]
