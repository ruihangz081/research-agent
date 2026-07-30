"""进程级材料运行时缓存。

`build_runtime()` 每次调用都会新开一条 SQLite 连接。它在 6 个模块里被调用，
此前生命周期管理方式各不相同：有的显式 `close()`、有的用 `lru_cache` 保持单例、
Web 层则是模块级长连接。WAL 模式下多连接能工作，但混合模式让"谁负责关闭"
变得不可推理，也容易在 agent 工具里反复建连。

这里按数据目录缓存运行时，全进程共享同一条连接：
  - `get_runtime(data_dir)` —— 取（必要时创建）该目录的 (service, queue)
  - `get_service(data_dir)` —— 只要 service 的便捷入口
  - `reset_runtime()` —— 关闭并清空缓存（测试切换 tmp 目录、或运行时改配置后调用）

调用方不再负责关闭连接；连接随进程生命周期存在。
"""
from __future__ import annotations

import threading
from pathlib import Path

from .jobs import JobQueue
from .repository import SQLiteRepository
from .service import SourceService
from .storage import LocalObjectStore

_LOCK = threading.RLock()
_RUNTIMES: dict[str, tuple[SourceService, JobQueue]] = {}


def _key(data_dir: str | Path) -> str:
    return str(Path(data_dir).expanduser().resolve())


def create_runtime(data_dir: str | Path) -> tuple[SourceService, JobQueue]:
    """新建一套运行时（不进缓存）。需要独立连接的场景（如备份）可用。"""
    root = Path(data_dir)
    repository = SQLiteRepository(root / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(root / "objects"))
    return service, JobQueue(repository)


def get_runtime(data_dir: str | Path) -> tuple[SourceService, JobQueue]:
    """取该数据目录的共享运行时，首次调用时创建。"""
    key = _key(data_dir)
    with _LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = create_runtime(data_dir)
            _RUNTIMES[key] = runtime
        return runtime


def get_service(data_dir: str | Path) -> SourceService:
    return get_runtime(data_dir)[0]


def reset_runtime(data_dir: str | Path | None = None) -> None:
    """关闭并丢弃缓存的运行时。

    data_dir 为 None 时清空全部。测试在切换临时目录之间、或运行时修改
    SOURCE_DATA_DIR 后需要调用，避免继续用指向旧路径的连接。
    """
    with _LOCK:
        keys = [_key(data_dir)] if data_dir is not None else list(_RUNTIMES)
        for key in keys:
            runtime = _RUNTIMES.pop(key, None)
            if runtime is None:
                continue
            try:
                runtime[0].repository.close()
            except Exception:
                # 关闭失败不应阻止缓存清理
                pass
