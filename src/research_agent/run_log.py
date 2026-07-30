"""按项目持久化的执行日志。

内存中的 `JOBS` 在服务重启后清零，而排查上一次失败恰恰最需要那段日志。
这里把每条阶段进展追加写入项目目录下的 `run_log.jsonl`，重启后可回读。

格式：每行一个 JSON 对象 {"ts": ISO8601, "time": "HH:MM:SS", "message": str}
选用 JSONL 而非 JSON 数组，是为了让追加写入无需读取和重写整个文件。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

FILE_RUN_LOG = "run_log.jsonl"

# 单个项目回读与保留的日志条数上限
MAX_ENTRIES = 300
# 超过该行数时压实文件，避免长期运行后无限增长
_COMPACT_THRESHOLD = MAX_ENTRIES * 4


def log_path(project_dir: Path) -> Path:
    return project_dir / FILE_RUN_LOG


def append(project_dir: Path, message: str) -> dict[str, str]:
    """追加一条日志并返回该条目。写盘失败不影响主流程。"""
    now = datetime.now()
    entry = {
        "ts": now.isoformat(),
        "time": now.strftime("%H:%M:%S"),
        "message": message,
    }
    path = log_path(project_dir)
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _compact_if_needed(path)
    except OSError:
        # 日志是辅助信息，写盘失败不应中断调研
        pass
    return entry


def read(project_dir: Path, limit: int = MAX_ENTRIES) -> list[dict[str, str]]:
    """回读最近 limit 条日志。文件缺失或损坏时返回已能解析的部分。"""
    path = log_path(project_dir)
    if not path.is_file():
        return []
    entries: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 跳过被截断的尾行
                if isinstance(value, dict) and "message" in value:
                    entries.append(
                        {
                            "time": str(value.get("time", "")),
                            "message": str(value["message"]),
                            "ts": str(value.get("ts", "")),
                        }
                    )
    except OSError:
        return entries[-limit:]
    return entries[-limit:]


def _compact_if_needed(path: Path) -> None:
    """行数超阈值时只保留最近 MAX_ENTRIES 条。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= _COMPACT_THRESHOLD:
            return
        tail = lines[-MAX_ENTRIES:]
        temp = path.with_suffix(".jsonl.tmp")
        temp.write_text("".join(tail), encoding="utf-8")
        temp.replace(path)
    except OSError:
        pass
