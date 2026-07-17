"""Security checks for untrusted research materials."""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class SourceSecurityError(ValueError):
    """Raised when an upload cannot be safely accepted."""


_ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".tsv", ".ppt", ".pptx",
    ".html", ".htm", ".mhtml", ".md", ".markdown", ".txt", ".rtf", ".jpg", ".jpeg",
    ".png", ".tif", ".tiff", ".bmp", ".zip",
}
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"忽略(之前|上面|所有).*指令", re.I),
    re.compile(r"system\s+message|系统消息|developer\s+message|开发者消息", re.I),
    re.compile(r"reveal\s+(your|the)\s+prompt|泄露.*提示词", re.I),
)


def safe_filename(filename: str) -> str:
    """Return a single safe basename and reject ambiguous path input."""
    if "/" in filename or "\\" in filename:
        raise SourceSecurityError("filename must be a basename")
    name = Path(filename).name
    if not name or name in {".", ".."}:
        raise SourceSecurityError("filename must be a basename")
    if any(ord(ch) < 32 for ch in name) or len(name) > 255:
        raise SourceSecurityError("filename contains control characters or is too long")
    if Path(name).suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise SourceSecurityError(f"unsupported file extension: {Path(name).suffix}")
    return name


def ensure_within(root: Path, candidate: Path) -> Path:
    root = root.expanduser().resolve()
    path = candidate.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SourceSecurityError("path escapes project boundary") from exc
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO, block_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(block_size):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def inspect_zip(data: bytes, max_members: int = 1000, max_uncompressed: int = 512 * 1024 * 1024) -> list[str]:
    """Reject traversal and zip bombs before extracting any member."""
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise SourceSecurityError("archive has too many members")
            total = 0
            names: list[str] = []
            for info in members:
                name = info.filename.replace("\\", "/")
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise SourceSecurityError("archive contains path traversal")
                total += info.file_size
                if total > max_uncompressed:
                    raise SourceSecurityError("archive uncompressed size exceeds limit")
                names.append(name)
            return names
    except zipfile.BadZipFile as exc:
        raise SourceSecurityError("invalid zip archive") from exc


def sanitize_untrusted_text(text: str) -> tuple[str, list[str]]:
    """Annotate instruction-like material; never execute it as agent instructions."""
    warnings: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            warnings.append("prompt_injection_like_text")
            break
    return text, warnings
