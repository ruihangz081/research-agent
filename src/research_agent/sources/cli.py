"""Source Center CLI; every operation delegates to SourceService."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api import build_runtime
from .jobs import SourceWorker
from .search import SearchFilters


def _json(value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _runtime(args):
    return build_runtime(args.data_dir)


def upload(args) -> int:
    service, queue = _runtime(args)
    items = []
    for value in args.files:
        path = Path(value).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        result = service.register_bytes(args.project_id, path.name, path.read_bytes())
        job = queue.enqueue(args.project_id, "ingest", f"ingest:{args.project_id}:{result.source.sha256}", result.source.source_id)
        items.append({"source": result.source.model_dump(mode="json"), "job": job.model_dump(mode="json"), "deduplicated": result.deduplicated})
    _json({"items": items})
    return 0


def list_sources(args) -> int:
    service, _ = _runtime(args)
    _json({"items": [item.model_dump(mode="json") for item in service.list_sources(args.project_id, args.all_versions)]})
    return 0


def process(args) -> int:
    service, _ = _runtime(args)
    worker = SourceWorker(service, worker_id="cli-worker")
    jobs = []
    while job := worker.run_once():
        jobs.append(job.model_dump(mode="json"))
        if args.once:
            break
    _json({"jobs": jobs})
    return 0


def search(args) -> int:
    service, _ = _runtime(args)
    filters = SearchFilters(include_inactive=args.include_inactive)
    _json({"items": [item.model_dump(mode="json") for item in service.search(args.project_id, args.query, limit=args.limit, filters=filters)]})
    return 0


def read(args) -> int:
    service, _ = _runtime(args)
    if args.chunk_id:
        value = service.read_chunk(args.project_id, args.chunk_id)
        _json({key: item.model_dump(mode="json") if hasattr(item, "model_dump") else item for key, item in value.items()})
    else:
        source = service.get_source(args.project_id, args.source_id)
        document = service.repository.get_document(args.source_id, args.project_id)
        _json({"source": source.model_dump(mode="json"), "document": document.model_dump(mode="json") if document else None})
    return 0


def set_state(args) -> int:
    service, _ = _runtime(args)
    source = service.activate(args.project_id, args.source_id) if args.action == "activate" else service.archive(args.project_id, args.source_id)
    _json(source)
    return 0


def inspect(args) -> int:
    service, _ = _runtime(args)
    source = service.get_source(args.project_id, args.source_id)
    _json({"source": source.model_dump(mode="json"), "audit": [event.model_dump(mode="json") for event in service.repository.audit_events(args.project_id, args.source_id)],
           "evidence": [evidence.model_dump(mode="json") for evidence in service.repository.list_evidence(args.project_id, args.source_id)]})
    return 0


def configure_parser(subparsers) -> None:
    root = subparsers.add_parser("sources", help="manage project research materials")
    root.add_argument("--data-dir", default=".data/sources")
    commands = root.add_subparsers(dest="source_command", required=True)

    command = commands.add_parser("upload")
    command.add_argument("project_id")
    command.add_argument("files", nargs="+")
    command.set_defaults(func=upload)

    command = commands.add_parser("list")
    command.add_argument("project_id")
    command.add_argument("--all-versions", action="store_true")
    command.set_defaults(func=list_sources)

    command = commands.add_parser("process")
    command.add_argument("--once", action="store_true")
    command.set_defaults(func=process)

    command = commands.add_parser("search")
    command.add_argument("project_id")
    command.add_argument("query")
    command.add_argument("--limit", type=int, default=10)
    command.add_argument("--include-inactive", action="store_true")
    command.set_defaults(func=search)

    command = commands.add_parser("read")
    command.add_argument("project_id")
    command.add_argument("source_id")
    command.add_argument("--chunk-id")
    command.set_defaults(func=read)

    for action in ("activate", "archive"):
        command = commands.add_parser(action)
        command.add_argument("project_id")
        command.add_argument("source_id")
        command.set_defaults(func=set_state, action=action)

    command = commands.add_parser("inspect")
    command.add_argument("project_id")
    command.add_argument("source_id")
    command.set_defaults(func=inspect)
