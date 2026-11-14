#!/usr/bin/env python3
"""可复跑演练：关系表组装嵌套交付文件。

从干净环境运行（项目根目录）::

    python3 -I examples/demo_assembly.py                 # 小型演练 + 小样本压力
    python3 -I examples/demo_assembly.py --stress-rows 300000 --stress-mib 280

演练内容：

1. 交接单 / 箱 / 明细 / 签收备注四个节点的小型组装，期望对象在脚本内独立
   列出并逐对象核对（同一根的两个箱与三条备注不产生六个根；空箱保留；
   载荷相同但身份不同的明细不去重；NULL 排序在前；插入顺序不影响结果）。
2. 一个四层方案（区域→仓库→库区→货架），证明不是三张表的特例。
3. 失败诊断：孤儿、重复身份、空父引用、错误存储类型、配置名冲突、
   无权限写出——每种都确认没有留下最终 JSONL。
4. WAL 并发：导出读到第一根后另一连接改写后续根与子行，当前文件仍对应
   原读取视图；导出结束后连接释放，下一次导出才看到新值。
5. 压力：单个根下面挂 ``--stress-rows`` 条直接孩子、总文本载荷超过
   ``--stress-mib`` MiB，在子进程中运行终端命令并记录耗时、峰值 RSS、
   临时磁盘量与访问方法；默认小样本，完整规模为 300000 行 / 280 MiB，
   要求 Linux 进程峰值 RSS 不超过 96 MiB。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from archindex import (  # noqa: E402
    AssemblyDataError,
    AssemblyPlanError,
    assemble_nested,
)

MAIN = ROOT / "archindex" / "__main__.py"
PLAN_PATH = ROOT / "examples" / "assembly_plan.example.json"

RSS_LIMIT_MIB = 96


# --------------------------------------------------------------------------
# 1. 交接单演练数据与独立列出的期望
# --------------------------------------------------------------------------

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

# 期望的根对象与孩子身份次序，独立于实现列出：
# 根按 order_no 字典序；箱按 sort_key（NULL 在前）；明细无排序列、按身份；
# 备注按 created。
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


def build_handover_db(path: Path, wal: bool = False) -> None:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(HANDOVER_SCHEMA)
    for sql, params in HANDOVER_ROWS:
        conn.execute(sql, params)
    conn.commit()
    conn.close()


def read_jsonl(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def drill_handover(work: Path) -> None:
    print("=" * 72)
    print("演练 1：交接单 / 箱 / 明细 / 签收备注（四节点方案）")
    print("=" * 72)
    db = work / "handover.sqlite"
    out = work / "handover.jsonl"
    build_handover_db(db)
    result = assemble_nested(str(PLAN_PATH), db, out, temp_dir=work)
    raw = out.read_bytes()
    objects = read_jsonl(out)

    assert result.roots == 2, result
    assert result.node_rows == {"order": 2, "box": 3, "detail": 4,
                                "remark": 3}, result.node_rows
    # 同一根有两个箱和三条独立备注：仍是 1 个根对象、1 个物理行，
    # 不是 2×3=6 个根。
    assert raw.count(b"\n") == 2, raw.count(b"\n")
    # 逐对象核对独立列出的期望。
    assert objects == EXPECTED_HANDOVER, "actual != expected"
    # 空箱仍在，载荷相同的两个明细不去重。
    assert objects[0]["boxes"][1]["details"] == []
    assert objects[0]["boxes"][0]["details"][1] == \
        objects[0]["boxes"][0]["details"][2]
    # 中文换行被转义、整根只有一个物理行；64 位整数精确；金额仍是字符串。
    assert "第一行\\n第二行".encode("utf-8") in raw
    assert "第一行\n第二行".encode("utf-8") not in raw
    assert b'"bigRef":9223372036854775807' in raw
    assert b'"amountText":"123.4500"' in raw
    print(f"根对象数: {result.roots}（两个箱 + 三条备注没有放大成六个根）")
    print(f"各节点行数: {result.node_rows}")
    print(f"物理行数: {raw.count(bytes([10]))}")
    print("逐对象核对：实际文件与独立列出的期望完全一致")
    print("交付值样例（中文换行 / 64位整数 / 金额文本 / 来源列）:")
    first = raw.decode("utf-8").splitlines()[0]
    print("  " + first[:160] + " ...")
    print(f"输出 {result.output_bytes} 字节，耗时 {result.elapsed_seconds:.3f}s")


# --------------------------------------------------------------------------
# 2. 四层方案
# --------------------------------------------------------------------------

def drill_four_level(work: Path) -> None:
    print()
    print("=" * 72)
    print("演练 2：四层方案（区域→仓库→库区→货架）")
    print("=" * 72)
    db = work / "tree.sqlite"
    out = work / "tree.jsonl"
    conn = sqlite3.connect(db)
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
    plan = {"nodes": [
        {"name": "region", "table": "region", "identity": ["region_id"],
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
         "parent": "zone", "array": "shelves", "foreign_key": ["zone_id"],
         "scalars": [{"column": "code", "alias": "code"}],
         "order_by": ["shelf_id"]},
    ]}
    result = assemble_nested(plan, db, out, temp_dir=work)
    objects = read_jsonl(out)
    expected = [
        {"name": "华东", "warehouses": [
            {"name": "上海仓", "zones": [
                {"name": "A区", "shelves": [{"code": "S-01"},
                                            {"code": "S-02"}]},
                {"name": "B区", "shelves": []}]},
            {"name": "杭州仓", "zones": []}]},
        {"name": "华北", "warehouses": []},
    ]
    assert result.roots == 2 and objects == expected, objects
    print(f"四层嵌套输出 {result.roots} 个根，空分支在各层都保留为 []")
    print("  " + json.dumps(objects[0], ensure_ascii=False)[:150] + " ...")


# --------------------------------------------------------------------------
# 3. 失败诊断
# --------------------------------------------------------------------------

def drill_failures(work: Path) -> None:
    print()
    print("=" * 72)
    print("演练 3：失败诊断（每种失败都不留下最终 JSONL）")
    print("=" * 72)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))

    cases: list[tuple[str, list, type, str]] = [
        ("孤儿明细（箱 999 不存在）",
         [("INSERT INTO detail VALUES (999, 999, 'SKU-X', 1, NULL)", None)],
         AssemblyDataError, "orphan"),
        ("重复箱身份",
         [("INSERT INTO box VALUES (10, 1, 'B-88', '重复', NULL)", None)],
         AssemblyDataError, "duplicate"),
        ("空父引用（明细不属于任何箱）",
         [("INSERT INTO detail VALUES (998, NULL, 'SKU-Y', 1, NULL)", None)],
         AssemblyDataError, "null foreign-key"),
        ("错误存储类型（REAL 身份）",
         [("INSERT INTO box VALUES (20.5, 1, 'B-99', '实数身份', NULL)", None)],
         AssemblyDataError, "unsupported storage type"),
    ]
    for label, extra, exc_type, pattern in cases:
        db = work / "case.sqlite"
        out = work / "case.jsonl"
        build_handover_db(db)
        conn = sqlite3.connect(db)
        for sql, params in extra:
            conn.execute(sql, params or ())
        conn.commit()
        conn.close()
        try:
            assemble_nested(plan, db, out, temp_dir=work)
        except exc_type as exc:
            assert pattern in str(exc), str(exc)
            assert not out.exists(), "failure must not leave a final JSONL"
            print(f"  [拒绝] {label}\n         {exc}")
        else:
            raise AssertionError(f"{label}: 应当拒绝却成功了")
        finally:
            db.unlink(missing_ok=True)

    bad_plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    bad_plan["nodes"][1]["array"] = "orderNo"  # 与根标量别名冲突
    db = work / "case.sqlite"
    out = work / "case.jsonl"
    build_handover_db(db)
    try:
        assemble_nested(bad_plan, db, out, temp_dir=work)
    except AssemblyPlanError as exc:
        assert not out.exists()
        print(f"  [拒绝] 配置名冲突（子数组名与标量别名）\n         {exc}")
    else:
        raise AssertionError("配置名冲突应当被拒绝")

    blocker = work / "not-a-dir"
    blocker.write_text("regular file\n", encoding="utf-8")
    try:
        assemble_nested(plan, db, blocker / "out.jsonl", temp_dir=work)
    except OSError as exc:
        assert not (blocker / "out.jsonl").exists()
        print(f"  [拒绝] 无权限/不可写的写出位置\n         {exc}")
    else:
        raise AssertionError("不可写位置应当失败")
    db.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 4. WAL 并发快照
# --------------------------------------------------------------------------

def drill_wal(work: Path) -> None:
    print()
    print("=" * 72)
    print("演练 4：WAL 并发修改下的一致读取")
    print("=" * 72)
    db = work / "wal.sqlite"
    out1 = work / "wal-1.jsonl"
    out2 = work / "wal-2.jsonl"
    build_handover_db(db, wal=True)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    injected = []

    def progress(stage, node, count):
        if stage == "extracted" and node == "order" and not injected:
            injected.append(True)
            other = sqlite3.connect(db)
            other.execute(
                "UPDATE handover_order SET handler='被改写' WHERE order_id=1")
            other.execute(
                "UPDATE detail SET note='并发修改' WHERE detail_id=200")
            other.execute(
                "INSERT INTO handover_order VALUES "
                "(3,'H-000','新单','9.9900',1,1,'m',0)")
            other.commit()
            other.close()

    assemble_nested(plan, db, out1, temp_dir=work, progress=progress)
    first = read_jsonl(out1)
    assert first == EXPECTED_HANDOVER, "当前文件必须仍对应原读取视图"
    print("  导出读到第一根后另一连接改写了后续根与子行；")
    print("  当前文件仍是原读取视图（handler 未被改写、没有新单 H-000）✓")

    assemble_nested(plan, db, out2, temp_dir=work)
    second = read_jsonl(out2)
    assert len(second) == 3 and second[0]["orderNo"] == "H-000"
    changed = [o for o in second if o["orderNo"] == "H-002"][0]
    assert changed["handler"] == "被改写"
    print("  下一次导出才看到新值（H-000 出现、handler 已改写）✓")

    # 导出结束后连接已释放：另一连接可以立刻写入。
    conn = sqlite3.connect(db, timeout=5)
    conn.execute("DELETE FROM handover_order WHERE order_id=3")
    conn.commit()
    conn.close()
    print("  导出结束后源库连接已释放，可立即写入 ✓")


# --------------------------------------------------------------------------
# 5. 压力：单个巨根的有界内存导出
# --------------------------------------------------------------------------

_TOKEN = re.compile(rb"""
    "(?:\\.|[^"\\])*"           # string
  | [{}\[\]:,]                  # punctuation
  | -?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?   # number
  | true | false | null
  | \n                          # JSONL record separator
""", re.VERBOSE)


class StreamJsonlValidator:
    """O(1) 内存的增量 JSONL 校验器：证明输出确实可以流式解析。"""

    def __init__(self) -> None:
        self.buf = b""
        self.stack: list[str] = []
        self.state = "value"
        self.records = 0
        self.objects_at_depth: dict[int, int] = {}

    def _handle(self, tok: bytes) -> None:
        st = self.state
        if st == "value":
            if tok == b"{":
                self.objects_at_depth[len(self.stack)] = \
                    self.objects_at_depth.get(len(self.stack), 0) + 1
                self.stack.append("{")
                self.state = "key"
            elif tok == b"[":
                self.stack.append("[")
            elif tok in (b"}", b"]", b",", b":", b"\n"):
                raise ValueError(f"unexpected {tok!r} in value position")
            else:
                self.state = "post" if self.stack else "line"
            return
        if st == "key":
            if tok == b"}":
                self.stack.pop()
                self.state = "post" if self.stack else "line"
            elif tok.startswith(b'"'):
                self.state = "colon"
            else:
                raise ValueError(f"expected object key, got {tok!r}")
            return
        if st == "colon":
            if tok != b":":
                raise ValueError("expected ':'")
            self.state = "value"
            return
        if st == "post":
            if tok == b",":
                self.state = "key" if self.stack[-1] == "{" else "value"
            elif tok == b"}" and self.stack[-1] == "{":
                self.stack.pop()
                self.state = "post" if self.stack else "line"
            elif tok == b"]" and self.stack[-1] == "[":
                self.stack.pop()
                self.state = "post" if self.stack else "line"
            else:
                raise ValueError(f"unexpected {tok!r} after value")
            return
        if st == "line":
            if tok != b"\n":
                raise ValueError("expected end of record")
            self.records += 1
            self.state = "value"
            return
        raise AssertionError(self.state)

    def feed(self, data: bytes) -> None:
        self.buf += data
        pos = 0
        end = len(self.buf)
        while pos < end:
            ch = self.buf[pos:pos + 1]
            if ch in (b" ", b"\t", b"\r"):
                pos += 1
                continue
            match = _TOKEN.match(self.buf, pos)
            if match is None:
                # 缓冲区末尾的未完成 token（跨块字符串/字面量）留到下一块。
                break
            tok = match.group(0)
            if match.end() == end and tok[:1] in b"-0123456789":
                # 数字在缓冲区末尾可能只是前缀，留到下一块再判定。
                break
            pos = match.end()
            self._handle(tok)
        self.buf = self.buf[pos:]
        if len(self.buf) > 64 * 1024 * 1024:
            raise ValueError("unparseable or oversized token in stream")

    def close(self) -> None:
        if self.buf.strip(b" \t\r"):
            raise ValueError(f"truncated JSON tail {self.buf[:40]!r}")
        if self.stack or self.state not in ("value", "line"):
            raise ValueError("unbalanced JSON stream")


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except FileNotFoundError:
                pass
    return total


def _deleted_temp_bytes(pid: int) -> int:
    """SQLite 排序暂存文件创建后即 unlink，经 /proc/<pid>/fd 计量。"""
    total = 0
    fd_dir = f"/proc/{pid}/fd"
    try:
        for name in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, name))
                if "(deleted)" in target:
                    total += os.stat(os.path.join(fd_dir, name)).st_size
            except OSError:
                continue
    except FileNotFoundError:
        pass
    return total


def _vmhwm_kib(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except FileNotFoundError:
        pass
    return 0


def stress(work: Path, rows: int, mib: int) -> None:
    print()
    print("=" * 72)
    print(f"演练 5：压力 —— 单根 {rows} 条直接孩子，文本载荷目标 >{mib} MiB")
    print("=" * 72)
    db = work / "stress.sqlite"
    out = work / "stress.jsonl"
    plan_path = work / "stress-plan.json"
    temp_dir = work / "scratch"
    temp_dir.mkdir()

    per_row = max(64, mib * 1024 * 1024 // rows)
    body_unit = "压力测试文本ABC-"
    unit_bytes = len(body_unit.encode("utf-8"))
    repeats = max(1, -(-per_row // unit_bytes))  # 向上取整，确保总量达标
    actual_mib = rows * repeats * unit_bytes / 2**20
    print(f"构建源库：1 个根、3 个箱、{rows} 条直接孩子（每条约 "
          f"{repeats * unit_bytes} 字节文本，合计约 {actual_mib:.0f} MiB）...")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE s_order (oid INTEGER, ono TEXT);
    CREATE TABLE s_box (bid INTEGER, oid INTEGER, bno TEXT);
    CREATE TABLE s_item (iid INTEGER, oid INTEGER, seq INTEGER, body TEXT);
    CREATE TABLE s_note (nid INTEGER, oid INTEGER, body TEXT);
    """)
    conn.execute("INSERT INTO s_order VALUES (1, 'STRESS-1')")
    for b in range(3):
        conn.execute("INSERT INTO s_box VALUES (?,?,?)", (b + 1, 1, f"BOX-{b}"))
    conn.execute("INSERT INTO s_note VALUES (1, 1, '首条备注')")
    conn.execute("INSERT INTO s_note VALUES (2, 1, '末条备注')")
    t0 = time.monotonic()
    batch = []
    for i in range(rows):
        batch.append((i, 1, rows - i, f"明细{i:07d}-" + body_unit * repeats))
        if len(batch) >= 5000:
            conn.executemany("INSERT INTO s_item VALUES (?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO s_item VALUES (?,?,?,?)", batch)
    conn.commit()
    print(f"  源库就绪（{time.monotonic() - t0:.1f}s，"
          f"{db.stat().st_size / 2**20:.0f} MiB）")

    plan = {"nodes": [
        {"name": "order", "table": "s_order", "identity": ["oid"],
         "scalars": [{"column": "ono", "alias": "ono"}], "order_by": []},
        {"name": "item", "table": "s_item", "identity": ["iid"],
         "parent": "order", "array": "items", "foreign_key": ["oid"],
         "scalars": [{"column": "iid", "alias": "iid"},
                     {"column": "body", "alias": "body"}],
         "order_by": ["seq"]},
        {"name": "box", "table": "s_box", "identity": ["bid"],
         "parent": "order", "array": "boxes", "foreign_key": ["oid"],
         "scalars": [{"column": "bno", "alias": "bno"}], "order_by": ["bid"]},
        {"name": "note", "table": "s_note", "identity": ["nid"],
         "parent": "order", "array": "notes", "foreign_key": ["oid"],
         "scalars": [{"column": "body", "alias": "body"}], "order_by": ["nid"]},
    ]}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")

    print("以终端命令在子进程中导出（访问方法：只读快照事务 + 调用者目录内")
    print("独立临时 SQLite 建 (父序号,排序列,身份) 有序访问表 + 每层一个")
    print("前向游标 + 有界写出缓冲）...")
    cmd = [sys.executable, "-I", str(MAIN), "assemble",
           "--database", str(db), "--plan", str(plan_path),
           "--output", str(out), "--temp-dir", str(temp_dir)]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    peak_rss_kib = 0
    peak_temp_bytes = 0
    while proc.poll() is None:
        peak_rss_kib = max(peak_rss_kib, _vmhwm_kib(proc.pid))
        peak_temp_bytes = max(
            peak_temp_bytes,
            _dir_bytes(temp_dir) + _deleted_temp_bytes(proc.pid))
        time.sleep(0.05)
    stdout, stderr = proc.communicate()
    elapsed = time.monotonic() - t0
    # 本进程只派生这一个子进程，RUSAGE_CHILDREN 的 ru_maxrss 即其峰值。
    rusage_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak_rss_kib = max(peak_rss_kib, rusage_after)
    if proc.returncode != 0:
        print(stderr)
        raise SystemExit(f"stress export failed: {proc.returncode}")
    summary = json.loads(stdout)

    # 校验：根数、各层行数、首尾孩子、文件可流式解析。
    validator = StreamJsonlValidator()
    lines = 0
    with open(out, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            lines += chunk.count(b"\n")
            validator.feed(chunk)
    validator.close()
    assert lines == 1, f"expected exactly one physical line, got {lines}"
    assert validator.records == 1
    depth = validator.objects_at_depth
    assert depth.get(0) == 1, depth          # 一个根对象
    assert depth.get(2) == rows + 3 + 2, depth   # 孩子 + 箱 + 备注
    with open(out, "rb") as fh:
        head = fh.read(1 << 16)
    with open(out, "rb") as fh:
        fh.seek(max(0, out.stat().st_size - (1 << 16)))
        tail = fh.read()
    first_iid = int(re.search(rb'"iid":(\d+)', head).group(1))
    last_iid = int(re.findall(rb'"iid":(\d+)', tail)[-1])
    # seq = rows - iid：seq 最小者（iid 最大）排最前。
    assert first_iid == rows - 1, first_iid
    assert last_iid == 0, last_iid
    assert summary["roots"] == 1
    assert summary["node_rows"]["item"] == rows

    out_mib = out.stat().st_size / 2**20
    rss_mib = peak_rss_kib / 1024
    print(f"  根数: {summary['roots']}，各层行数: {summary['node_rows']}")
    print(f"  物理行数: {lines}，流式校验通过（深度计数 {depth}）")
    print(f"  首孩子 iid={first_iid}，末孩子 iid={last_iid}")
    print(f"  输出大小: {out_mib:.1f} MiB")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  进程峰值 RSS: {rss_mib:.1f} MiB（上限 {RSS_LIMIT_MIB} MiB）")
    print(f"  临时磁盘峰值: {peak_temp_bytes / 2**20:.1f} MiB"
          f"（含已 unlink 的 SQLite 排序暂存）")
    assert rss_mib <= RSS_LIMIT_MIB, f"RSS {rss_mib:.1f} MiB 超限"
    print(f"  ✓ 单根 {rows} 条孩子在 {RSS_LIMIT_MIB} MiB RSS 内完成")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stress-rows", type=int, default=20000,
                        help="压力演练单个根下的孩子行数（完整规模 300000）")
    parser.add_argument("--stress-mib", type=int, default=24,
                        help="压力演练孩子文本载荷目标 MiB（完整规模 280）")
    parser.add_argument("--keep", action="store_true",
                        help="保留演练目录（默认结束后删除）")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="ax-assembly-demo-"))
    print(f"演练目录: {work}")
    try:
        drill_handover(work)
        drill_four_level(work)
        drill_failures(work)
        drill_wal(work)
        stress(work, args.stress_rows, args.stress_mib)
    finally:
        if args.keep:
            print(f"\n演练目录保留在: {work}")
        else:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
            print("\n演练目录已清理")
    print("\n全部演练通过 ✓")


if __name__ == "__main__":
    raise SystemExit(main())
