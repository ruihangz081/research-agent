"""LLM HTTP 客户端。

基于 httpx.AsyncClient，调用 OpenAI 兼容的 Chat Completions API。
支持：
- 非流式 chat()
- 流式 chat_stream()（SSE 解析）
- 指数退避重试（429/5xx）
- 连接池复用（async with 上下文管理器）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from .errors import (
    AuthenticationError,
    ContextLengthExceededError,
    LLMError,
    ModelNotFoundError,
    QuotaExhaustedError,
    RateLimitError,
    ServerError,
)
from .types import (
    ChatMessage,
    FunctionCallInfo,
    LLMResponse,
    StreamChunk,
    ToolCallInfo,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


class LLMClient:
    """异步 LLM 客户端。

    用法：
        async with LLMClient(base_url=..., api_key=..., model=...) as client:
            resp = await client.chat(messages)
    """

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o",
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=30.0),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
            },
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ═══════════════════════════════════════════════════
    # 非流式调用
    # ═══════════════════════════════════════════════════

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """非流式 Chat Completion。"""
        body = self._build_body(messages, tools, temperature, max_tokens, stream=False)
        data = await self._request_with_retry(body)
        return self._parse_response(data)

    # ═══════════════════════════════════════════════════
    # 流式调用
    # ═══════════════════════════════════════════════════

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式 Chat Completion，逐 chunk 产出。"""
        body = self._build_body(messages, tools, temperature, max_tokens, stream=True)

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self._client.stream(
                    "POST", "/chat/completions", json=body
                ) as resp:
                    if resp.status_code != 200:
                        error_body = ""
                        async for line in resp.aiter_lines():
                            error_body += line
                        self._raise_for_status(resp.status_code, error_body)

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            chunk = self._parse_sse_chunk(line[6:])
                            if chunk:
                                yield chunk
                    return  # 成功完成
            except QuotaExhaustedError:
                # 固定窗口额度耗尽：重试无意义，直接上抛进入 paused。
                raise
            except (RateLimitError, ServerError) as e:
                if attempt == self.max_retries:
                    raise
                delay = self._calc_delay(attempt)
                logger.warning(
                    "Stream attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt, self.max_retries, e, delay,
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    raise LLMError(f"HTTP error: {e}") from e
                delay = self._calc_delay(attempt)
                await asyncio.sleep(delay)

    # ═══════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════

    def _build_body(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
        temperature: float,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            # 请求流式响应也返回 usage（OpenAI 兼容扩展）。不支持该字段的服务
            # 会忽略它，此时 token 统计只是缺失，不影响调用本身。
            body["stream_options"] = {"include_usage": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = [t.to_dict() for t in tools]
        return body

    async def _request_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        """带重试的 POST 请求。"""
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.post("/chat/completions", json=body)
                if resp.status_code == 200:
                    return resp.json()
                self._raise_for_status(resp.status_code, resp.text)
            except QuotaExhaustedError:
                # 固定窗口额度耗尽：重试无意义，直接上抛进入 paused。
                raise
            except (RateLimitError, ServerError) as e:
                last_err = e
                if attempt == self.max_retries:
                    raise
                delay = self._calc_delay(attempt)
                logger.warning(
                    "Attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt, self.max_retries, e, delay,
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as e:
                last_err = e
                if attempt == self.max_retries:
                    raise LLMError(f"HTTP error: {e}") from e
                delay = self._calc_delay(attempt)
                await asyncio.sleep(delay)
        raise LLMError(f"All {self.max_retries} retries failed") from last_err

    def _calc_delay(self, attempt: int) -> float:
        return self.retry_base_delay * (2 ** (attempt - 1))

    @staticmethod
    def _raise_for_status(status_code: int, body: str) -> None:
        """根据状态码抛出对应的错误。"""
        if status_code == 429:
            if LLMClient._is_quota_exhausted(body):
                raise QuotaExhaustedError(
                    "Quota exhausted", status_code=status_code, body=body
                )
            raise RateLimitError("Rate limited", status_code=status_code, body=body)
        if status_code in (401, 403):
            raise AuthenticationError(
                "Authentication failed", status_code=status_code, body=body
            )
        if status_code == 404:
            raise ModelNotFoundError(
                "Model not found", status_code=status_code, body=body
            )
        if status_code >= 500:
            raise ServerError(
                f"Server error {status_code}", status_code=status_code, body=body
            )
        # 检查是否为 context length 错误
        if "context_length" in body.lower() or "maximum context" in body.lower():
            raise ContextLengthExceededError(
                "Context length exceeded", status_code=status_code, body=body
            )
        raise LLMError(
            f"API error {status_code}", status_code=status_code, body=body
        )

    #: 429 响应体中出现这些关键词即判定为「固定窗口额度耗尽」而非短期限流。
    #: 不同厂商措辞不同（余额不足 / 配额用完 / 计费 / 额度已耗尽），统一用小写子串匹配。
    _QUOTA_KEYWORDS = (
        "quota",
        "balance",
        "billing",
        "insufficient",
        "exhausted",
        "额度",
        "余额",
        "欠费",
        "配额",
        "计费",
        "耗尽",
        "用量超限",
        "充值",
    )

    @staticmethod
    def _is_quota_exhausted(body: str) -> bool:
        lowered = (body or "").lower()
        return any(keyword in lowered for keyword in LLMClient._QUOTA_KEYWORDS)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LLMResponse:
        """解析非流式响应。"""
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        tool_calls = None
        raw_tcs = message.get("tool_calls")
        if raw_tcs:
            tool_calls = [
                ToolCallInfo(
                    id=tc.get("id", ""),
                    type=tc.get("type", "function"),
                    function=FunctionCallInfo(
                        name=tc.get("function", {}).get("name", ""),
                        arguments=tc.get("function", {}).get("arguments", ""),
                    ),
                )
                for tc in raw_tcs
            ]

        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
        )

    @staticmethod
    def _parse_sse_chunk(json_str: str) -> StreamChunk | None:
        """解析单行 SSE data。"""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None

        # include_usage 的最后一个 chunk 通常 choices 为空、只带 usage
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta", {})

        return StreamChunk(
            delta_content=delta.get("content"),
            delta_tool_calls=delta.get("tool_calls"),
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
        )
