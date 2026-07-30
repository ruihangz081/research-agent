"""材料运行时共享与失效测试（backlog 第 9 项）。

原问题：build_runtime() 每次调用新开 SQLite 连接，6 个模块各自决定是否 close()，
混合生命周期让"谁负责关闭"无法推理。
"""
from pathlib import Path

import pytest

from research_agent import config
from research_agent.sources import runtime
from research_agent.sources.api import build_runtime


@pytest.fixture(autouse=True)
def clean_runtime():
    runtime.reset_runtime()
    yield
    runtime.reset_runtime()


def test_same_dir_shares_one_runtime(tmp_path: Path) -> None:
    first = runtime.get_runtime(tmp_path)
    second = runtime.get_runtime(tmp_path)

    assert first[0] is second[0]
    assert first[1] is second[1]
    assert first[0].repository is second[0].repository


def test_build_runtime_returns_the_shared_instance(tmp_path: Path) -> None:
    """旧入口 build_runtime 现在也走共享缓存，不再每次建连。"""
    assert build_runtime(tmp_path)[0] is runtime.get_runtime(tmp_path)[0]


def test_different_dirs_get_separate_runtimes(tmp_path: Path) -> None:
    first = runtime.get_runtime(tmp_path / "a")
    second = runtime.get_runtime(tmp_path / "b")

    assert first[0] is not second[0]


def test_equivalent_paths_resolve_to_one_runtime(tmp_path: Path) -> None:
    """路径写法不同但指向同一目录时不应建两条连接。"""
    nested = tmp_path / "sources"
    nested.mkdir()
    first = runtime.get_runtime(nested)
    second = runtime.get_runtime(tmp_path / "sources" / "." )

    assert first[0] is second[0]


def test_shared_connection_stays_usable_across_calls(tmp_path: Path) -> None:
    """此前的 bug 形态：一个调用方 close() 后，其他调用方拿到已关闭的连接。"""
    service = runtime.get_service(tmp_path)
    source = service.register_bytes("p", "a.txt", b"Revenue was 42 million").source
    service.parse_source("p", source.source_id)
    service.index_source("p", source.source_id)
    service.activate("p", source.source_id)

    # 另一处代码再取一次并继续读写，不应因连接被关闭而失败
    again = runtime.get_service(tmp_path)
    assert again.get_source("p", source.source_id).source_id == source.source_id
    assert again.repository.list_sources("p")


def test_reset_specific_dir_keeps_others(tmp_path: Path) -> None:
    first = runtime.get_runtime(tmp_path / "a")
    second = runtime.get_runtime(tmp_path / "b")

    runtime.reset_runtime(tmp_path / "a")

    assert runtime.get_runtime(tmp_path / "a")[0] is not first[0]
    assert runtime.get_runtime(tmp_path / "b")[0] is second[0]


def test_reset_all_discards_every_runtime(tmp_path: Path) -> None:
    first = runtime.get_runtime(tmp_path / "a")
    second = runtime.get_runtime(tmp_path / "b")

    runtime.reset_runtime()

    assert runtime.get_runtime(tmp_path / "a")[0] is not first[0]
    assert runtime.get_runtime(tmp_path / "b")[0] is not second[0]


def test_create_runtime_bypasses_cache(tmp_path: Path) -> None:
    """需要独立连接的场景（例如备份）仍可绕过缓存。"""
    shared = runtime.get_runtime(tmp_path)
    standalone = runtime.create_runtime(tmp_path)

    assert standalone[0] is not shared[0]
    standalone[0].repository.close()
    # 关闭独立连接不影响共享连接
    assert runtime.get_service(tmp_path).repository.list_sources("p") == []


def test_reset_tolerates_already_closed_connection(tmp_path: Path) -> None:
    """外部代码提前关闭连接时，reset 仍应清理缓存而不抛异常。"""
    service = runtime.get_service(tmp_path)
    service.repository.close()

    runtime.reset_runtime()

    assert runtime.get_service(tmp_path) is not service


def test_agent_tools_and_orchestrator_share_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具层与编排层必须落到同一条连接，否则证据写入与门禁读取会错位。"""
    from research_agent.sources.runtime import get_service
    from research_agent.tools.builtins import project_sources

    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    runtime.reset_runtime()

    assert project_sources._service() is get_service(config.SOURCE_DATA_DIR)
