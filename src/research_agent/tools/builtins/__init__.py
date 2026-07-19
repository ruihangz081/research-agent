"""内置工具注册。"""
from .read_file import read_file  # noqa: F401
from .write_file import write_file  # noqa: F401
from .web_search import web_search  # noqa: F401
from .web_fetch import web_fetch  # noqa: F401
from .project_sources import (  # noqa: F401
    list_project_sources, search_project_sources, read_project_source, inspect_source_evidence,
    record_project_evidence,
)


def _register_all() -> None:
    """触发所有内置工具的导入注册（被 tools/__init__.py 调用）。"""
    pass
