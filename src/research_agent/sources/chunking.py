"""Structure-aware deterministic chunk generation."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .enums import BlockType
from .models import ContentBlock, SourceAsset, SourceChunk, SourceDocument


def token_count(text: str) -> int:
    return len(re.findall(r"[一-鿿]|[A-Za-z0-9_]+", text))


def _make_chunk(source: SourceAsset, blocks: list[ContentBlock], ordinal: int) -> SourceChunk:
    text = "\n".join(block.text.strip() for block in blocks if block.text.strip())
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    identity = hashlib.sha256(f"{source.source_id}:{source.version}:{ordinal}:{content_hash}".encode()).hexdigest()[:24]
    locators = [block.locator for block in blocks if block.locator is not None]
    heading_path = next((block.heading_path for block in reversed(blocks) if block.heading_path), [])
    return SourceChunk(chunk_id=f"chk_{identity}", source_id=source.source_id, source_version=source.version,
                       text=text, heading_path=heading_path, locators=locators,
                       block_ids=[block.block_id for block in blocks], token_count=token_count(text),
                       content_hash=content_hash, ordinal=ordinal)


def chunk_document(source: SourceAsset, document: SourceDocument, max_chars: int = 1800, overlap_blocks: int = 1) -> list[SourceChunk]:
    """Chunk on block boundaries; table blocks remain atomic."""
    chunks: list[SourceChunk] = []
    pending: list[ContentBlock] = []
    length = 0
    for block in sorted(document.blocks, key=lambda item: item.order):
        if not block.text.strip():
            continue
        atomic = block.block_type == BlockType.TABLE
        would_overflow = pending and length + len(block.text) + 1 > max_chars
        if would_overflow or (atomic and pending):
            chunks.append(_make_chunk(source, pending, len(chunks)))
            pending = pending[-overlap_blocks:] if overlap_blocks and not atomic else []
            length = sum(len(item.text) + 1 for item in pending)
        if atomic and len(block.text) > max_chars:
            chunks.append(_make_chunk(source, [block], len(chunks)))
            pending, length = [], 0
            continue
        pending.append(block)
        length += len(block.text) + 1
        if atomic:
            chunks.append(_make_chunk(source, pending, len(chunks)))
            pending, length = [], 0
    if pending:
        chunks.append(_make_chunk(source, pending, len(chunks)))
    return chunks
