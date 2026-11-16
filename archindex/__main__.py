"""Command-line entry points for archindex."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from archindex._vendor import ensure_numpy

    ensure_numpy()
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
    from archindex.labelread import (
        CellDataError,
        load_descriptor,
    )
    from archindex.relgen import (
        DEFAULT_BATCH_SIZE as GEN_DEFAULT_BATCH_SIZE,
        DEFAULT_CACHE_SIZE_KB as GEN_DEFAULT_CACHE_SIZE_KB,
        RowFactory,
        generate_database,
        load_recipe,
    )
else:
    from ._vendor import ensure_numpy

    ensure_numpy()
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
    from .labelread import (
        CellDataError,
        load_descriptor,
    )
    from .relgen import (
        DEFAULT_BATCH_SIZE as GEN_DEFAULT_BATCH_SIZE,
        DEFAULT_CACHE_SIZE_KB as GEN_DEFAULT_CACHE_SIZE_KB,
        RowFactory,
        generate_database,
        load_recipe,
    )


def _parse_seed(text: str) -> int:
    """Parse a decimal or 0x-prefixed seed, keeping it inside [0, 2**64)."""
    try:
        seed = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"seed must be an integer (decimal or 0x...), got {text!r}")
    if not 0 <= seed <= 2 ** 64 - 1:
        raise argparse.ArgumentTypeError(
            f"seed must lie in [0, 2**64), got {seed}")
    return seed


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

    gen_sample = sub.add_parser(
        "gen-sample",
        help="print addressed rows of a virtual table described by a "
             "generation recipe (no database is read or written)")
    gen_sample.add_argument("--recipe", required=True,
                            help="generation recipe JSON file")
    gen_sample.add_argument("--seed", required=True, type=_parse_seed,
                            help="integer seed in [0, 2**64), decimal or "
                                 "0x-prefixed")
    gen_sample.add_argument("--table", required=True,
                            help="table name from the recipe")
    sel = gen_sample.add_mutually_exclusive_group(required=True)
    sel.add_argument("--row", type=int, help="single zero-based row number")
    sel.add_argument("--rows", metavar="START:STOP",
                     help="contiguous row-number range [START, STOP)")

    gen_db = sub.add_parser(
        "gen-db",
        help="materialise a whole generation recipe into a new SQLite "
             "database with real primary/foreign keys")
    gen_db.add_argument("--recipe", required=True,
                        help="generation recipe JSON file")
    gen_db.add_argument("--seed", required=True, type=_parse_seed,
                        help="integer seed in [0, 2**64), decimal or "
                             "0x-prefixed")
    gen_db.add_argument("--output", required=True,
                        help="new SQLite path (must not exist)")
    gen_db.add_argument("--cache-size-kb", type=int,
                        default=GEN_DEFAULT_CACHE_SIZE_KB,
                        help="SQLite page cache in KiB "
                             f"(default {GEN_DEFAULT_CACHE_SIZE_KB})")
    gen_db.add_argument("--batch-size", type=int,
                        default=GEN_DEFAULT_BATCH_SIZE,
                        help="rows inserted per batch "
                             f"(default {GEN_DEFAULT_BATCH_SIZE})")

    label_inspect = sub.add_parser(
        "label-inspect",
        help="open a label-coordinate descriptor and print its axes, visible "
             "labels, sources and variables without reading array payloads")
    label_inspect.add_argument("descriptor", help="label-read descriptor JSON")

    label_read = sub.add_parser(
        "label-read",
        help="read aligned grid cells from a label-coordinate descriptor as JSONL")
    label_read.add_argument("descriptor", help="label-read descriptor JSON")
    label_read.add_argument("--request", required=True,
                            help="read request JSON: "
                                 '{"variables":[...], "selection":{axis:[labels]},'
                                 ' "mode":"strict|priority", "priority":[ids]}')
    label_read.add_argument("--batch-size", type=int,
                            help="max cells materialised per output batch")
    return parser


def _run_label_read(store, request: dict, batch_size) -> int:
    variables = request.get("variables")
    selection = request.get("selection")
    mode = request.get("mode", "strict")
    priority = request.get("priority")
    # Validate the whole request before the first cell is emitted.  The
    # generator receives the ORIGINAL selection and re-validates it itself;
    # passing the already-resolved dict back would double-parse it.
    info_list, _resolved, mode, priority = store._prepare_request(
        variables, selection, mode, priority)
    variable_names = [info.name for info in info_list]
    written = 0
    totals = {name: {"value": 0, "masked": 0, "absent": 0}
              for name in variable_names}
    stream = sys.stdout
    # Emit cell by cell with an immediate flush: if a later cell raises a
    # CellDataError, every previously emitted cell has truly left the
    # process as valid JSONL, giving an observable (but never "successful")
    # partial stream.  --batch-size only groups the flush cadence; cell
    # materialisation itself stays one-at-a-time through the generator.
    since_flush = 0
    for cell in store.iter_cells(variable_names, selection, mode=mode,
                                 priority=priority):
        for name in variable_names:
            totals[name][cell["variables"][name]["state"]] += 1
        stream.write(json.dumps(cell, ensure_ascii=False,
                                separators=(",", ":")))
        stream.write("\n")
        written += 1
        since_flush += 1
        if not batch_size or since_flush >= batch_size:
            stream.flush()
            since_flush = 0
    stream.flush()
    summary = {"status": "ok", "cells": written, "variables": totals}
    stream.write(json.dumps({"summary": summary}, ensure_ascii=False,
                            separators=(",", ":")))
    stream.write("\n")
    stream.flush()
    return 0


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
        if args.command == "gen-sample":
            recipe = load_recipe(Path(args.recipe))
            factory = RowFactory(recipe, args.seed)
            if args.row is not None:
                addresses = [(args.row, factory.row(args.table, args.row))]
            else:
                try:
                    start_s, stop_s = args.rows.split(":", 1)
                    start, stop = int(start_s, 10), int(stop_s, 10)
                except ValueError:
                    print(f"error: --rows expects START:STOP, got "
                          f"{args.rows!r}", file=sys.stderr)
                    return 2
                addresses = list(factory.rows(args.table, start, stop))
            json.dump({
                "table": args.table,
                "seed": args.seed,
                "rows": [{"row": number, "values": values}
                         for number, values in addresses],
                "rows_computed": factory.rows_computed,
            }, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0
        if args.command == "gen-db":
            outcome = generate_database(
                load_recipe(Path(args.recipe)), args.seed,
                Path(args.output), cache_size_kb=args.cache_size_kb,
                batch_size=args.batch_size)
            json.dump(outcome.to_dict(), sys.stdout, ensure_ascii=False,
                      indent=2)
            sys.stdout.write("\n")
            return 0
        if args.command == "label-inspect":
            store = load_descriptor(Path(args.descriptor))
            json.dump(store.inspect(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0
        if args.command == "label-read":
            store = load_descriptor(Path(args.descriptor))
            try:
                with open(args.request, "r", encoding="utf-8") as handle:
                    request = json.load(handle)
            except OSError as exc:
                print(f"error: cannot read request {args.request!r}: {exc}",
                      file=sys.stderr)
                return 2
            except json.JSONDecodeError as exc:
                print(f"error: request is not valid JSON: {exc}",
                      file=sys.stderr)
                return 2
            if not isinstance(request, dict):
                print("error: read request must be a JSON object",
                      file=sys.stderr)
                return 2
            try:
                return _run_label_read(store, request, args.batch_size)
            except CellDataError as exc:
                # Cells emitted before the offending one are already valid
                # JSONL; flush them so the partial stream is observable,
                # then fail nonzero without any success summary marker.
                sys.stdout.flush()
                print(f"error: {exc}", file=sys.stderr)
                return 2
    except (ArchiveIndexError, FileExistsError, FileNotFoundError, OSError,
            ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
