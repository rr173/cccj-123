"""Command-line entry points for archindex."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from archindex.errors import ArchiveIndexError
    from archindex.indexer import index_archive
    from archindex.ingestion import ingest_sources
else:
    from .errors import ArchiveIndexError
    from .indexer import index_archive
    from .ingestion import ingest_sources


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m archindex",
        description="Archive indexing and streaming CSV-to-SQLite ingestion")
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
    except (ArchiveIndexError, FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
