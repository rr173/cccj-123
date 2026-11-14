"""Tests for nested-delivery assembly (relational tables -> JSONL)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zipfile

from archindex import (
    AssemblyDataError,
    AssemblyPlanError,
    assemble_nested,
    ingest_sources,
    parse_assembly_plan,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "archindex", "__main__.py")


def read_jsonl(path: str) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def handover_plan() -> dict:
    return {
        "nodes": [
            {"name": "order", "table": "handover_order",
             "identity": ["order_id"],
             "scalars": [
                 {"column": "order_no", "alias": "orderNo"},
                 {"column": "handler", "alias": "handler"},
                 {"column": "amount", "alias": "amountText"},
                 {"column": "big_ref", "alias": "bigRef"},
                 {"column": "source_id", "alias": "sourceId"},
                 {"column": "member", "alias": "sourceMember"},
                 {"column": "occurrence", "alias": "sourceOccurrence"}],
             "order_by": ["order_no"]},
            {"name": "box", "table": "box", "identity": ["box_id"],
             "parent": "order", "array": "boxes", "foreign_key": ["order_id"],
             "scalars": [
                 {"column": "box_no", "alias": "boxNo"},
                 {"column": "label", "alias": "label"}],
             "order_by": ["sort_key"]},
            {"name": "detail", "table": "detail", "identity": ["detail_id"],
             "parent": "box", "array": "details", "foreign_key": ["box_id"],
             "scalars": [
                 {"column": "sku", "alias": "sku"},
                 {"column": "qty", "alias": "qty"},
                 {"column": "note", "alias": "note"}],
             "order_by": []},
            {"name": "remark", "table": "handover_remark",
             "identity": ["remark_id"],
             "parent": "order", "array": "remarks",
             "foreign_key": ["order_id"],
             "scalars": [
                 {"column": "body", "alias": "body"},
                 {"column": "created", "alias": "created"}],
             "order_by": ["created"]},
        ]
    }


HANDOVER_SCHEMA = """
CREATE TABLE handover_order (
  order_id INTEGER, order_no TEXT, handler TEXT, amount TEXT,
  big_ref INTEGER, source_id INTEGER, member TEXT, occurrence INTEGER);
CREATE TABLE box (
  box_id INTEGER, order_id INTEGER, box_no TEXT, label TEXT, sort_key TEXT);
CREATE TABLE detail (
  detail_id INTEGER, box_id INTEGER, sku TEXT, qty INTEGER, note TEXT);
CREATE TABLE handover_remark (
  remark_id INTEGER, order_id INTEGER, body TEXT, created TEXT);
"""

# (sql, params) rows; deliberately scrambled insert order and rowids.
HANDOVER_ROWS = [
    ("INSERT INTO handover_order VALUES (?,?,?,?,?,?,?,?)",
     (1, "H-002", "张三\n（夜班）", "123.4500", 9223372036854775807,
      7, "daily.zip", 1)),
    ("INSERT INTO handover_order VALUES (?,?,?,?,?,?,?,?)",
     (2, "H-001", "李四", "0.0100", -9223372036854775808,
      8, "later.tar.gz", 0)),
    ("INSERT INTO box VALUES (?,?,?,?,?)", (11, 2, "B-02", "空箱", "A")),
    ("INSERT INTO box VALUES (?,?,?,?,?)", (10, 2, "B-01", "冷链", None)),
    ("INSERT INTO box VALUES (?,?,?,?,?)", (12, 1, "B-03", "普货", "B")),
    ("INSERT INTO detail VALUES (?,?,?,?,?)",
     (102, 10, "SKU-DUP", 5, None)),
    ("INSERT INTO detail VALUES (?,?,?,?,?)",
     (100, 10, "SKU-1", 2, "第一行\n第二行 \"引号\" \\反斜杠")),
    ("INSERT INTO detail VALUES (?,?,?,?,?)",
     (101, 10, "SKU-DUP", 5, None)),
    ("INSERT INTO detail VALUES (?,?,?,?,?)", (200, 12, "SKU-9", 1, "x")),
    ("INSERT INTO handover_remark VALUES (?,?,?,?)",
     (3, 2, "备注3：已核对", "2026-01-03T08:00:00Z")),
    ("INSERT INTO handover_remark VALUES (?,?,?,?)",
     (1, 2, "备注1：已核对", "2026-01-01T08:00:00Z")),
    ("INSERT INTO handover_remark VALUES (?,?,?,?)",
     (2, 2, "备注2：已核对", "2026-01-02T08:00:00Z")),
]

# Expected objects, listed independently of the implementation: roots are
# ordered by order_no; boxes by sort_key (NULL first); details by identity
# (the detail node declares zero sort columns); remarks by created.
EXPECTED_HANDOVER = [
    {
        "orderNo": "H-001", "handler": "李四", "amountText": "0.0100",
        "bigRef": -9223372036854775808,
        "sourceId": 8, "sourceMember": "later.tar.gz", "sourceOccurrence": 0,
        "boxes": [
            {"boxNo": "B-01", "label": "冷链", "details": [
                {"sku": "SKU-1", "qty": 2,
                 "note": "第一行\n第二行 \"引号\" \\反斜杠"},
                {"sku": "SKU-DUP", "qty": 5, "note": None},
                {"sku": "SKU-DUP", "qty": 5, "note": None},
            ]},
            {"boxNo": "B-02", "label": "空箱", "details": []},
        ],
        "remarks": [
            {"body": "备注1：已核对", "created": "2026-01-01T08:00:00Z"},
            {"body": "备注2：已核对", "created": "2026-01-02T08:00:00Z"},
            {"body": "备注3：已核对", "created": "2026-01-03T08:00:00Z"},
        ],
    },
    {
        "orderNo": "H-002", "handler": "张三\n（夜班）",
        "amountText": "123.4500", "bigRef": 9223372036854775807,
        "sourceId": 7, "sourceMember": "daily.zip", "sourceOccurrence": 1,
        "boxes": [
            {"boxNo": "B-03", "label": "普货",
             "details": [{"sku": "SKU-9", "qty": 1, "note": "x"}]},
        ],
        "remarks": [],
    },
]


def build_handover_db(path: str, rows=HANDOVER_ROWS, wal: bool = False) -> None:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(HANDOVER_SCHEMA)
    for sql, params in rows:
        conn.execute(sql, params)
    conn.commit()
    conn.close()


class AssemblyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-assembly-")
        self.db = os.path.join(self.tmp, "src.sqlite")
        self.out = os.path.join(self.tmp, "out.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assemble(self, plan, db=None, out=None, **kwargs):
        return assemble_nested(plan, db or self.db, out or self.out,
                               temp_dir=self.tmp, **kwargs)


class HandoverEndToEndTest(AssemblyTestCase):
    def test_expected_objects_and_physical_lines(self):
        build_handover_db(self.db)
        result = self.assemble(handover_plan())
        self.assertEqual(result.roots, 2)
        self.assertEqual(result.node_rows,
                         {"order": 2, "box": 3, "detail": 4, "remark": 3})

        with open(self.out, "rb") as fh:
            raw = fh.read()
        # One root = one physical line: two roots with two boxes and three
        # remarks must not multiply into six lines.
        self.assertEqual(raw.count(b"\n"), 2)
        # Real newlines inside strings are escaped, Chinese stays UTF-8.
        self.assertNotIn("第一行\n第二行".encode("utf-8"), raw)
        self.assertIn("第一行\\n第二行".encode("utf-8"), raw)
        self.assertIn("张三".encode("utf-8"), raw)
        # 64-bit integers are exact, amounts stay JSON strings.
        self.assertIn(b'"bigRef":9223372036854775807', raw)
        self.assertIn(b'"bigRef":-9223372036854775808', raw)
        self.assertIn(b'"amountText":"123.4500"', raw)

        objects = read_jsonl(self.out)
        self.assertEqual(objects, EXPECTED_HANDOVER)
        # The two payload-identical details are both kept.
        details = objects[0]["boxes"][0]["details"]
        self.assertEqual(len(details), 3)
        self.assertEqual(details[1], details[2])
        # The empty box survives with an explicit empty array.
        self.assertEqual(objects[0]["boxes"][1]["details"], [])
        # Identity columns are not emitted unless declared as scalars.
        self.assertNotIn("order_id", objects[0])
        self.assertNotIn("box_id", objects[0]["boxes"][0])

    def test_insertion_order_and_rowid_do_not_matter(self):
        build_handover_db(self.db)
        other = os.path.join(self.tmp, "other.sqlite")
        build_handover_db(other, rows=list(reversed(HANDOVER_ROWS)))
        out2 = os.path.join(self.tmp, "out2.jsonl")
        self.assemble(handover_plan())
        self.assemble(handover_plan(), db=other, out=out2)
        with open(self.out, "rb") as fh:
            first = fh.read()
        with open(out2, "rb") as fh:
            second = fh.read()
        self.assertEqual(first, second)

    def test_python_and_cli_entry_points_agree(self):
        build_handover_db(self.db)
        plan_path = os.path.join(self.tmp, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(handover_plan(), fh, ensure_ascii=False)
        cli_out = os.path.join(self.tmp, "cli.jsonl")
        proc = subprocess.run(
            [sys.executable, "-I", MAIN, "assemble",
             "--database", self.db, "--plan", plan_path,
             "--output", cli_out, "--temp-dir", self.tmp],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["roots"], 2)
        self.assertEqual(summary["node_rows"]["detail"], 4)
        self.assemble(handover_plan())
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), open(cli_out, "rb").read())
        # The scratch directory is cleaned up; only our outputs remain.
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["cli.jsonl", "out.jsonl", "plan.json", "src.sqlite"])


class FourLevelPlanTest(AssemblyTestCase):
    def test_four_levels_are_not_a_three_table_special_case(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE region (region_id INTEGER, name TEXT);
        CREATE TABLE warehouse (wh_id INTEGER, region_id INTEGER, name TEXT);
        CREATE TABLE zone (zone_id INTEGER, wh_id INTEGER, name TEXT);
        CREATE TABLE shelf (shelf_id INTEGER, zone_id INTEGER, code TEXT);
        """)
        conn.execute("INSERT INTO region VALUES (2, '华北')")
        conn.execute("INSERT INTO region VALUES (1, '华东')")
        conn.execute("INSERT INTO warehouse VALUES (11, 1, '杭州仓')")
        conn.execute("INSERT INTO warehouse VALUES (10, 1, '上海仓')")
        conn.execute("INSERT INTO zone VALUES (101, 10, 'B区')")
        conn.execute("INSERT INTO zone VALUES (100, 10, 'A区')")
        conn.execute("INSERT INTO shelf VALUES (1001, 100, 'S-02')")
        conn.execute("INSERT INTO shelf VALUES (1000, 100, 'S-01')")
        conn.commit()
        conn.close()
        plan = {
            "nodes": [
                {"name": "region", "table": "region",
                 "identity": ["region_id"],
                 "scalars": [{"column": "name", "alias": "name"}],
                 "order_by": ["region_id"]},
                {"name": "wh", "table": "warehouse", "identity": ["wh_id"],
                 "parent": "region", "array": "warehouses",
                 "foreign_key": ["region_id"],
                 "scalars": [{"column": "name", "alias": "name"}],
                 "order_by": ["wh_id"]},
                {"name": "zone", "table": "zone", "identity": ["zone_id"],
                 "parent": "wh", "array": "zones", "foreign_key": ["wh_id"],
                 "scalars": [{"column": "name", "alias": "name"}],
                 "order_by": ["zone_id"]},
                {"name": "shelf", "table": "shelf", "identity": ["shelf_id"],
                 "parent": "zone", "array": "shelves",
                 "foreign_key": ["zone_id"],
                 "scalars": [{"column": "code", "alias": "code"}],
                 "order_by": ["shelf_id"]},
            ]
        }
        result = self.assemble(plan)
        self.assertEqual(result.roots, 2)
        self.assertEqual(read_jsonl(self.out), [
            {"name": "华东", "warehouses": [
                {"name": "上海仓", "zones": [
                    {"name": "A区", "shelves": [
                        {"code": "S-01"}, {"code": "S-02"}]},
                    {"name": "B区", "shelves": []},
                ]},
                {"name": "杭州仓", "zones": []},
            ]},
            {"name": "华北", "warehouses": []},
        ])


class IdentitySemanticsTest(AssemblyTestCase):
    def test_composite_identity_with_separator_characters(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE grp (a TEXT, b TEXT, label TEXT);
        CREATE TABLE item (item_id INTEGER, ga TEXT, gb TEXT, v TEXT);
        """)
        conn.execute("INSERT INTO grp VALUES ('A|B', 'C', '第一组')")
        conn.execute("INSERT INTO grp VALUES ('A', 'B|C', '第二组')")
        conn.execute("INSERT INTO item VALUES (1, 'A|B', 'C', '属于第一组')")
        conn.execute("INSERT INTO item VALUES (2, 'A', 'B|C', '属于第二组')")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "g", "table": "grp", "identity": ["a", "b"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": ["a", "b"]},
            {"name": "i", "table": "item", "identity": ["item_id"],
             "parent": "g", "array": "items", "foreign_key": ["ga", "gb"],
             "scalars": [{"column": "v", "alias": "v"}], "order_by": []},
        ]}
        self.assemble(plan)
        # Components are never fused, so "A|B","C" and "A","B|C" stay
        # distinct and each child lands under its own composite key.
        self.assertEqual(read_jsonl(self.out), [
            {"label": "第二组", "items": [{"v": "属于第二组"}]},
            {"label": "第一组", "items": [{"v": "属于第一组"}]},
        ])

    def test_text_01_and_integer_1_are_different_identities(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE p (pid, label TEXT);
        CREATE TABLE c (cid INTEGER, pref, v TEXT);
        """)
        conn.execute("INSERT INTO p VALUES (1, '整数一')")
        conn.execute("INSERT INTO p VALUES ('01', '文本零一')")
        conn.execute("INSERT INTO c VALUES (10, 1, '整数子')")
        conn.execute("INSERT INTO c VALUES (11, '01', '文本子')")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
            {"name": "c", "table": "c", "identity": ["cid"], "parent": "p",
             "array": "children", "foreign_key": ["pref"],
             "scalars": [{"column": "v", "alias": "v"}], "order_by": []},
        ]}
        self.assemble(plan)
        # '01' and 1 coexist (no duplicate-identity error) and each child
        # matches only the parent with the same storage type.  With zero
        # sort columns the identity itself orders rows: INTEGER < TEXT.
        self.assertEqual(read_jsonl(self.out), [
            {"label": "整数一", "children": [{"v": "整数子"}]},
            {"label": "文本零一", "children": [{"v": "文本子"}]},
        ])

    def test_cross_type_foreign_key_is_an_orphan(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE p (pid INTEGER, label TEXT);
        CREATE TABLE c (cid INTEGER, pref, v TEXT);
        """)
        conn.execute("INSERT INTO p VALUES (1, '整数一')")
        conn.execute("INSERT INTO c VALUES (10, '1', '文本引用')")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
            {"name": "c", "table": "c", "identity": ["cid"], "parent": "p",
             "array": "children", "foreign_key": ["pref"],
             "scalars": [{"column": "v", "alias": "v"}], "order_by": []},
        ]}
        with self.assertRaisesRegex(AssemblyDataError, "orphan"):
            self.assemble(plan)
        self.assertFalse(os.path.exists(self.out))


class SortingDeterminismTest(AssemblyTestCase):
    def _build(self, path: str, sort_values: list) -> None:
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE r (rid INTEGER, label TEXT);
        CREATE TABLE t (tid INTEGER, rrid INTEGER, s, v TEXT);
        """)
        conn.execute("INSERT INTO r VALUES (1, 'root')")
        for tid, sort in sort_values:
            conn.execute("INSERT INTO t VALUES (?,?,?,?)",
                         (tid, 1, sort, f"v{tid}"))
        conn.commit()
        conn.close()

    def _plan(self):
        return {"nodes": [
            {"name": "r", "table": "r", "identity": ["rid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
            {"name": "t", "table": "t", "identity": ["tid"], "parent": "r",
             "array": "items", "foreign_key": ["rrid"],
             "scalars": [{"column": "v", "alias": "v"},
                         {"column": "tid", "alias": "tid"}],
             "order_by": ["s"]},
        ]}

    def test_null_first_ties_broken_by_identity(self):
        # NULL sorts before non-NULL; equal keys fall back to identity.
        self._build(self.db, [(5, None), (2, "a"), (9, None), (1, "a")])
        self.assemble(self._plan())
        items = read_jsonl(self.out)[0]["items"]
        self.assertEqual([i["tid"] for i in items], [5, 9, 1, 2])

    def test_zero_sort_columns_uses_identity_order(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE r (rid INTEGER);
        CREATE TABLE t (tid INTEGER, rrid INTEGER);
        """)
        conn.execute("INSERT INTO r VALUES (1)")
        for tid in (40, 10, 30, 20):
            conn.execute("INSERT INTO t VALUES (?,?)", (tid, 1))
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "r", "table": "r", "identity": ["rid"],
             "scalars": [], "order_by": []},
            {"name": "t", "table": "t", "identity": ["tid"], "parent": "r",
             "array": "items", "foreign_key": ["rrid"],
             "scalars": [{"column": "tid", "alias": "tid"}], "order_by": []},
        ]}
        self.assemble(plan)
        items = read_jsonl(self.out)[0]["items"]
        self.assertEqual([i["tid"] for i in items], [10, 20, 30, 40])

    def test_mixed_sort_storage_types_rejected(self):
        self._build(self.db, [(1, "a"), (2, 7)])
        with self.assertRaisesRegex(AssemblyDataError, "mixes"):
            self.assemble(self._plan())
        self.assertFalse(os.path.exists(self.out))

    def test_real_sort_value_rejected(self):
        self._build(self.db, [(1, 1.5)])
        with self.assertRaisesRegex(AssemblyDataError, "sort column"):
            self.assemble(self._plan())
        self.assertFalse(os.path.exists(self.out))


class IntegrityFailureTest(AssemblyTestCase):
    def _plan(self):
        return {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
            {"name": "c", "table": "c", "identity": ["cid"], "parent": "p",
             "array": "children", "foreign_key": ["pref"],
             "scalars": [{"column": "v", "alias": "v"}], "order_by": []},
        ]}

    def _two_table_db(self, parent_rows, child_rows):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE p (pid INTEGER, label TEXT);
        CREATE TABLE c (cid INTEGER, pref, v TEXT);
        """)
        for row in parent_rows:
            conn.execute("INSERT INTO p VALUES (?,?)", row)
        for row in child_rows:
            conn.execute("INSERT INTO c VALUES (?,?,?)", row)
        conn.commit()
        conn.close()

    def test_orphan_rejected_with_node_and_identity(self):
        self._two_table_db([(1, "一")], [(10, 1, "正常"), (11, 999, "孤儿")])
        with self.assertRaisesRegex(
                AssemblyDataError,
                r"node 'c'.*orphan.*\[11\].*\[999\]"):
            self.assemble(self._plan())
        self.assertFalse(os.path.exists(self.out))

    def test_duplicate_identity_rejected(self):
        self._two_table_db([(1, "一"), (1, "一bis")], [])
        with self.assertRaisesRegex(
                AssemblyDataError, r"node 'p'.*duplicate.*\[1\]"):
            self.assemble(self._plan())
        self.assertFalse(os.path.exists(self.out))

    def test_null_foreign_key_rejected(self):
        self._two_table_db([(1, "一")], [(10, None, "空引用")])
        with self.assertRaisesRegex(
                AssemblyDataError, r"node 'c'.*null foreign-key.*pref"):
            self.assemble(self._plan())
        self.assertFalse(os.path.exists(self.out))

    def test_partial_null_composite_foreign_key_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE p (a INTEGER, b INTEGER, label TEXT);
        CREATE TABLE c (cid INTEGER, fa INTEGER, fb INTEGER, v TEXT);
        """)
        conn.execute("INSERT INTO p VALUES (1, 2, 'x')")
        conn.execute("INSERT INTO c VALUES (10, 1, NULL, '半空引用')")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["a", "b"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
            {"name": "c", "table": "c", "identity": ["cid"], "parent": "p",
             "array": "children", "foreign_key": ["fa", "fb"],
             "scalars": [{"column": "v", "alias": "v"}], "order_by": []},
        ]}
        with self.assertRaisesRegex(
                AssemblyDataError, r"null foreign-key.*'fb'"):
            self.assemble(plan)
        self.assertFalse(os.path.exists(self.out))

    def test_null_identity_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE p (pid INTEGER, label TEXT)")
        conn.execute("INSERT INTO p VALUES (NULL, '空身份')")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
        ]}
        with self.assertRaisesRegex(AssemblyDataError, "null identity"):
            self.assemble(plan)
        self.assertFalse(os.path.exists(self.out))

    def test_wrong_storage_types_rejected(self):
        cases = [
            ("REAL identity", "CREATE TABLE p (pid REAL, label TEXT)",
             "INSERT INTO p VALUES (1.5, 'x')", "identity column 'pid'"),
            ("BLOB identity", "CREATE TABLE p (pid BLOB, label TEXT)",
             "INSERT INTO p VALUES (x'01', 'x')", "identity column 'pid'"),
            ("REAL foreign key",
             "CREATE TABLE p (pid INTEGER, label TEXT);"
             "CREATE TABLE c (cid INTEGER, pref REAL, v TEXT)",
             "INSERT INTO p VALUES (1,'x');"
             "INSERT INTO c VALUES (9, 1.0, 'y')", "foreign-key column"),
        ]
        for label, schema, fill, pattern in cases:
            with self.subTest(label=label):
                db = os.path.join(self.tmp, label.replace(" ", "_") + ".sqlite")
                conn = sqlite3.connect(db)
                conn.executescript(schema + ";" + fill + ";")
                conn.commit()
                conn.close()
                plan = {"nodes": [
                    {"name": "p", "table": "p", "identity": ["pid"],
                     "scalars": [{"column": "label", "alias": "label"}],
                     "order_by": []},
                ]}
                if "foreign" in label:
                    plan["nodes"].append(
                        {"name": "c", "table": "c", "identity": ["cid"],
                         "parent": "p", "array": "children",
                         "foreign_key": ["pref"],
                         "scalars": [{"column": "v", "alias": "v"}],
                         "order_by": []})
                with self.assertRaisesRegex(AssemblyDataError, pattern):
                    self.assemble(plan, db=db)
                self.assertFalse(os.path.exists(self.out))

    def test_blob_scalar_rejected_with_column_and_identity(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE p (pid INTEGER, payload BLOB)")
        conn.execute("INSERT INTO p VALUES (7, x'DEAD')")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "payload", "alias": "payload"}],
             "order_by": []},
        ]}
        with self.assertRaisesRegex(
                AssemblyDataError, r"node 'p'.*column 'payload'.*BLOB.*\[7\]"):
            self.assemble(plan)
        self.assertFalse(os.path.exists(self.out))

    def test_non_finite_real_scalar_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE p (pid INTEGER, x REAL)")
        conn.execute("INSERT INTO p VALUES (7, ?)", (float("inf"),))
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "x", "alias": "x"}], "order_by": []},
        ]}
        with self.assertRaisesRegex(AssemblyDataError, "non-finite REAL"):
            self.assemble(plan)
        self.assertFalse(os.path.exists(self.out))

    def test_finite_real_scalar_passes_through(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE p (pid INTEGER, x REAL)")
        conn.execute("INSERT INTO p VALUES (7, 0.125)")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "x", "alias": "x"}], "order_by": []},
        ]}
        self.assemble(plan)
        self.assertEqual(read_jsonl(self.out), [{"x": 0.125}])

    def test_row_payload_limit(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE p (pid INTEGER, x TEXT)")
        conn.execute("INSERT INTO p VALUES (7, ?)", ("x" * 5000,))
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "x", "alias": "x"}], "order_by": []},
        ]}
        with self.assertRaisesRegex(AssemblyDataError, "single-row limit"):
            self.assemble(plan, max_row_payload_bytes=1024)
        self.assertFalse(os.path.exists(self.out))
        self.assemble(plan, max_row_payload_bytes=1024 * 1024)
        self.assertEqual(len(read_jsonl(self.out)), 1)


class EmptyAndRootCasesTest(AssemblyTestCase):
    def test_empty_root_and_empty_children_yield_empty_file(self):
        build_handover_db(self.db, rows=[])
        result = self.assemble(handover_plan())
        self.assertEqual(result.roots, 0)
        self.assertTrue(os.path.exists(self.out))
        self.assertEqual(os.path.getsize(self.out), 0)

    def test_empty_root_with_child_rows_still_finds_orphans(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(HANDOVER_SCHEMA)
        conn.execute("INSERT INTO box VALUES (10, 999, 'B-01', 'x', NULL)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(AssemblyDataError, "orphan"):
            self.assemble(handover_plan())
        self.assertFalse(os.path.exists(self.out))

    def test_grandchild_orphan_found_when_middle_level_empty(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(HANDOVER_SCHEMA)
        conn.execute(
            "INSERT INTO handover_order VALUES (1,'H-1','h','1.00',1,1,'m',0)")
        conn.execute("INSERT INTO detail VALUES (5, 777, 'SKU', 1, NULL)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(
                AssemblyDataError, r"node 'detail'.*orphan"):
            self.assemble(handover_plan())
        self.assertFalse(os.path.exists(self.out))


class PlanValidationTest(AssemblyTestCase):
    def _base_nodes(self):
        return [
            {"name": "p", "table": "p", "identity": ["pid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
            {"name": "c", "table": "c", "identity": ["cid"], "parent": "p",
             "array": "children", "foreign_key": ["pid"],
             "scalars": [{"column": "v", "alias": "v"}], "order_by": []},
        ]

    def _expect_plan_error(self, nodes, pattern):
        with self.assertRaisesRegex(AssemblyPlanError, pattern):
            parse_assembly_plan({"nodes": nodes})

    def test_duplicate_node_names(self):
        nodes = self._base_nodes()
        nodes[1]["name"] = "p"
        self._expect_plan_error(nodes, "duplicate node name")

    def test_unknown_parent(self):
        nodes = self._base_nodes()
        nodes[1]["parent"] = "nope"
        self._expect_plan_error(nodes, "unknown parent")

    def test_multiple_roots(self):
        nodes = self._base_nodes()
        del nodes[1]["parent"], nodes[1]["array"], nodes[1]["foreign_key"]
        self._expect_plan_error(nodes, "exactly one root")

    def test_no_root_is_a_cycle_error(self):
        nodes = self._base_nodes()
        nodes[0]["parent"] = "c"
        nodes[0]["array"] = "parents"
        nodes[0]["foreign_key"] = ["cid"]
        self._expect_plan_error(nodes, "exactly one root")

    def test_cycle_unreachable_from_root(self):
        nodes = self._base_nodes()
        nodes.append({"name": "x", "table": "c", "identity": ["cid"],
                      "parent": "y", "array": "xs", "foreign_key": ["cid"],
                      "scalars": [], "order_by": []})
        nodes.append({"name": "y", "table": "c", "identity": ["cid"],
                      "parent": "x", "array": "ys", "foreign_key": ["cid"],
                      "scalars": [], "order_by": []})
        self._expect_plan_error(nodes, "cycle")

    def test_foreign_key_arity_mismatch(self):
        nodes = self._base_nodes()
        nodes[1]["foreign_key"] = ["pid", "extra"]
        self._expect_plan_error(nodes, "foreign_key has 2")

    def test_scalar_alias_conflicts(self):
        nodes = self._base_nodes()
        nodes[0]["scalars"].append({"column": "label", "alias": "label"})
        self._expect_plan_error(nodes, "duplicate scalar alias")

    def test_array_name_conflicts_with_alias(self):
        nodes = self._base_nodes()
        nodes[1]["array"] = "label"
        self._expect_plan_error(nodes, "conflicts with a scalar alias")

    def test_two_children_same_array_name(self):
        nodes = self._base_nodes()
        nodes.append({"name": "c2", "table": "c", "identity": ["cid"],
                      "parent": "p", "array": "children",
                      "foreign_key": ["pid"], "scalars": [], "order_by": []})
        self._expect_plan_error(nodes, "conflicts with a scalar alias")

    def test_root_must_not_have_array(self):
        nodes = self._base_nodes()
        nodes[0]["array"] = "roots"
        self._expect_plan_error(nodes, "must not declare")

    def test_empty_identity_rejected(self):
        nodes = self._base_nodes()
        nodes[0]["identity"] = []
        self._expect_plan_error(nodes, "identity")

    def test_duplicate_order_by_columns_rejected(self):
        nodes = self._base_nodes()
        nodes[0]["order_by"] = ["label", "label"]
        self._expect_plan_error(nodes, "duplicate")

    def test_unknown_table_and_column(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE p (pid INTEGER, label TEXT)")
        conn.execute("CREATE TABLE c (cid INTEGER, pid INTEGER, v TEXT)")
        conn.commit()
        conn.close()
        nodes = self._base_nodes()
        nodes[0]["table"] = "missing"
        with self.assertRaisesRegex(AssemblyPlanError, "table 'missing'"):
            self.assemble({"nodes": nodes})
        nodes = self._base_nodes()
        nodes[0]["scalars"].append({"column": "ghost", "alias": "g"})
        with self.assertRaisesRegex(
                AssemblyPlanError, "column 'ghost'.*does not exist"):
            self.assemble({"nodes": nodes})
        self.assertFalse(os.path.exists(self.out))

    def test_weird_identifier_names_are_quoted_not_blacklisted(self):
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE "odd tbl" ("we ird""col" INTEGER, '
                     '"select" TEXT, "x); DROP TABLE p; --" TEXT)')
        conn.execute('INSERT INTO "odd tbl" VALUES (1, \'a\', \'b\')')
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "o", "table": "odd tbl",
             "identity": ['we ird"col'],
             "scalars": [{"column": "select", "alias": "s"},
                         {"column": "x); DROP TABLE p; --", "alias": "t"}],
             "order_by": []},
        ]}
        self.assemble(plan)
        self.assertEqual(read_jsonl(self.out), [{"s": "a", "t": "b"}])


class TargetProtectionTest(AssemblyTestCase):
    def setUp(self):
        super().setUp()
        build_handover_db(self.db)

    def test_existing_target_refused(self):
        with open(self.out, "w", encoding="utf-8") as fh:
            fh.write("keep me\n")
        with self.assertRaises(FileExistsError):
            self.assemble(handover_plan())
        with open(self.out, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep me\n")

    def test_target_must_not_be_source_alias(self):
        alias = os.path.join(self.tmp, "..",
                             os.path.basename(self.tmp), "src.sqlite")
        with self.assertRaisesRegex(ValueError, "source database"):
            self.assemble(handover_plan(), out=alias)

    def test_unwritable_output_location_leaves_no_file(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("i am a regular file\n")
        bad_out = os.path.join(blocker, "out.jsonl")
        with self.assertRaises(OSError):
            self.assemble(handover_plan(), out=bad_out)
        self.assertFalse(os.path.exists(bad_out))
        if os.geteuid() != 0:
            sealed = os.path.join(self.tmp, "sealed")
            os.mkdir(sealed)
            os.chmod(sealed, 0o500)
            sealed_out = os.path.join(sealed, "out.jsonl")
            with self.assertRaises(PermissionError):
                self.assemble(handover_plan(), out=sealed_out)
            self.assertFalse(os.path.exists(sealed_out))

    def test_failure_leaves_no_stray_files(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO detail VALUES (999, 12345, 'X', 1, NULL)")
        conn.commit()
        conn.close()
        before = set(os.listdir(self.tmp))
        with self.assertRaises(AssemblyDataError):
            self.assemble(handover_plan())
        after = set(os.listdir(self.tmp))
        self.assertEqual(before, after)


class WalSnapshotTest(AssemblyTestCase):
    def test_concurrent_writer_does_not_change_current_file(self):
        build_handover_db(self.db, wal=True)
        plan = handover_plan()
        injected = []

        def progress(stage, node, count):
            if stage == "extracted" and node == "order" and not injected:
                injected.append(True)
                # Another connection rewrites later roots and child rows
                # while our read transaction is still open.
                other = sqlite3.connect(self.db)
                other.execute(
                    "UPDATE handover_order SET handler='被改写' "
                    "WHERE order_id=1")
                other.execute(
                    "UPDATE detail SET note='并发修改' WHERE detail_id=200")
                other.execute("DELETE FROM handover_remark WHERE remark_id=3")
                other.execute(
                    "INSERT INTO handover_order VALUES "
                    "(3,'H-000','新单','9.9900',1,1,'m',0)")
                other.commit()
                other.close()

        self.assemble(plan, progress=progress)
        self.assertTrue(injected)
        objects = read_jsonl(self.out)
        # The current file still reflects the original read view.
        self.assertEqual(objects, EXPECTED_HANDOVER)
        # A fresh export sees the new values.
        out2 = os.path.join(self.tmp, "out2.jsonl")
        self.assemble(plan, out=out2)
        later = read_jsonl(out2)
        self.assertEqual(len(later), 3)
        self.assertEqual(later[0]["orderNo"], "H-000")
        changed = [o for o in later if o["orderNo"] == "H-002"][0]
        self.assertEqual(changed["handler"], "被改写")
        self.assertEqual(
            changed["boxes"][0]["details"][0]["note"], "并发修改")
        first = [o for o in later if o["orderNo"] == "H-001"][0]
        self.assertEqual(len(first["remarks"]), 2)

    def test_source_connection_released_and_old_commands_work(self):
        build_handover_db(self.db, wal=True)
        self.assemble(handover_plan())
        # No locks are held: another connection can write immediately.
        conn = sqlite3.connect(self.db, timeout=5)
        conn.execute(
            "INSERT INTO handover_order VALUES (9,'H-9','h','1.00',1,1,'m',0)")
        conn.commit()
        conn.close()
        # The pre-existing CLI commands still run.
        zip_path = os.path.join(self.tmp, "a.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("x.txt", b"hello")
        proc = subprocess.run(
            [sys.executable, "-I", MAIN, "index", zip_path],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["members"][0]["name"],
                         "x.txt")


class IngestionDatabaseUntouchedTest(AssemblyTestCase):
    def test_export_does_not_modify_ingested_database(self):
        contract = {
            "table": "business_event",
            "fields": [
                {"name": "event_id", "type": "integer", "nullable": False},
                {"name": "event_name", "type": "text", "nullable": False},
                {"name": "note", "type": "text", "nullable": True},
            ],
            "primary_key": ["event_id"],
        }
        contract_path = os.path.join(self.tmp, "contract.json")
        with open(contract_path, "w", encoding="utf-8") as fh:
            json.dump(contract, fh, ensure_ascii=False)
        zip_path = os.path.join(self.tmp, "in.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("d.csv", "event_id,event_name,note\n"
                                 "1，订单，你好\n2，退款，\n".replace("，", ","))
        manifest = [{
            "archive": zip_path, "member": "d.csv", "encoding": "utf-8",
            "delimiter": ",",
            "mapping": {"event_id": "event_id", "event_name": "event_name",
                        "note": "note"},
        }]
        manifest_path = os.path.join(self.tmp, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
        outcome = ingest_sources(contract_path, manifest_path, self.db)
        self.assertEqual(outcome.accepted, 2)

        def digest():
            with open(self.db, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()

        before = digest()
        plan = {"nodes": [
            {"name": "e", "table": "business_event",
             "identity": ["event_id"],
             "scalars": [
                 {"column": "event_name", "alias": "name"},
                 {"column": "note", "alias": "note"},
                 {"column": "source_id", "alias": "sourceId"},
                 {"column": "member", "alias": "sourceMember"}],
             "order_by": ["event_id"]},
        ]}
        result = self.assemble(plan)
        self.assertEqual(result.roots, 2)
        objects = read_jsonl(self.out)
        # Source-locating columns survive as declared scalars.
        self.assertEqual(objects[0]["sourceId"], 1)
        self.assertEqual(objects[0]["sourceMember"], "d.csv")
        # The database file, quarantine and source summary are untouched.
        self.assertEqual(digest(), before)
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0], 0)
        self.assertEqual(
            conn.execute(
                "SELECT logical_records, accepted, quarantined "
                "FROM source_summary").fetchone(), (2, 2, 0))
        conn.close()


class SameTableMultipleRolesTest(AssemblyTestCase):
    def test_one_table_two_roles(self):
        # The box table plays two roles against the exchange table: each
        # box appears under its current exchange and again under the
        # exchange it originated from.
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE exchange (eid INTEGER, label TEXT);
        CREATE TABLE box (
          bid INTEGER, cur_e INTEGER, origin_e INTEGER, code TEXT);
        """)
        conn.execute("INSERT INTO exchange VALUES (1, '当前交接')")
        conn.execute("INSERT INTO exchange VALUES (2, '上次交接')")
        conn.execute("INSERT INTO box VALUES (10, 1, 2, 'BOX-10')")
        conn.execute("INSERT INTO box VALUES (11, 1, 1, 'BOX-11')")
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "ex", "table": "exchange", "identity": ["eid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": ["eid"]},
            {"name": "cur", "table": "box", "identity": ["bid"],
             "parent": "ex", "array": "currentBoxes",
             "foreign_key": ["cur_e"],
             "scalars": [{"column": "code", "alias": "code"}],
             "order_by": ["bid"]},
            {"name": "orig", "table": "box", "identity": ["bid"],
             "parent": "ex", "array": "originBoxes",
             "foreign_key": ["origin_e"],
             "scalars": [{"column": "code", "alias": "code"}],
             "order_by": ["bid"]},
        ]}
        self.assemble(plan)
        self.assertEqual(read_jsonl(self.out), [
            {"label": "当前交接",
             "currentBoxes": [{"code": "BOX-10"}, {"code": "BOX-11"}],
             "originBoxes": [{"code": "BOX-11"}]},
            {"label": "上次交接",
             "currentBoxes": [],
             "originBoxes": [{"code": "BOX-10"}]},
        ])


class BoundedMemorySmokeTest(AssemblyTestCase):
    def test_single_huge_root_stays_bounded(self):
        # A single root with thousands of children and multi-megabyte text
        # payload must stream; Python-side allocations stay far below the
        # payload volume.  The full 300k-row / 256 MiB / RSS<=96 MiB stress
        # run lives in examples/demo_assembly.py.
        rows, payload = 4000, 700
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE r (rid INTEGER, label TEXT);
        CREATE TABLE d (did INTEGER, rrid INTEGER, seq INTEGER, body TEXT);
        """)
        conn.execute("INSERT INTO r VALUES (1, '巨根')")
        text = "长文本" * (payload // 9)
        for i in range(rows):
            conn.execute("INSERT INTO d VALUES (?,?,?,?)",
                         (i, 1, rows - i, f"{text}{i:06d}"))
        conn.commit()
        conn.close()
        plan = {"nodes": [
            {"name": "r", "table": "r", "identity": ["rid"],
             "scalars": [{"column": "label", "alias": "label"}],
             "order_by": []},
            {"name": "d", "table": "d", "identity": ["did"], "parent": "r",
             "array": "items", "foreign_key": ["rrid"],
             "scalars": [{"column": "body", "alias": "body"},
                         {"column": "did", "alias": "did"}],
             "order_by": ["seq"]},
        ]}
        tracemalloc.start()
        try:
            result = self.assemble(plan, batch_size=256)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result.roots, 1)
        self.assertEqual(result.node_rows["d"], rows)
        self.assertLess(peak, 24 * 1024 * 1024)
        objects = read_jsonl(self.out)
        self.assertEqual(len(objects), 1)
        items = objects[0]["items"]
        self.assertEqual(len(items), rows)
        # Sorted by seq descending identity asc: seq rows-i means did rows-1
        # first, did 0 last.
        self.assertEqual(items[0]["did"], rows - 1)
        self.assertEqual(items[-1]["did"], 0)


if __name__ == "__main__":
    unittest.main()
