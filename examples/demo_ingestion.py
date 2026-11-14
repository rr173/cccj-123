#!/usr/bin/env python3
"""Reproduce the streaming CSV ingestion demo from a clean Python environment.

The script uses only the Python standard library.  It creates its synthetic
archives in a fresh directory, invokes the importable :mod:`archindex` API,
then opens the resulting SQLite database and prints row-level verification
queries.

Run from the project root::

    python3 -I examples/demo_ingestion.py
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import tracemalloc
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from archindex import ingest_sources, index_archive  # noqa: E402


CONTRACT = json.loads((ROOT / "examples" / "contract.json").read_text(
    encoding="utf-8"))


def write_zip_duplicate(path: Path) -> None:
    second = (
        "﻿编号,标题,金额,发生时间,备注\r\n"
        "1,订单,12.3400,2026-01-02T03:04:05.123456+08:00,"
        "\"带引号,和逗号\"\r\n"
        "01,审计,0.0001,2026-01-02T00:00:00+00:00,重复整数键\r\n"
        "2,退款,-2.5,2026-01-01T20:00:00-05:00,\r\n"
        "3,订单,1.23456,2026-01-01T00:00:00Z,小数位过多\r\n"
        "9223372036854775808,订单,1.0000,2026-01-01T00:00:00Z,超范围\r\n"
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data/daily.csv", "first occurrence - must not be selected\n")
        zf.writestr("data/daily.csv", second)


def write_tar_gz(path: Path) -> None:
    csv_text = (
        "event_name\tevent_id\tevent_time\tvalue\textra\tnote\n"
        "审计\t10\t2026-01-03T10:00:00+00:00\t100.0000\tignored\t\"第一行\n"
        "第二行 \"\"引号\"\"\n"
        "第三行\"\n"
        "订单\t11\t2026-01-03 11:00:00+00:00\t200\tignored\t中文默认状态\n"
        "订单\t2\t2026-01-03T00:00:00Z\t2.0000\tignored\t跨成员重复键\n"
        "订单\t12\t2026-01-03T00:00:00\t1.0000\tignored\t无时区时间\n"
        "订单\t13\t2026-01-03T00:00:00Z\t1.0000\tignored\t字段过多\t意外列\n"
    )
    payload = csv_text.encode("gb18030")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("in/table.tsv")
        info.size = len(payload)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(payload))
    with gzip.open(path, "wb") as fh:
        fh.write(buffer.getvalue())


def build_manifest(zip_path: Path, tar_path: Path) -> list[dict]:
    return [
        {
            "archive": str(zip_path),
            "member": "data/daily.csv",
            "occurrence": 1,
            "encoding": "utf-8",
            "delimiter": ",",
            "mapping": {
                "编号": "event_id",
                "标题": "event_name",
                "金额": "amount",
                "发生时间": "occurred_at",
                "备注": "note",
            },
            "ignore_extra_columns": True,
        },
        {
            "archive": str(tar_path),
            "member": "in/table.tsv",
            "encoding": "gb18030",
            "delimiter": "\t",
            "mapping": {
                "event_id": "event_id",
                "event_name": "event_name",
                "value": "amount",
                "event_time": "occurred_at",
                "note": "note",
            },
            "ignore_extra_columns": True,
        },
    ]


def verify_database(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        print("\n-- cleaned business rows --")
        for row in conn.execute(
                "SELECT source_id, member, occurrence, record_number, "
                "start_physical_line, end_physical_line, event_id, "
                "event_name, amount, occurred_at, status, note "
                "FROM business_event ORDER BY event_id"):
            print(dict(row))

        print("\n-- quarantine decisions --")
        for row in conn.execute(
                "SELECT source_id, record_number, start_physical_line, "
                "end_physical_line, reason, raw_cells FROM quarantine "
                "ORDER BY quarantine_id"):
            print(dict(row))

        print("\n-- source summaries --")
        for row in conn.execute(
                "SELECT source_id, archive, member, occurrence, encoding, "
                "delimiter, logical_records, accepted, quarantined, status "
                "FROM source_summary ORDER BY source_id"):
            print(dict(row))


def demonstrate_large_streaming_memory(work: Path) -> None:
    rows = 220_000
    long_text = "长字段ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 20
    archive = work / "large.zip"
    database = work / "large.sqlite"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("large.csv", "w") as raw:
            raw.write("event_id,payload\n".encode("utf-8"))
            for i in range(rows):
                raw.write(f"{i},{long_text}-{i}\n".encode("utf-8"))
    contract = {
        "table": "large_event",
        "fields": [
            {"name": "event_id", "type": "integer", "nullable": False},
            {"name": "payload", "type": "text", "nullable": False},
        ],
        "primary_key": ["event_id"],
    }
    manifest = [{
        "archive": str(archive),
        "member": "large.csv",
        "encoding": "utf-8",
        "delimiter": ",",
        "mapping": {"event_id": "event_id", "payload": "payload"},
    }]
    member_size = zipfile.ZipFile(archive).getinfo("large.csv").file_size
    tracemalloc.start()
    outcome = ingest_sources(contract, manifest, database)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    with sqlite3.connect(database) as conn:
        count = conn.execute("SELECT count(*) FROM large_event").fetchone()[0]
    print("\n-- bounded-memory check --")
    print(json.dumps({
        "rows": count,
        "uncompressed_member_bytes": member_size,
        "tracemalloc_peak_bytes": peak,
        "peak_divided_by_member": peak / member_size,
        "accepted": outcome.accepted,
        "quarantined": outcome.quarantined,
    }, ensure_ascii=False, indent=2))


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="archindex-demo-"))
    zip_path = work / "daily.zip"
    tar_path = work / "daily.tar.gz"
    database = work / "business.sqlite"
    write_zip_duplicate(zip_path)
    write_tar_gz(tar_path)

    print("-- archive index still identifies both duplicate members --")
    index = index_archive(zip_path)
    for member in index.occurrences("data/daily.csv"):
        print({"member": member.name, "occurrence": member.occurrence,
               "size": member.size, "sha256": member.sha256})

    manifest = build_manifest(zip_path, tar_path)
    manifest_path = work / "manifest.json"
    contract_path = work / "contract.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    contract_path.write_text(
        json.dumps(CONTRACT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\ncontract: {contract_path}")
    print(f"manifest: {manifest_path}")

    outcome = ingest_sources(contract_path, manifest_path, database)
    print("\n-- ingestion result --")
    print(outcome)
    verify_database(database)
    demonstrate_large_streaming_memory(work)
    print(f"\nartifacts remain in: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
