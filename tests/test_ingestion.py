"""End-to-end and failure tests for streaming tabular ingestion."""

from __future__ import annotations

import copy
import gzip
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import tracemalloc
import unittest
import zipfile

from archindex import (
    ContractValidationError,
    CorruptArchiveError,
    ManifestValidationError,
    SourceStreamError,
    SourceStructureError,
    ingest_sources,
)
from tests.archive_builders import (
    build_tar,
    build_zip_with_duplicates,
    truncate_file,
)


def write_zip(path: str, members: dict[str, bytes],
              compression: int = zipfile.ZIP_DEFLATED) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def write_tar_gz(path: str, members: dict[str, bytes]) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    with gzip.open(path, "wb") as fh:
        fh.write(buffer.getvalue())


def base_contract() -> dict:
    return {
        "table": "business_event",
        "fields": [
            {"name": "event_id", "type": "integer", "nullable": False},
            {"name": "event_name", "type": "text", "nullable": False,
             "allowed": ["订单", "退款", "审计"]},
            {"name": "amount", "type": "decimal", "nullable": False,
             "precision": 12, "scale": 4, "min": "-99999999.9999",
             "max": "99999999.9999"},
            {"name": "occurred_at", "type": "timestamp", "nullable": False},
            {"name": "status", "type": "text", "nullable": False,
             "default": "NEW", "allowed": ["NEW", "OK", "审计"]},
            {"name": "note", "type": "text", "nullable": True},
        ],
        "primary_key": ["event_id"],
    }


class IngestionEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-ingest-")
        self.work = os.path.join(self.tmp, "input")
        os.mkdir(self.work)
        self.zip_path = os.path.join(self.work, "daily.zip")
        self.tar_path = os.path.join(self.work, "daily.tar.gz")
        self.db_path = os.path.join(self.tmp, "out.sqlite")
        self.contract_path = os.path.join(self.tmp, "contract.json")
        self.manifest_path = os.path.join(self.tmp, "manifest.json")

        # The duplicate is selected by occurrence=1; the first payload would
        # produce event_id 999 and must not be read.
        duplicate_zip = [
            ("data/daily.csv", b"unused\n"),
            ("data/daily.csv",
             "﻿编号,标题,金额,发生时间,备注\r\n"
             "1,订单,12.3400,2026-01-02T03:04:05.123456+08:00,"
             "\"带引号,和逗号\"\r\n"
             "01,审计,0.0001,2026-01-02T00:00:00+00:00,重复整数键\r\n"
             "2,退款,-2.5,2026-01-01T20:00:00-05:00,\r\n"
             "3,订单,1.23456,2026-01-01T00:00:00+00:00,小数位过多\r\n"
             "9223372036854775808,订单,1.0000,2026-01-01T00:00:00Z,超范围\r\n"
             .encode("utf-8")),
        ]
        build_zip_with_duplicates(self.zip_path, duplicate_zip)

        tar_csv = (
            "event_name\tevent_id\tevent_time\tvalue\textra\tnote\n"
            # A logical record spans three physical lines because of newlines
            # and escaped quotes inside quoted fields.
            "审计\t10\t2026-01-03T10:00:00+00:00\t100.0000\tignored\t\"第一行\n"
            "第二行 \"\"引号\"\"\n"
            "第三行\"\n"
            "订单\t11\t2026-01-03 11:00:00+00:00\t200\tignored\t中文默认状态\n"
            "订单\t2\t2026-01-03T00:00:00Z\t2.0000\tignored\t跨成员重复键\n"
            "订单\t12\t2026-01-03T00:00:00\t1.0000\tignored\t坏时间\n"
            "订单\t13\t2026-01-03T00:00:00Z\t1.0000\tignored\t字段过多\t意外列\n"
        ).encode("gb18030")
        write_tar_gz(self.tar_path, {"in/table.tsv": tar_csv})

        with open(self.contract_path, "w", encoding="utf-8") as fh:
            json.dump(base_contract(), fh, ensure_ascii=False)
        manifest = [
            {
                "archive": self.zip_path,
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
                "archive": self.tar_path,
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
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)

    def test_cli_and_database_contents(self):
        # Importable Python API first.
        result = ingest_sources(
            self.contract_path, self.manifest_path, self.db_path)
        self.assertEqual((result.logical_records, result.accepted,
                          result.quarantined), (10, 4, 6))
        self.assertTrue(os.path.exists(self.db_path))

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM business_event ORDER BY event_id").fetchall()
            self.assertEqual([r["event_id"] for r in rows], [1, 2, 10, 11])

            first = conn.execute(
                "SELECT * FROM business_event WHERE event_id=1").fetchone()
            self.assertEqual(first["event_name"], "订单")
            self.assertEqual(first["amount"], "12.3400")
            refund = conn.execute(
                "SELECT amount FROM business_event WHERE event_id=2").fetchone()
            self.assertEqual(refund[0], "-2.5")
            self.assertEqual(type(first["event_id"]).__name__, "int")
            self.assertEqual(first["status"], "NEW")
            self.assertEqual(first["note"], "带引号,和逗号")
            self.assertEqual(
                first["occurred_at"], "2026-01-01 19:04:05.123456+00:00")
            self.assertEqual(
                (first["source_id"], first["record_number"],
                 first["start_physical_line"], first["end_physical_line"]),
                (1, 1, 2, 2))

            second = conn.execute(
                "SELECT * FROM business_event WHERE event_id=2").fetchone()
            self.assertEqual(second["amount"], "-2.5")
            self.assertEqual(
                second["occurred_at"], "2026-01-02 01:00:00.000000+00:00")

            multiline = conn.execute(
                "SELECT * FROM business_event WHERE event_id=10").fetchone()
            self.assertEqual(multiline["event_name"], "审计")
            self.assertEqual(multiline["status"], "NEW")
            self.assertIn('第二行 "引号"', multiline["note"])
            self.assertEqual(
                (multiline["record_number"], multiline["start_physical_line"],
                 multiline["end_physical_line"]), (1, 2, 4))
            self.assertEqual(
                (multiline["source_id"], multiline["member"],
                 multiline["occurrence"]),
                (2, "in/table.tsv", 0))

            quarantine = conn.execute(
                "SELECT member, record_number, start_physical_line, "
                "end_physical_line, reason, raw_cells FROM quarantine "
                "ORDER BY quarantine_id").fetchall()
            self.assertEqual(len(quarantine), 6)
            reasons = "\n".join(r["reason"] for r in quarantine)
            self.assertIn("duplicate business primary key", reasons)
            self.assertIn("fractional digit", reasons)
            self.assertIn("signed SQLite 64-bit range", reasons)
            self.assertIn("timezone", reasons)
            self.assertIn("field count mismatch", reasons)
            duplicate = next(
                r for r in quarantine if "duplicate business" in r["reason"])
            self.assertEqual(duplicate["record_number"], 2)
            self.assertEqual(json.loads(duplicate["raw_cells"])[0], "01")
            cross_member_duplicates = conn.execute(
                "SELECT count(*) FROM quarantine "
                "WHERE reason LIKE 'duplicate business primary key%'").fetchone()[0]
            self.assertEqual(cross_member_duplicates, 2)
            bad_count = next(
                r for r in quarantine if "field count mismatch" in r["reason"])
            self.assertEqual(bad_count["start_physical_line"], 8)

            summary = conn.execute(
                "SELECT * FROM source_summary ORDER BY source_id").fetchall()
            self.assertEqual(
                [(r["logical_records"], r["accepted"], r["quarantined"],
                  r["status"]) for r in summary],
                [(5, 2, 3, "completed"), (5, 2, 3, "completed")])
            self.assertEqual(summary[0]["encoding"], "utf-8")
            self.assertEqual(summary[1]["delimiter"], "\t")

    def test_terminal_entry_point(self):
        ingest_sources(
            self.contract_path, self.manifest_path,
            os.path.join(self.tmp, "api.sqlite"))
        cli_db = os.path.join(self.tmp, "cli.sqlite")
        entry = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "archindex", "__main__.py")
        completed = subprocess.run(
            ["python3", "-I", entry, "ingest",
             "--contract", self.contract_path,
             "--manifest", self.manifest_path,
             "--output", cli_db],
            text=True, capture_output=True, check=True)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            (payload["accepted"], payload["quarantined"]), (4, 6))
        with sqlite3.connect(cli_db) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM business_event")
                .fetchone()[0], 4)

    def test_existing_target_is_refused_without_overwrite(self):
        ingest_sources(self.contract_path, self.manifest_path, self.db_path)
        diagnostic = os.path.join(self.tmp, "diag.json")
        with self.assertRaises(FileExistsError):
            ingest_sources(
                self.contract_path, self.manifest_path, self.db_path,
                diagnostic_path=diagnostic)
        self.assertFalse(os.path.exists(diagnostic))


class IngestionFailureClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-ingest-fail-")
        self.archive = os.path.join(self.tmp, "src.zip")
        self.db = os.path.join(self.tmp, "out.sqlite")
        self.contract = base_contract()

    def _run(self, payload: bytes, *, contract=None, manifest=None,
             encoding="utf-8", delimiter=",", mapping=None,
             extra=False, diagnostic=False, archive_path=None):
        path = archive_path or self.archive
        write_zip(path, {"a.csv": payload})
        cpath = os.path.join(self.tmp, "contract.json")
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(contract or self.contract, fh, ensure_ascii=False)
        source_mapping = mapping or {
            "event_id": "event_id",
            "event_name": "event_name",
            "amount": "amount",
            "occurred_at": "occurred_at",
            "note": "note",
        }
        source = {
            "archive": path,
            "member": "a.csv",
            "encoding": encoding,
            "delimiter": delimiter,
            "mapping": source_mapping,
            "ignore_extra_columns": extra,
        }
        if manifest is not None:
            source = manifest
            source.setdefault("archive", path)
            source.setdefault("member", "a.csv")
        mpath = os.path.join(self.tmp, "manifest.json")
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump([source], fh, ensure_ascii=False)
        diag = os.path.join(self.tmp, "diagnostic.json") if diagnostic else None
        try:
            ingest_sources(cpath, mpath, self.db, diagnostic_path=diag)
        except Exception as exc:
            ctx_exception = exc
        else:
            raise AssertionError("ingestion unexpectedly succeeded")
        self.assertFalse(os.path.exists(self.db))
        if diagnostic:
            self.assertTrue(os.path.exists(diag))
            with open(diag, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["error_type"],
                                 type(ctx_exception).__name__)
        return ctx_exception

    def test_contract_and_manifest_errors_before_data(self):
        good_header = (
            "event_id,event_name,amount,occurred_at,note\n"
            "1,订单,1.0000,2026-01-01T00:00:00Z,x\n")
        contract=copy.deepcopy(self.contract)
        contract["fields"] = [
            {**f, "type": "money"} if f["name"] == "amount" else f
            for f in contract["fields"]]
        self.assertIsInstance(
            self._run(good_header.encode(), contract=contract),
            ContractValidationError)
        self.assertIsInstance(self._run(
            good_header.encode(),
            contract={**copy.deepcopy(self.contract),
                      "primary_key": []}), ContractValidationError)
        duplicate_fields = [dict(f) for f in self.contract["fields"]]
        duplicate_fields.append(dict(duplicate_fields[0]))
        self.assertIsInstance(self._run(
            good_header.encode(),
            contract={**copy.deepcopy(self.contract),
                      "fields": duplicate_fields}), ContractValidationError)
        bad_default = [{**f, "default": "not-a-number"}
                       if f["name"] == "amount" else f
                       for f in self.contract["fields"]]
        empty_default = [{**f, "default": ""}
                         if f["name"] == "status" else f
                         for f in self.contract["fields"]]
        self.assertIsInstance(self._run(
            good_header.encode(),
            contract={**copy.deepcopy(self.contract),
                      "fields": empty_default}), ContractValidationError)

        self.assertIsInstance(self._run(
            good_header.encode(), manifest={
                "archive": self.archive, "member": "a.csv",
                "encoding": "latin-1", "delimiter": ",",
                "mapping": {"event_id": "event_id"}}),
            ManifestValidationError)
        self.assertIsInstance(self._run(
            good_header.encode(), manifest={
                "archive": self.archive, "member": "a.csv",
                "encoding": "utf-8", "delimiter": "|",
                "mapping": {"event_id": "event_id"}}),
            ManifestValidationError)

    def test_source_structure_errors_are_fatal(self):
        payload = (
            "event_id,event_id,amount,occurred_at,note\n"
            "1,订单,1,2026-01-01T00:00:00Z,x\n").encode()
        self.assertIsInstance(self._run(payload), SourceStructureError)

        payload = (
            "event_id,event_name,amount,occurred_at,note,unmapped\n"
            "1,订单,1,2026-01-01T00:00:00Z,x,y\n").encode()
        self.assertIsInstance(self._run(payload), SourceStructureError)

        payload = (
            "id,event_name,amount,occurred_at,note\n"
            "1,订单,1,2026-01-01T00:00:00Z,x\n").encode()
        self.assertIsInstance(self._run(payload), SourceStructureError)

        with zipfile.ZipFile(self.archive, "w") as zf:
            zf.mkdir("dir.csv")
        cpath = os.path.join(self.tmp, "contract.json")
        mpath = os.path.join(self.tmp, "manifest.json")
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(self.contract, fh)
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump([{"archive": self.archive, "member": "dir.csv",
                        "encoding": "utf-8", "delimiter": ",",
                        "mapping": {"event_id": "event_id"}}], fh)
        with self.assertRaises(SourceStructureError):
            ingest_sources(cpath, mpath, self.db)
        self.assertFalse(os.path.exists(self.db))

    def test_bad_rows_are_quarantined_but_unterminated_quote_is_fatal(self):
        # The row-level invalid values are individually represented, while a
        # later quote error prevents reliable parsing and must terminate.
        payload = (
            "event_id,event_name,amount,occurred_at,note\n"
            "1,订单,99999999999999.0000,2026-01-01T00:00:00Z,range\n"
            "2,订单,1.12345,2026-01-01T00:00:00Z,scale\n"
            "3,订单,1.0000,2026-01-01T00:00:00,naive\n"
            "4,订单,1.0000,2026-01-01T00:00:00Z,\"unterminated\n")
        error = self._run(payload.encode(), diagnostic=True)
        self.assertIsInstance(error, SourceStreamError)
        self.assertIn("unexpected end of data", str(error))
        self.assertFalse(os.path.exists(self.db))

    def test_bad_encoding_error_identifies_source_and_progress(self):
        payload = (
            "event_id,event_name,amount,occurred_at,note\n"
            "1,订单,1.0000,2026-01-01T00:00:00Z,中文\n")
        error = self._run(payload.encode("gb18030"), encoding="utf-8")
        self.assertIsInstance(error, (SourceStreamError, CorruptArchiveError))
        self.assertEqual(error.source_id, 1)
        self.assertEqual(error.member, "a.csv")
        self.assertEqual(error.occurrence, 0)
        self.assertGreater(error.bytes_processed, 0)
        self.assertIn("a.csv", str(error))

    def test_truncated_compressed_member_is_fatal_and_diagnosed(self):
        good = os.path.join(self.tmp, "good.zip")
        payload = (
            "event_id,event_name,amount,occurred_at,note\n"
            + "1,订单,1.0000,2026-01-01T00:00:00Z,x\n" * 1000).encode()
        write_zip(good, {"a.csv": payload})
        cpath = os.path.join(self.tmp, "contract.json")
        mpath = os.path.join(self.tmp, "manifest.json")
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(self.contract, fh)
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump([{"archive": good, "member": "a.csv",
                        "encoding": "utf-8", "delimiter": ",",
                        "mapping": {
                            "event_id": "event_id",
                            "event_name": "event_name",
                            "amount": "amount",
                            "occurred_at": "occurred_at",
                            "note": "note",
                        }}], fh)
        # A valid source first proves the failure is caused by truncation.
        ingest_sources(cpath, mpath, os.path.join(self.tmp, "good.sqlite"))
        truncate_file(good, 0.6)
        with self.assertRaises(CorruptArchiveError):
            ingest_sources(
                os.path.join(self.tmp, "contract.json"),
                os.path.join(self.tmp, "manifest.json"),
                os.path.join(self.tmp, "truncated.sqlite"),
                diagnostic_path=os.path.join(self.tmp, "trunc-diag.json"))
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "truncated.sqlite")))


class StreamingMemoryTest(unittest.TestCase):
    def test_large_input_does_not_buffer_all_business_rows(self):
        tmp = tempfile.mkdtemp(prefix="ax-ingest-mem-")
        archive = os.path.join(tmp, "large.zip")
        db = os.path.join(tmp, "large.sqlite")
        contract = {
            "table": "large_event",
            "fields": [
                {"name": "event_id", "type": "integer", "nullable": False},
                {"name": "payload", "type": "text", "nullable": False},
            ],
            "primary_key": ["event_id"],
        }
        cpath = os.path.join(tmp, "contract.json")
        mpath = os.path.join(tmp, "manifest.json")
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(contract, fh)
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump([{
                "archive": archive, "member": "large.csv",
                "encoding": "utf-8", "delimiter": ",",
                "mapping": {"event_id": "event_id", "payload": "payload"},
            }], fh)

        rows = 220_000
        long_text = "长字段ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 20
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            with zf.open("large.csv", "w") as raw:
                raw.write("event_id,payload\n".encode())
                for i in range(rows):
                    raw.write(f"{i},{long_text}-{i}\n".encode())
        uncompressed_size = sum(
            i.file_size for i in zipfile.ZipFile(archive).infolist()
            if i.filename == "large.csv")

        tracemalloc.start()
        try:
            outcome = ingest_sources(cpath, mpath, db)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(outcome.accepted, rows)
        self.assertEqual(outcome.quarantined, 0)
        # The retained key set is small; a 1 MiB decoder/CSV working set is
        # enough.  This assertion documents actual measured bounded memory and
        # is deliberately far smaller than the multi-megabyte member.
        self.assertLess(peak, uncompressed_size / 4)
        self.assertLess(peak, 40 * 1024 * 1024)
        with sqlite3.connect(db) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM large_event").fetchone()[0],
                rows)
            self.assertEqual(
                conn.execute(
                    "SELECT length(payload) FROM large_event WHERE event_id=?"
                    , (rows - 1,)).fetchone()[0],
                len(long_text) + len(str(rows - 1)) + 1)
            self.assertEqual(
                conn.execute(
                    "SELECT logical_records, accepted, quarantined "
                    "FROM source_summary").fetchone(),
                (rows, rows, 0))


if __name__ == "__main__":
    unittest.main()
