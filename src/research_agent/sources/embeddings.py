"""Real embedding provider abstraction used by hybrid retrieval."""
from __future__ import annotations

import math
import os
from typing import Protocol

import httpx


class EmbeddingProvider(Protocol):
    model_name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    """Small synchronous client for OpenAI-compatible embedding endpoints."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_name, "input": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        items = sorted(response.json()["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in items]


def configured_provider() -> EmbeddingProvider | None:
    base_url = os.getenv("SOURCE_EMBEDDING_BASE_URL", "").strip()
    api_key = os.getenv("SOURCE_EMBEDDING_API_KEY", "").strip()
    model = os.getenv("SOURCE_EMBEDDING_MODEL", "").strip()
    if not (base_url and api_key and model):
        return None
    return OpenAIEmbeddingProvider(base_url, api_key, model)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
