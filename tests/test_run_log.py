"""执行日志持久化测试（backlog 第 3 项）。"""
from pathlib import Path

from research_agent import run_log


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    run_log.append(tmp_path, "第一条")
    run_log.append(tmp_path, "第二条")

    entries = run_log.read(tmp_path)

    assert [item["message"] for item in entries] == ["第一条", "第二条"]
    assert all(item["time"] for item in entries)
    assert all(item["ts"] for item in entries)


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    assert run_log.read(tmp_path / "nope") == []


def test_append_creates_project_dir(tmp_path: Path) -> None:
    target = tmp_path / "fresh-project"
    run_log.append(target, "创建目录")
    assert run_log.log_path(target).is_file()


def test_read_skips_corrupted_lines(tmp_path: Path) -> None:
    """尾行被截断（例如进程在写入中途被杀）时仍能读出完整条目。"""
    run_log.append(tmp_path, "完整条目")
    path = run_log.log_path(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "broken", "mess')

    entries = run_log.read(tmp_path)

    assert [item["message"] for item in entries] == ["完整条目"]


def test_read_respects_limit(tmp_path: Path) -> None:
    for index in range(10):
        run_log.append(tmp_path, f"条目{index}")

    entries = run_log.read(tmp_path, limit=3)

    assert [item["message"] for item in entries] == ["条目7", "条目8", "条目9"]


def test_file_is_compacted_when_it_grows_too_large(tmp_path: Path) -> None:
    """越过压实阈值后文件被截短，避免长期运行无限增长。"""
    total = run_log._COMPACT_THRESHOLD + 5
    for index in range(total):
        run_log.append(tmp_path, f"条目{index}")

    lines = run_log.log_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()

    # 压实在越过阈值那次写入时发生，之后又追加了几条，故不等于 MAX_ENTRIES
    assert len(lines) < total
    assert len(lines) <= run_log._COMPACT_THRESHOLD
    entries = run_log.read(tmp_path)
    assert entries[-1]["message"] == f"条目{total - 1}"
