"""内置工具：Write — 写入文件内容。"""
from pathlib import Path

from ..registry import default_registry


@default_registry.tool(
    name="Write",
    description=(
        "Write content to a file inside the current project directory, "
        "creating directories as needed. The path must stay within the project; "
        "paths outside it are rejected."
    ),
)
async def write_file(file_path: str, content: str) -> str:
    """Write content to a file.

    Args:
        file_path: Path to the file to write, restricted to the project directory.
        content: The text content to write.
    """
    try:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} characters to {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"
