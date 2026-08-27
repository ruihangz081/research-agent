"""LLM 错误分类回归测试：429 短期限流 vs 固定窗口额度耗尽。"""
from __future__ import annotations

import pytest

from research_agent.llm import (
    AuthenticationError,
    ContextLengthExceededError,
    LLMError,
    ModelNotFoundError,
    QuotaExhaustedError,
    RateLimitError,
)
from research_agent.llm.client import LLMClient


def test_429_with_quota_keyword_raises_quota_exhausted() -> None:
    with pytest.raises(QuotaExhaustedError):
        LLMClient._raise_for_status(
            429, '{"error": {"message": "Your quota has been exhausted"}}'
        )


def test_429_with_balance_keyword_raises_quota_exhausted() -> None:
    with pytest.raises(QuotaExhaustedError):
        LLMClient._raise_for_status(429, "余额不足，请充值")


def test_429_plain_raises_rate_limit() -> None:
    with pytest.raises(RateLimitError) as exc_info:
        LLMClient._raise_for_status(429, '{"error": {"message": "Too many requests"}}')
    # 短期限流不是额度耗尽
    assert not isinstance(exc_info.value, QuotaExhaustedError)


def test_quota_exhausted_is_rate_limit_subclass() -> None:
    assert issubclass(QuotaExhaustedError, RateLimitError)


def test_401_raises_auth_error() -> None:
    with pytest.raises(AuthenticationError):
        LLMClient._raise_for_status(401, "invalid key")


def test_404_raises_model_not_found() -> None:
    with pytest.raises(ModelNotFoundError):
        LLMClient._raise_for_status(404, "model not found")


def test_context_length_keyword() -> None:
    with pytest.raises(ContextLengthExceededError):
        LLMClient._raise_for_status(400, "context_length exceeded")


def test_unknown_400_raises_llm_error() -> None:
    with pytest.raises(LLMError):
        LLMClient._raise_for_status(400, "bad request")
