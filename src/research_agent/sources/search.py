"""Project-scoped keyword and semantic hybrid retrieval."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .enums import SourceStatus
from .models import SearchResult, SourceAsset, SourceChunk
from .repository import SQLiteRepository
from .embeddings import EmbeddingProvider, configured_provider, cosine


_ALIASES = {
    "revenue": {"revenue", "sales", "收入", "营收", "营业收入", "销售额"},
    "annual": {"annual", "yearly", "年度", "全年"},
    "usd": {"usd", "dollar", "dollars", "美元"},
}
_CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}


def _chinese_integer(value: str) -> int:
    total = section = number = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[char]
            continue
        unit = _CHINESE_UNITS.get(char)
        if unit is None:
            continue
        if unit < 10_000:
            section += (number or 1) * unit
        else:
            section = (section + number) * unit
            total += section
            section = 0
        number = 0
    return total + section + number


def _numeric_tokens(text: str) -> list[str]:
    normalized = text.casefold().replace(",", "")
    tokens: list[str] = []
    for raw, suffix in re.findall(r"(\d+(?:\.\d+)?)\s*(m|million|万|亿)?", normalized):
        number = float(raw)
        multiplier = {"m": 1_000_000, "million": 1_000_000, "万": 10_000, "亿": 100_000_000}.get(suffix, 1)
        absolute = number * multiplier
        if absolute >= 1_000_000:
            absolute_token = str(int(absolute)) if absolute.is_integer() else f"{absolute:.6f}".rstrip("0").rstrip(".")
            tokens.extend([absolute_token, f"{absolute / 1_000_000:g}m"])
    for raw in re.findall(r"[零〇一二两三四五六七八九十百千万亿]+", text):
        absolute = _chinese_integer(raw)
        if absolute >= 1_000_000:
            tokens.extend([str(absolute), f"{absolute / 1_000_000:g}m"])
    return tokens


def _terms(text: str) -> list[str]:
    lowered = text.casefold()
    words = re.findall(r"[a-z0-9_]+|[一-鿿]", lowered)
    chinese = "".join(re.findall(r"[一-鿿]", lowered))
    words.extend(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    for canonical, aliases in _ALIASES.items():
        if any(alias in lowered for alias in aliases):
            words.append(canonical)
    words.extend(_numeric_tokens(text))
    return words


@dataclass(frozen=True)
class SearchFilters:
    source_ids: frozenset[str] | None = None
    media_types: frozenset[str] | None = None
    source_tiers: frozenset[str] | None = None
    include_inactive: bool = False


class HybridSearchIndex:
    def __init__(self, repository: SQLiteRepository, embedding_provider: EmbeddingProvider | None = None):
        self.repository = repository
        self.embedding_provider = embedding_provider if embedding_provider is not None else configured_provider()

    def search(self, project_id: str, query: str, *, limit: int = 10, filters: SearchFilters | None = None,
               adjacent: int = 1) -> list[SearchResult]:
        filters = filters or SearchFilters()
        query_terms = _terms(query)
        if not query_terms:
            return []
        query_counts = Counter(query_terms)
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
        semantic_scores: dict[str, float] = {}
        if self.embedding_provider:
            query_vector = self.embedding_provider.embed([query])[0]
            stored = self.repository.get_chunk_embeddings([chunk.chunk_id for chunk in chunks], self.embedding_provider.model_name)
            missing = [chunk for chunk in chunks if chunk.chunk_id not in stored]
            if missing:
                generated = self.embedding_provider.embed([chunk.text for chunk in missing])
                self.repository.put_chunk_embeddings(missing, self.embedding_provider.model_name, generated)
                stored.update({chunk.chunk_id: vector for chunk, vector in zip(missing, generated)})
            semantic_scores = {chunk.chunk_id: max(0.0, cosine(query_vector, stored[chunk.chunk_id])) for chunk in chunks}
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
            semantic_score = semantic_scores.get(chunk.chunk_id, 0.0)
            matched_terms = sum(weight for term, weight in query_counts.items() if counts[term])
            coverage_bonus = 0.15 * matched_terms / max(sum(query_counts.values()), 1)
            phrase_bonus = 0.2 if query.casefold() in chunk.text.casefold() else 0
            heading_bonus = 0.08 if any(term in _terms(" ".join(chunk.heading_path)) for term in query_terms) else 0
            tier_bonus = {"S": 0.06, "A": 0.04, "B": 0.02}.get(eligible[chunk.source_id].source_tier, 0)
            semantic_weight = 0.35 if self.embedding_provider else 0.0
            keyword_weight = 0.55 if self.embedding_provider else 0.90
            score = keyword_weight * min(keyword_score, 1.0) + semantic_weight * semantic_score + coverage_bonus + phrase_bonus + heading_bonus + tier_bonus
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
