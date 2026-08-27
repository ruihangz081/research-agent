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
    """429 Too Many Requests — 触发重试（短期限流）。"""

    pass


class QuotaExhaustedError(RateLimitError):
    """429 固定窗口额度耗尽 — 不重试，直接进入 paused。

    与短期限流区分：短期限流（retry-after 秒级）退避重试即可恢复；额度耗尽
    （余额不足 / 月度配额用完 / billing 相关）重试只会白白消耗等待时间，必须
    等待外部条件恢复（充值、配额刷新）。识别依据是 429 响应体中的额度/余额/
    计费关键词，或 retry-after 异常大的退避窗口。
    """

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
