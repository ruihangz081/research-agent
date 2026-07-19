"""Content-addressed immutable local object storage."""
from __future__ import annotations

import os
import hashlib
import tempfile
from pathlib import Path

from .security import sha256_bytes


class LocalObjectStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid sha256")
        return self.root / digest[:2] / digest[2:]

    def put(self, data: bytes) -> tuple[str, str]:
        digest = sha256_bytes(data)
        destination = self._path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return digest, f"cas://{digest}"

    def put_stream(self, stream, block_size: int = 1024 * 1024) -> tuple[str, str, int]:
        """Persist a seekable upload without loading it into application memory."""
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        digest = hashlib.sha256()
        size = 0
        try:
            stream.seek(0)
            with os.fdopen(fd, "wb") as handle:
                while block := stream.read(block_size):
                    digest.update(block)
                    size += len(block)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            value = digest.hexdigest()
            destination = self._path(value)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                os.unlink(temporary)
            else:
                os.replace(temporary, destination)
            return value, f"cas://{value}", size
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, digest: str) -> bytes:
        return self._path(digest).read_bytes()

    def open(self, digest: str):
        return self._path(digest).open("rb")

    def exists(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def delete(self, digest: str) -> None:
        # Raw objects are immutable and content-addressed; deletion is explicit and rare.
        self._path(digest).unlink(missing_ok=True)
