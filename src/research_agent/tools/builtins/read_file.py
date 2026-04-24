"""内置工具：Read — 读取文件内容。"""
from pathlib import Path

from ..registry import default_registry


@default_registry.tool(name="Read", description="Read the contents of a file at the given absolute path.")
async def read_file(file_path: str) -> str:
    """Read a file and return its contents.

    Args:
        file_path: Absolute path to the file to read.
    """
    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found: {file_path}"
    if not path.is_file():
        return f"Error: Not a file: {file_path}"
    try:
        content = path.read_text(encoding="utf-8")
        # 超长文件截断，避免上下文爆炸
        if len(content) > 50_000:
            return content[:50_000] + f"\n\n[... truncated, total {len(content)} chars]"
        return content
    except UnicodeDecodeError:
        return f"Error: File is not UTF-8 text: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"
