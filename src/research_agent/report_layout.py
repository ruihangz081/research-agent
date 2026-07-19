"""Compatibility wrappers for the unified report formatting pipeline."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config
from .llm import LLMClient
from .report_formatting import find_latex_engine, generate_report_artifacts

FILE_FINAL_REPORT_TEX = config.FILE_FINAL_REPORT_TEX
# The legacy typeset filename now resolves to the single formal PDF.
FILE_FINAL_REPORT_TYPESET_PDF = config.FILE_FINAL_REPORT_PDF


async def generate_typeset_artifacts(
    *,
    topic: str,
    project_dir: Path,
    final_report_path: Path,
    client: LLMClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return await generate_report_artifacts(
        topic=topic,
        project_dir=project_dir,
        final_report_path=final_report_path,
        client=client,
        force=force,
    )


__all__ = [
    "FILE_FINAL_REPORT_TEX",
    "FILE_FINAL_REPORT_TYPESET_PDF",
    "find_latex_engine",
    "generate_typeset_artifacts",
]
