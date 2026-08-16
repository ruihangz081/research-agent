"""R5：文件工具的项目目录边界隔离。

验证 Read/Write 工具无论收到绝对路径、`..` 还是符号链接，都不能逃逸项目目录。
"""
from __future__ import annotations

import pytest

from research_agent.agent_loop.loop import (
    _resolve_file_path,
    _resolve_file_paths,
    _execute_tool_call,
)
from research_agent.llm.types import FunctionCallInfo, ToolCallInfo
from research_agent.tools.registry import ToolRegistry


def test_relative_path_resolves_within_cwd(tmp_path) -> None:
    assert _resolve_file_path("outline.md", str(tmp_path)) == str(
        (tmp_path / "outline.md").resolve()
    )


def test_absolute_path_within_cwd_is_allowed(tmp_path) -> None:
    target = tmp_path / "data" / "report.md"
    assert _resolve_file_path(str(target), str(tmp_path)) == str(target.resolve())


def test_parent_traversal_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="escapes the project directory"):
        _resolve_file_path("../outside.md", str(tmp_path))


def test_absolute_path_outside_cwd_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="escapes the project directory"):
        _resolve_file_path("/etc/passwd", str(tmp_path))


def test_symlink_escape_is_rejected(tmp_path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    with pytest.raises(ValueError, match="escapes the project directory"):
        _resolve_file_path("link.md", str(tmp_path))


def test_resolve_file_paths_only_touches_path_keys(tmp_path) -> None:
    args = {"file_path": "a.md", "path": "b.md", "content": "hello", "mode": "w"}
    resolved = _resolve_file_paths(args, str(tmp_path))
    assert resolved["file_path"] == str((tmp_path / "a.md").resolve())
    assert resolved["path"] == str((tmp_path / "b.md").resolve())
    # 非路径字段原样保留
    assert resolved["content"] == "hello"
    assert resolved["mode"] == "w"


def test_non_string_path_value_is_left_untouched(tmp_path) -> None:
    args = {"file_path": None, "content": "x"}
    resolved = _resolve_file_paths(args, str(tmp_path))
    assert resolved["file_path"] is None


@pytest.mark.anyio
async def test_execute_tool_call_returns_error_on_escape(tmp_path) -> None:
    registry = ToolRegistry()

    @registry.tool(name="Write")
    async def write_tool(file_path: str, content: str) -> str:
        return f"wrote {file_path}"

    tc = ToolCallInfo(
        id="call-1",
        function=FunctionCallInfo(
            name="Write",
            arguments='{"file_path": "../../etc/evil.md", "content": "x"}',
        ),
    )
    result = await _execute_tool_call(tc, registry, str(tmp_path))
    assert result.startswith("Error:")
    assert "escapes the project directory" in result


@pytest.mark.anyio
async def test_execute_tool_call_allows_in_project_write(tmp_path) -> None:
    registry = ToolRegistry()
    written: list[str] = []

    @registry.tool(name="Write")
    async def write_tool(file_path: str, content: str) -> str:
        written.append(file_path)
        return f"wrote {file_path}"

    tc = ToolCallInfo(
        id="call-1",
        function=FunctionCallInfo(
            name="Write",
            arguments='{"file_path": "report.md", "content": "x"}',
        ),
    )
    result = await _execute_tool_call(tc, registry, str(tmp_path))
    assert result == f"wrote {(tmp_path / 'report.md').resolve()}"
    assert written == [str((tmp_path / "report.md").resolve())]
