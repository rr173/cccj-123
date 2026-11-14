"""One-off, streaming CSV-to-SQLite ingestion."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import (
    Contract,
    FieldContract,
    convert_cell,
    key_value,
    load_contract,
    parse_contract,
    storage_value,
)
from .csvstream import CsvRecord, iter_csv_records
from .errors import (
    ContractValidationError,
    CorruptArchiveError,
    IngestionError,
    ManifestValidationError,
    SourceStructureError,
)
from .indexer import index_archive
from .manifest import SourceSpec, load_manifest, parse_manifest
from .records import IndexResult, Limits
from .streams import open_verified_member, resolve_member

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PreparedSource:
    source_id: int
    spec: SourceSpec
    index: IndexResult
    record: Any
    source_columns: tuple[str, ...]
    target_by_source: dict[str, str]
    ordered_sources: list[str]


@dataclass(frozen=True, slots=True)
class IngestionResult:
    database_path: str
    logical_records: int
    accepted: int
    quarantined: int
    source_count: int


def _quote_ident(name: str) -> str:
    # Identifiers are already restricted to [A-Za-z_][A-Za-z0-9_]*.
    return '"' + name.replace('"', '""') + '"'


def _load_json_or_objects(
    contract: str | Path | Contract | dict[str, Any],
    manifest: str | Path | list[dict[str, Any]],
) -> tuple[Contract, list[SourceSpec]]:
    if isinstance(contract, Contract):
        parsed_contract = contract
    elif isinstance(contract, dict):
        parsed_contract = parse_contract(contract)
    else:
        parsed_contract = load_contract(contract)

    if isinstance(manifest, list):
        specs = parse_manifest(manifest, parsed_contract)
    else:
        specs = load_manifest(manifest, parsed_contract)
    return parsed_contract, specs


def _index_archives(specs: Iterable[SourceSpec],
                    limits: Limits | None) -> dict[str, IndexResult]:
    indices: dict[str, IndexResult] = {}
    for spec in specs:
        archive_path = os.path.abspath(spec.archive)
        if archive_path not in indices:
            indices[archive_path] = index_archive(archive_path, limits)
    return indices


def _read_header(source: PreparedSource) -> CsvRecord:
    with open_verified_member(
            source.index, source.record, verify_on_close=False) as binary:
        parser = iter_csv_records(
            binary, source.spec.encoding, source.spec.delimiter,
            source_id=source.source_id, member=source.spec.member,
            occurrence=source.record.occurrence)
        try:
            header = next(iter(parser))
        except StopIteration:
            raise SourceStructureError(
                f"source {source.source_id} ({source.spec.member}#"
                f"{source.record.occurrence}) has no CSV header")
        except CorruptArchiveError:
            raise
    return header


def prepare_sources(specs: list[SourceSpec],
                    indices: dict[str, IndexResult],
                    contract: Contract) -> list[PreparedSource]:
    """Resolve and validate all source headers before accepting data rows."""
    prepared: list[PreparedSource] = []
    for source_id, spec in enumerate(specs, start=1):
        archive_path = os.path.abspath(spec.archive)
        index = indices[archive_path]
        record = resolve_member(index, spec.member, spec.occurrence)
        if record.is_dir or record.kind != "file":
            raise SourceStructureError(
                f"source {source_id}: {spec.member!r} is a {record.kind}; "
                "directories and links cannot be CSV data sources")
        header = _read_header(PreparedSource(
            source_id=source_id, spec=spec, index=index, record=record,
            source_columns=(), target_by_source={}, ordered_sources=[]))

        columns = header.fields
        if len(set(columns)) != len(columns):
            dupes = sorted({x for x in columns if columns.count(x) > 1})
            raise SourceStructureError(
                f"source {source_id} ({spec.member}#"
                f"{record.occurrence}): duplicate source header(s): {dupes}")
        unknown_mapping = [s for s in spec.mapping if s not in columns]
        if unknown_mapping:
            raise SourceStructureError(
                f"source {source_id}: mapping references absent source "
                f"column(s): {unknown_mapping}")
        target_by_source = dict(spec.mapping)
        extra = [c for c in columns if c not in target_by_source]
        if extra and not spec.ignore_extra_columns:
            raise SourceStructureError(
                f"source {source_id}: unmapped extra source column(s): {extra}; "
                "set ignore_extra_columns=true to skip them explicitly")

        mapped_targets = list(target_by_source.values())
        if len(set(mapped_targets)) != len(mapped_targets):  # Manifest also checks.
            raise SourceStructureError(
                f"source {source_id}: duplicate target column in mapping")

        missing: list[str] = []
        for fld in contract.fields:
            if fld.name in mapped_targets:
                continue
            if fld.default is not None or fld.nullable:
                continue
            missing.append(fld.name)
        if missing:
            raise SourceStructureError(
                f"source {source_id}: required target column(s) have neither "
                f"a source mapping nor a default: {missing}")

        prepared.append(PreparedSource(
            source_id=source_id,
            spec=spec,
            index=index,
            record=record,
            source_columns=tuple(columns),
            target_by_source=target_by_source,
            ordered_sources=columns,
        ))
    return prepared


def _create_database(path: str, contract: Contract) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    columns = [
        "source_id INTEGER NOT NULL",
        "member TEXT NOT NULL",
        "occurrence INTEGER NOT NULL",
        "record_number INTEGER NOT NULL",
        "start_physical_line INTEGER NOT NULL",
        "end_physical_line INTEGER NOT NULL",
    ]
    for fld in contract.fields:
        sql_type = {
            "text": "TEXT",
            "integer": "INTEGER",
            "decimal": "TEXT",
            "timestamp": "TEXT",
        }[fld.type]
        nullable = "" if fld.nullable else " NOT NULL"
        columns.append(f"{_quote_ident(fld.name)} {sql_type}{nullable}")
    pk_cols = ", ".join(_quote_ident(x) for x in contract.primary_key)
    conn.execute(
        f"CREATE TABLE {_quote_ident(contract.table)} ("
        + ", ".join(columns)
        + f", PRIMARY KEY ({pk_cols})) WITHOUT ROWID")
    conn.execute(
        "CREATE TABLE quarantine ("
        "quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_id INTEGER NOT NULL, member TEXT NOT NULL, "
        "occurrence INTEGER NOT NULL, record_number INTEGER NOT NULL, "
        "start_physical_line INTEGER NOT NULL, "
        "end_physical_line INTEGER NOT NULL, "
        "raw_cells TEXT NOT NULL, reason TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE source_summary ("
        "source_id INTEGER PRIMARY KEY, archive TEXT NOT NULL, "
        "member TEXT NOT NULL, occurrence INTEGER NOT NULL, "
        "encoding TEXT NOT NULL, delimiter TEXT NOT NULL, "
        "logical_records INTEGER NOT NULL, accepted INTEGER NOT NULL, "
        "quarantined INTEGER NOT NULL, "
        "start_physical_line INTEGER, end_physical_line INTEGER, "
        "status TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE ingestion_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")
    return conn


def _missing_or_value(fld: FieldContract, cell: str | None) -> Any:
    if cell is None or cell == "":
        if fld.default is not None:
            return convert_cell(fld, fld.default, is_default=True)
        if fld.nullable:
            return None
        raise ValueError("missing value is not nullable and has no default")
    return convert_cell(fld, cell)


def _write_summary(conn: sqlite3.Connection, source: PreparedSource,
                   counts: dict[str, int], status: str,
                   last_line: int | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO source_summary "
        "(source_id, archive, member, occurrence, encoding, delimiter, "
        "logical_records, accepted, quarantined, start_physical_line, "
        "end_physical_line, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source.source_id, os.path.abspath(source.spec.archive),
         source.spec.member, source.record.occurrence, source.spec.encoding,
         source.spec.delimiter, counts["logical"], counts["accepted"],
         counts["quarantined"], 1, last_line, status))


def _normalized_key_value(fld: FieldContract, value: Any) -> Any:
    if fld.type == "integer":
        return int(value)
    return key_value(fld, value)


def _process_source(conn: sqlite3.Connection, source: PreparedSource,
                    contract: Contract, seen_keys: set[tuple[Any, ...]]) -> dict[str, int]:
    counts = {"logical": 0, "accepted": 0, "quarantined": 0}
    fld_by_name = contract.by_name
    pk_fields = [fld_by_name[name] for name in contract.primary_key]
    target_by_source = {
        target: source for source, target in source.target_by_source.items()}
    table = _quote_ident(contract.table)
    insert_sql = (
        f"INSERT INTO {table} ("
        + ", ".join(_quote_ident(x) for x in (
            "source_id", "member", "occurrence", "record_number",
            "start_physical_line", "end_physical_line", *contract.field_names))
        + ") VALUES (" + ", ".join("?" for _ in range(6 + len(contract.fields)))
        + ")")

    with open_verified_member(source.index, source.record) as binary:
        parser = iter_csv_records(
            binary, source.spec.encoding, source.spec.delimiter,
            source_id=source.source_id, member=source.spec.member,
            occurrence=source.record.occurrence)
        try:
            header = next(parser)
        except StopIteration:
            raise SourceStructureError(
                f"source {source.source_id} has no CSV header")
        if header.fields != list(source.source_columns):
            raise SourceStructureError(
                f"source {source.source_id} header changed during data read")

        last_line = header.end_line
        for record in parser:
            if not record.fields:
                # A blank physical line is formatting, not a business record.
                continue
            counts["logical"] += 1
            record_number = counts["logical"]
            last_line = record.end_line
            raw_cells = list(record.fields)
            quarantine_reason: str | None = None

            if len(record.fields) != len(source.source_columns):
                quarantine_reason = (
                    f"field count mismatch: header has "
                    f"{len(source.source_columns)} column(s), record has "
                    f"{len(record.fields)} field(s)")
                conn.execute(
                    "INSERT INTO quarantine (source_id, member, occurrence, "
                    "record_number, start_physical_line, end_physical_line, "
                    "raw_cells, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (source.source_id, source.spec.member,
                     source.record.occurrence, record_number,
                     record.start_line, record.end_line,
                     json.dumps(raw_cells, ensure_ascii=False),
                     quarantine_reason))
                counts["quarantined"] += 1
                continue

            row_by_name = dict(zip(source.source_columns, record.fields))
            converted: dict[str, Any] = {}
            for fld in contract.fields:
                source_name = target_by_source.get(fld.name)
                cell = None if source_name is None else row_by_name[source_name]
                try:
                    converted[fld.name] = _missing_or_value(fld, cell)
                except (ValueError, ArithmeticError) as exc:
                    quarantine_reason = (
                        f"column {fld.name!r} rejected value "
                        f"{'' if cell is None else cell!r}: {exc}")
                    break

            key: tuple[Any, ...] | None = None
            if quarantine_reason is None:
                try:
                    key = tuple(
                        _normalized_key_value(
                            fld, converted[fld.name]) for fld in pk_fields)
                except (ValueError, ArithmeticError) as exc:
                    quarantine_reason = f"invalid primary key: {exc}"

            if quarantine_reason is None:
                assert key is not None
                if key in seen_keys:
                    quarantine_reason = (
                        "duplicate business primary key "
                        + json.dumps(list(key), ensure_ascii=False, default=str)
                        + "; an earlier valid row already owns this key")
                else:
                    values = [
                        source.source_id, source.spec.member,
                        source.record.occurrence, record_number,
                        record.start_line, record.end_line,
                    ]
                    values.extend(
                        storage_value(fld, converted[fld.name])
                        for fld in contract.fields)
                    conn.execute(insert_sql, values)
                    seen_keys.add(key)
                    counts["accepted"] += 1

            if quarantine_reason is not None:
                conn.execute(
                    "INSERT INTO quarantine (source_id, member, occurrence, "
                    "record_number, start_physical_line, end_physical_line, "
                    "raw_cells, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (source.source_id, source.spec.member,
                     source.record.occurrence, record_number,
                     record.start_line, record.end_line,
                     json.dumps(raw_cells, ensure_ascii=False),
                     quarantine_reason))
                counts["quarantined"] += 1

    _write_summary(conn, source, counts, "completed", last_line)
    return counts


def _write_diagnostic(diagnostic_path: str | Path | None, exc: BaseException,
                      target: str | Path) -> None:
    if diagnostic_path is None:
        return
    payload = {
        "target": str(target),
        "error_type": type(exc).__name__,
        "error": str(exc),
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(exc, CorruptArchiveError):
        payload["category"] = "archive"
    elif isinstance(exc, SourceStructureError):
        payload["category"] = "source_structure"
    else:
        payload["category"] = "ingestion"
    with open(diagnostic_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def ingest_sources(
    contract: str | Path | Contract | dict[str, Any],
    manifest: str | Path | list[dict[str, Any]],
    target_database: str | Path,
    *,
    limits: Limits | None = None,
    diagnostic_path: str | Path | None = None,
) -> IngestionResult:
    """Build a SQLite database from streamed archive CSV members.

    The destination is created in a temporary sibling file and renamed only
    after every source is fully consumed and verified.  An existing target is
    rejected before any work starts.
    """
    target_database = os.path.abspath(target_database)
    if os.path.lexists(target_database):
        raise FileExistsError(
            f"refusing to overwrite existing target {target_database!r}")
    parent = os.path.dirname(target_database) or "."
    os.makedirs(parent, exist_ok=True)

    try:
        parsed_contract, specs = _load_json_or_objects(contract, manifest)
        indices = _index_archives(specs, limits)
        prepared = prepare_sources(specs, indices, parsed_contract)

        fd, tmp_path = tempfile.mkstemp(
            prefix=".ingest-", suffix=".sqlite", dir=parent)
        os.close(fd)
        os.unlink(tmp_path)
        conn: sqlite3.Connection | None = None
        totals = {"logical": 0, "accepted": 0, "quarantined": 0}
        try:
            conn = _create_database(tmp_path, parsed_contract)
            seen_keys: set[tuple[Any, ...]] = set()
            for source in prepared:
                counts = _process_source(
                    conn, source, parsed_contract, seen_keys)
                for key in totals:
                    totals[key] += counts[key]
            conn.execute(
                "INSERT INTO ingestion_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)))
            conn.execute(
                "INSERT INTO ingestion_meta(key, value) VALUES (?, ?)",
                ("created_utc",
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")))
            conn.commit()
            # A successful close is part of the publication requirement.
            conn.close()
            conn = None
            os.replace(tmp_path, target_database)
        except BaseException:
            if conn is not None:
                conn.close()
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
    except (IngestionError, CorruptArchiveError, OSError, sqlite3.Error,
            ValueError) as exc:
        _write_diagnostic(diagnostic_path, exc, target_database)
        raise

    return IngestionResult(
        database_path=target_database,
        logical_records=totals["logical"],
        accepted=totals["accepted"],
        quarantined=totals["quarantined"],
        source_count=len(specs),
    )
