"""Project-scoped keyword and semantic hybrid retrieval."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .enums import SourceStatus
from .models import SearchResult, SourceAsset, SourceChunk
from .repository import SQLiteRepository


def _terms(text: str) -> list[str]:
    lowered = text.casefold()
    words = re.findall(r"[a-z0-9_]+|[一-鿿]", lowered)
    chinese = "".join(re.findall(r"[一-鿿]", lowered))
    words.extend(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    return words


def _vector(text: str, dimensions: int = 384) -> dict[int, float]:
    counts: dict[int, float] = defaultdict(float)
    terms = _terms(text)
    for term in terms:
        bucket = int.from_bytes(hashlib.blake2b(term.encode(), digest_size=4).digest(), "big") % dimensions
        counts[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
    return {key: value / norm for key, value in counts.items()}


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    return sum(value * right.get(key, 0.0) for key, value in left.items())


@dataclass(frozen=True)
class SearchFilters:
    source_ids: frozenset[str] | None = None
    media_types: frozenset[str] | None = None
    source_tiers: frozenset[str] | None = None
    include_inactive: bool = False


class HybridSearchIndex:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def search(self, project_id: str, query: str, *, limit: int = 10, filters: SearchFilters | None = None,
               adjacent: int = 1) -> list[SearchResult]:
        filters = filters or SearchFilters()
        query_terms = _terms(query)
        if not query_terms:
            return []
        query_counts = Counter(query_terms)
        query_vector = _vector(query)
        sources = {source.source_id: source for source in self.repository.list_sources(project_id, include_superseded=True)}
        eligible: dict[str, SourceAsset] = {}
        for source_id, source in sources.items():
            if not filters.include_inactive and source.status != SourceStatus.ACTIVE:
                continue
            if filters.source_ids and source_id not in filters.source_ids:
                continue
            if filters.media_types and source.detected_media_type not in filters.media_types:
                continue
            if filters.source_tiers and source.source_tier not in filters.source_tiers:
                continue
            eligible[source_id] = source
        chunks = [chunk for chunk in self.repository.all_chunks(project_id) if chunk.source_id in eligible]
        if not chunks:
            return []
        document_frequency = Counter()
        tokenized: dict[str, Counter[str]] = {}
        for chunk in chunks:
            counts = Counter(_terms(chunk.text))
            tokenized[chunk.chunk_id] = counts
            document_frequency.update(counts.keys())
        average_length = sum(sum(counts.values()) for counts in tokenized.values()) / len(tokenized)
        results: list[SearchResult] = []
        for chunk in chunks:
            counts = tokenized[chunk.chunk_id]
            length = sum(counts.values())
            keyword_score = 0.0
            for term, query_weight in query_counts.items():
                frequency = counts[term]
                if not frequency:
                    continue
                inverse = math.log(1 + (len(chunks) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
                keyword_score += query_weight * inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / max(average_length, 1)))
            semantic_score = _cosine(query_vector, _vector(chunk.text))
            phrase_bonus = 0.2 if query.casefold() in chunk.text.casefold() else 0
            heading_bonus = 0.08 if any(term in _terms(" ".join(chunk.heading_path)) for term in query_terms) else 0
            tier_bonus = {"S": 0.06, "A": 0.04, "B": 0.02}.get(eligible[chunk.source_id].source_tier, 0)
            score = 0.55 * min(keyword_score, 1.0) + 0.35 * semantic_score + phrase_bonus + heading_bonus + tier_bonus
            if score > 0:
                results.append(SearchResult(chunk=chunk, source=eligible[chunk.source_id], score=score,
                                            keyword_score=keyword_score, semantic_score=semantic_score,
                                            highlights=[term for term in query_terms if term in counts][:8]))
        results.sort(key=lambda item: (-item.score, item.chunk.ordinal, item.chunk.chunk_id))
        deduplicated: list[SearchResult] = []
        hashes: set[str] = set()
        for result in results:
            if result.chunk.content_hash not in hashes:
                hashes.add(result.chunk.content_hash)
                deduplicated.append(result)
        selected = deduplicated[:limit]
        if adjacent <= 0:
            return selected
        selected_ids = {item.chunk.chunk_id for item in selected}
        by_source_ordinal = {(chunk.source_id, chunk.ordinal): chunk for chunk in chunks}
        expanded = list(selected)
        for result in list(selected):
            for offset in range(-adjacent, adjacent + 1):
                neighbor = by_source_ordinal.get((result.chunk.source_id, result.chunk.ordinal + offset))
                if neighbor and neighbor.chunk_id not in selected_ids:
                    selected_ids.add(neighbor.chunk_id)
                    expanded.append(SearchResult(chunk=neighbor, source=result.source, score=result.score * 0.55,
                                                 keyword_score=0, semantic_score=0, highlights=[]))
        expanded.sort(key=lambda item: (-item.score, item.chunk.ordinal))
        return expanded
