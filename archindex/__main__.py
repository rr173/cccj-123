"""Command-line entry points for archindex."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from archindex.assembly import (
        DEFAULT_BATCH_SIZE,
        DEFAULT_CACHE_SIZE_KB,
        DEFAULT_MAX_ROW_PAYLOAD_BYTES,
        DEFAULT_WRITE_BUFFER_BYTES,
        assemble_nested,
    )
    from archindex.errors import ArchiveIndexError
    from archindex.indexer import index_archive
    from archindex.ingestion import ingest_sources
else:
    from .assembly import (
        DEFAULT_BATCH_SIZE,
        DEFAULT_CACHE_SIZE_KB,
        DEFAULT_MAX_ROW_PAYLOAD_BYTES,
        DEFAULT_WRITE_BUFFER_BYTES,
        assemble_nested,
    )
    from .errors import ArchiveIndexError
    from .indexer import index_archive
    from .ingestion import ingest_sources


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m archindex",
        description="Archive indexing, streaming CSV-to-SQLite ingestion "
                    "and nested JSONL assembly")
    sub = parser.add_subparsers(dest="command", required=True)

    index_cmd = sub.add_parser("index", help="index an archive and print JSON")
    index_cmd.add_argument("archive")

    ingest = sub.add_parser(
        "ingest", help="stream selected CSV archive members into SQLite")
    ingest.add_argument("--contract", required=True, help="field contract JSON")
    ingest.add_argument("--manifest", required=True, help="source manifest JSON")
    ingest.add_argument("--output", required=True, help="new SQLite path")
    ingest.add_argument(
        "--diagnostic", help="optional JSON diagnostic path on failure")

    assemble = sub.add_parser(
        "assemble",
        help="assemble a nested JSONL delivery file from an existing "
             "SQLite database and an assembly plan")
    assemble.add_argument("--database", required=True,
                          help="existing SQLite database (opened read-only)")
    assemble.add_argument("--plan", required=True,
                          help="assembly plan JSON file")
    assemble.add_argument("--output", required=True,
                          help="new JSONL path (must not exist)")
    assemble.add_argument("--temp-dir",
                          help="directory for scratch structures "
                               "(default: the output directory)")
    assemble.add_argument("--cache-size-kb", type=int,
                          default=DEFAULT_CACHE_SIZE_KB,
                          help="scratch SQLite cache size in KiB "
                               f"(default {DEFAULT_CACHE_SIZE_KB})")
    assemble.add_argument("--batch-size", type=int,
                          default=DEFAULT_BATCH_SIZE,
                          help="rows fetched/inserted per batch "
                               f"(default {DEFAULT_BATCH_SIZE})")
    assemble.add_argument("--max-row-payload-bytes", type=int,
                          default=DEFAULT_MAX_ROW_PAYLOAD_BYTES,
                          help="single-row projected payload cap "
                               f"(default {DEFAULT_MAX_ROW_PAYLOAD_BYTES})")
    assemble.add_argument("--write-buffer-bytes", type=int,
                          default=DEFAULT_WRITE_BUFFER_BYTES,
                          help="JSONL write buffer size "
                               f"(default {DEFAULT_WRITE_BUFFER_BYTES})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "index":
            result = index_archive(args.archive)
            json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0
        if args.command == "ingest":
            outcome = ingest_sources(
                Path(args.contract), Path(args.manifest), Path(args.output),
                diagnostic_path=Path(args.diagnostic)
                if args.diagnostic else None)
            json.dump({
                "database_path": outcome.database_path,
                "source_count": outcome.source_count,
                "logical_records": outcome.logical_records,
                "accepted": outcome.accepted,
                "quarantined": outcome.quarantined,
            }, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0
        if args.command == "assemble":
            outcome = assemble_nested(
                Path(args.plan), Path(args.database), Path(args.output),
                temp_dir=Path(args.temp_dir) if args.temp_dir else None,
                cache_size_kb=args.cache_size_kb,
                batch_size=args.batch_size,
                max_row_payload_bytes=args.max_row_payload_bytes,
                write_buffer_bytes=args.write_buffer_bytes)
            json.dump({
                "database_path": outcome.database_path,
                "output_path": outcome.output_path,
                "roots": outcome.roots,
                "node_rows": outcome.node_rows,
                "output_bytes": outcome.output_bytes,
                "elapsed_seconds": round(outcome.elapsed_seconds, 3),
            }, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0
    except (ArchiveIndexError, FileExistsError, FileNotFoundError, OSError,
            ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
