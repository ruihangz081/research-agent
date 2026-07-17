"""LLM 错误类型层级。"""
from __future__ import annotations


class LLMError(Exception):
    """LLM Adapter 基础错误。"""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RateLimitError(LLMError):
    """429 Too Many Requests — 触发重试。"""

    pass


class AuthenticationError(LLMError):
    """401/403 — API Key 无效或无权限。"""

    pass


class ModelNotFoundError(LLMError):
    """404 — 模型不存在。"""

    pass


class ContextLengthExceededError(LLMError):
    """上下文窗口超限。"""

    pass


class ServerError(LLMError):
    """5xx 服务端错误 — 触发重试。"""

    pass
