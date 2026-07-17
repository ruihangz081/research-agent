"""Small dependency-free metrics and structured event helpers."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("research_agent.sources")


class SourceMetrics:
    def __init__(self):
        self._counts: Counter[str] = Counter()
        self._durations: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counts[name] += value

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self._durations.setdefault(name, []).append(time.perf_counter() - started)

    def snapshot(self) -> dict:
        with self._lock:
            return {"counters": dict(self._counts), "durations": {name: {"count": len(values), "p50": sorted(values)[len(values) // 2], "max": max(values)} for name, values in self._durations.items()}}

    def event(self, name: str, **fields) -> None:
        self.increment(name)
        logger.info(json.dumps({"event": name, **fields}, ensure_ascii=False, default=str))


metrics = SourceMetrics()
