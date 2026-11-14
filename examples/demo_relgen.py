#!/usr/bin/env python3
"""可复跑演练：关系型测试数据生成器（按地址生成关联数据）。

从干净环境运行（项目根目录）::

    python3 -I examples/demo_relgen.py                    # 全部演练（含 50 万行压力）
    python3 -I examples/demo_relgen.py --stress-rows 500000

演练内容：

1. 四张表（customer / handover_order / box / detail）、两个指向同一表的
   独立引用列（round_robin 与 hash 互不影响）以及两跳以上的派生
   （detail 经 box 读 order 上的客户标签）。期望行在脚本内以字面量冻结，
   并用一段**独立书写**的地址/派生公式实现（只依据 README 记录的映射
   规则，不复用 archindex.relgen 的代码）逐行核对。
2. 合法外键环可生成可落库；非法跨表派生环在生成前被拒绝并打印环路径。
3. 空父表：可空引用得 NULL（不做取模零），必填引用在开始生成前拒绝。
4. 整数溢出、中文长度（Unicode 码点）与单值 UTF-8 字节上限。
5. 完全相同载荷但不同主键的行各自保留。
6. 换 seed 只影响随机列及其派生值；打乱声明顺序不改数据；
   不同 PYTHONHASHSEED 的子进程结果一致；任意分段/反向/穿插调用与
   一次生成完全相同（不消耗共享随机状态）。
7. 整库落成 SQLite：真实主键/外键、行数、foreign_key_check、派生值
   抽样核对；随后在其上运行现有 assemble 命令与 index 命令。
8. 压力：五十万行分布在四张关联表中，子进程落库，峰值 RSS ≤ 96 MiB。
9. 十亿行虚拟父表：取末端十个孩子，打印实际访问的行数与耗时。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from archindex import (  # noqa: E402
    GenerationError,
    RecipeError,
    RowFactory,
    compile_recipe,
    generate_database,
    load_recipe,
)

MAIN = ROOT / "archindex" / "__main__.py"
RECIPE_PATH = ROOT / "examples" / "relgen_recipe.example.json"
SEED = 42
RSS_LIMIT_MIB = 96

CHECKS = 0


def ok(message: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  ok {CHECKS:02d}: {message}")


# --------------------------------------------------------------------------
# 0. 独立书写的地址与派生公式（只依据文档记录的映射规则）
# --------------------------------------------------------------------------

def ind_digest(seed: int, table: str, column: str, rownum: int,
               purpose: str) -> bytes:
    """SHA-256(域分隔 || u64be(seed) || 长度前缀的表名/列名 UTF-8
    || u64be(行号) || 长度前缀的用途标记) —— 按文档独立实现。"""
    h = hashlib.sha256()
    h.update(b"archindex-relgen\x00v1\x00")
    h.update(seed.to_bytes(8, "big"))
    for part in (table.encode("utf-8"), column.encode("utf-8")):
        h.update(len(part).to_bytes(4, "big"))
        h.update(part)
    h.update(rownum.to_bytes(8, "big"))
    purpose_b = purpose.encode("ascii")
    h.update(len(purpose_b).to_bytes(4, "big"))
    h.update(purpose_b)
    return h.digest()


def ind_u64(seed, table, column, rownum, purpose) -> int:
    return int.from_bytes(
        ind_digest(seed, table, column, rownum, purpose)[:8], "big")


def ind_letters(seed, table, column, rownum, count) -> str:
    base = ind_digest(seed, table, column, rownum, "value")
    out, block_no, block, pos = [], 0, b"", 0
    while len(out) < count:
        if pos >= len(block):
            block = hashlib.sha256(
                base + block_no.to_bytes(4, "big")).digest()
            block_no += 1
            pos = 0
        out.append(chr(ord("a") + block[pos] % 26))
        pos += 1
    return "".join(out)


def ind_customer(i: int) -> dict:
    label = None if ind_u64(SEED, "customer", "label", i, "null") % 4 < 1 \
        else ["金牌", "银牌", "铜牌"][
            ind_u64(SEED, "customer", "label", i, "value") % 3]
    return {
        "customer_id": 1000 + i * 10,
        "tier": 1 + ind_u64(SEED, "customer", "tier", i, "value") % 3,
        "label": label,
        "code": "客户-" + ind_letters(SEED, "customer", "code", i, 5),
    }


def ind_order(i: int) -> dict:
    backup = None
    if ind_u64(SEED, "handover_order", "backup_customer_id", i,
               "refnull") % 4 >= 1:
        backup = 1000 + (ind_u64(SEED, "handover_order",
                                 "backup_customer_id", i, "ref") % 4) * 10
    customer = ind_customer(i % 4)
    return {
        "order_id": 5000 + i,
        "customer_id": customer["customer_id"],
        "backup_customer_id": backup,
        "order_no": "H-" + ind_letters(SEED, "handover_order", "order_no",
                                       i, 4),
        "fee": 10 + ind_u64(SEED, "handover_order", "fee", i,
                            "value") % 90,
        "customer_label": customer["label"],
    }


def ind_box(i: int) -> dict:
    label = None if ind_u64(SEED, "box", "label", i, "null") % 4 < 1 \
        else ["冷链", "普货", "急件"][ind_u64(SEED, "box", "label", i,
                                            "value") % 3]
    order = ind_order(i % 6)
    return {
        "box_id": 7000 + i * 2,
        "order_id": order["order_id"],
        "label": label,
        "order_customer": order["customer_label"],
    }


def ind_detail(i: int) -> dict:
    box = ind_box(ind_u64(SEED, "detail", "box_id", i, "ref") % 9)
    qty = 1 + ind_u64(SEED, "detail", "qty", i, "value") % 5
    price = 100 + ind_u64(SEED, "detail", "unit_price", i, "value") % 900
    tag_source = box["order_customer"]
    return {
        "detail_id": 90000 + i,
        "box_id": box["box_id"],
        "qty": qty,
        "unit_price": price,
        "amount": qty * price,
        "route": None if tag_source is None else "路线:" + tag_source,
        "tag": tag_source if tag_source is not None else "无标签",
    }


# --------------------------------------------------------------------------
# 1. 人工列出并冻结的期望行（四表、两个独立引用、两跳派生）
# --------------------------------------------------------------------------

EXPECTED = {
 "customer": [
  {"customer_id": 1000, "tier": 2, "label": "铜牌", "code": "客户-jxmlk"},
  {"customer_id": 1010, "tier": 3, "label": "铜牌", "code": "客户-zmzlq"},
  {"customer_id": 1020, "tier": 3, "label": "铜牌", "code": "客户-ojqly"},
  {"customer_id": 1030, "tier": 3, "label": None, "code": "客户-lejcg"},
 ],
 "handover_order": [
  {"order_id": 5000, "customer_id": 1000, "backup_customer_id": None,
   "order_no": "H-wcxn", "fee": 84, "customer_label": "铜牌"},
  {"order_id": 5001, "customer_id": 1010, "backup_customer_id": 1010,
   "order_no": "H-otfk", "fee": 47, "customer_label": "铜牌"},
  {"order_id": 5002, "customer_id": 1020, "backup_customer_id": 1020,
   "order_no": "H-rjia", "fee": 31, "customer_label": "铜牌"},
  {"order_id": 5003, "customer_id": 1030, "backup_customer_id": 1030,
   "order_no": "H-cqsw", "fee": 33, "customer_label": None},
  {"order_id": 5004, "customer_id": 1000, "backup_customer_id": None,
   "order_no": "H-vnlg", "fee": 73, "customer_label": "铜牌"},
  {"order_id": 5005, "customer_id": 1010, "backup_customer_id": None,
   "order_no": "H-jmwv", "fee": 38, "customer_label": "铜牌"},
 ],
 "box": [
  {"box_id": 7000, "order_id": 5000, "label": None,
   "order_customer": "铜牌"},
  {"box_id": 7002, "order_id": 5001, "label": None,
   "order_customer": "铜牌"},
  {"box_id": 7004, "order_id": 5002, "label": None,
   "order_customer": "铜牌"},
  {"box_id": 7006, "order_id": 5003, "label": None,
   "order_customer": None},
  {"box_id": 7008, "order_id": 5004, "label": "普货",
   "order_customer": "铜牌"},
  {"box_id": 7010, "order_id": 5005, "label": "急件",
   "order_customer": "铜牌"},
  {"box_id": 7012, "order_id": 5000, "label": "冷链",
   "order_customer": "铜牌"},
  {"box_id": 7014, "order_id": 5001, "label": "急件",
   "order_customer": "铜牌"},
  {"box_id": 7016, "order_id": 5002, "label": "冷链",
   "order_customer": "铜牌"},
 ],
 "detail": [
  {"detail_id": 90000, "box_id": 7014, "qty": 4, "unit_price": 302,
   "amount": 1208, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90001, "box_id": 7006, "qty": 5, "unit_price": 445,
   "amount": 2225, "route": None, "tag": "无标签"},
  {"detail_id": 90002, "box_id": 7008, "qty": 5, "unit_price": 386,
   "amount": 1930, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90003, "box_id": 7014, "qty": 4, "unit_price": 356,
   "amount": 1424, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90004, "box_id": 7008, "qty": 2, "unit_price": 342,
   "amount": 684, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90005, "box_id": 7010, "qty": 4, "unit_price": 516,
   "amount": 2064, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90006, "box_id": 7016, "qty": 5, "unit_price": 633,
   "amount": 3165, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90007, "box_id": 7004, "qty": 5, "unit_price": 923,
   "amount": 4615, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90008, "box_id": 7014, "qty": 1, "unit_price": 902,
   "amount": 902, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90009, "box_id": 7014, "qty": 2, "unit_price": 777,
   "amount": 1554, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90010, "box_id": 7010, "qty": 3, "unit_price": 732,
   "amount": 2196, "route": "路线:铜牌", "tag": "铜牌"},
  {"detail_id": 90011, "box_id": 7008, "qty": 2, "unit_price": 615,
   "amount": 1230, "route": "路线:铜牌", "tag": "铜牌"},
 ],
}


def section_expected_rows() -> None:
    print("== 1. 四表期望行：冻结字面量 + 独立公式 + 生成器三方核对")
    recipe = load_recipe(RECIPE_PATH)
    factory = RowFactory(recipe, SEED)
    independent = {"customer": [ind_customer(i) for i in range(4)],
                   "handover_order": [ind_order(i) for i in range(6)],
                   "box": [ind_box(i) for i in range(9)],
                   "detail": [ind_detail(i) for i in range(12)]}
    # 独立公式与冻结字面量一致（证明字面量不是生成器的回声）。
    assert independent == EXPECTED, "独立公式与冻结期望不一致"
    for table in recipe.tables:
        for i in range(table.row_count):
            got = factory.row(table.name, i)
            assert got == EXPECTED[table.name][i], (table.name, i, got)
    ok("31 行期望（含两个独立引用列与两跳派生）逐行命中")

    # 两个指向 customer 的引用列各自独立选择。
    rr = [EXPECTED["handover_order"][i]["customer_id"] for i in range(6)]
    hh = [EXPECTED["handover_order"][i]["backup_customer_id"]
          for i in range(6)]
    assert rr == [1000, 1010, 1020, 1030, 1000, 1010]
    assert hh != rr
    ok("round_robin 引用按子行号轮转，hash 引用独立选择，互不绑定")

    # 只计算实际依赖的父行：一条 detail 恰好访问 4 行。
    fresh = RowFactory(recipe, SEED)
    fresh.row("detail", 0)
    assert fresh.rows_computed == 4, fresh.rows_computed
    ok("取一条 detail 只访问 4 行（detail/box/order/customer 各一）")


def section_cycles_and_empty() -> None:
    print("== 2. 合法外键环 vs 非法派生环；空父表")
    cycle = {"tables": [
        {"name": "A", "row_count": 5,
         "primary_key": {"column": "a_id", "base": 0, "stride": 1},
         "columns": [
             {"name": "b_id", "type": "integer",
              "reference": {"table": "B", "select": "round_robin",
                            "nullable": False}}]},
        {"name": "B", "row_count": 3,
         "primary_key": {"column": "b_id", "base": 100, "stride": 1},
         "columns": [
             {"name": "a_id", "type": "integer",
              "reference": {"table": "A", "select": "round_robin",
                            "nullable": False}}]}]}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cycle.sqlite"
        result = generate_database(compile_recipe(cycle), 7, out)
        assert result.table_rows == {"A": 5, "B": 3}
        conn = sqlite3.connect(out)
        try:
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()
    ok("A↔B 外键环（仅普通列）正常生成并落库，foreign_key_check 干净")

    bad = {"tables": [
        {"name": "A", "row_count": 5,
         "primary_key": {"column": "a_id", "base": 0, "stride": 1},
         "columns": [
             {"name": "b_id", "type": "integer",
              "reference": {"table": "B", "select": "hash",
                            "nullable": True}},
             {"name": "x", "type": "integer",
              "expression": {"op": "coalesce",
                             "args": [{"op": "ref", "column": "b_id",
                                       "target_column": "y"},
                                      {"op": "lit", "value": 0}]}}]},
        {"name": "B", "row_count": 3,
         "primary_key": {"column": "b_id", "base": 100, "stride": 1},
         "columns": [
             {"name": "a_id", "type": "integer",
              "reference": {"table": "A", "select": "hash",
                            "nullable": True}},
             {"name": "y", "type": "integer",
              "expression": {"op": "coalesce",
                             "args": [{"op": "ref", "column": "a_id",
                                       "target_column": "x"},
                                      {"op": "lit", "value": 1}]}}]}]}
    try:
        compile_recipe(bad)
        raise AssertionError("派生环未被拒绝")
    except RecipeError as exc:
        message = str(exc)
        assert "A.x" in message and "B.y" in message
        print(f"     拒绝信息: {message}")
    ok("跨表派生环在生成前被拒绝（即使 coalesce 可能短路也不例外）")

    empty_parent = {"name": "empty", "row_count": 0,
                    "primary_key": {"column": "id", "base": 0,
                                    "stride": 1}, "columns": []}
    nullable_doc = {"tables": [
        {"name": "c", "row_count": 4,
         "primary_key": {"column": "id", "base": 0, "stride": 1},
         "columns": [
             {"name": "p", "type": "integer",
              "reference": {"table": "empty", "select": "round_robin",
                            "nullable": True}}]},
        empty_parent]}
    factory = RowFactory(compile_recipe(nullable_doc), SEED)
    assert [factory.row("c", i)["p"] for i in range(4)] == [None] * 4
    ok("可空引用指向空表得到 NULL（无取模零、无虚构父键）")

    required_doc = {"tables": [
        {"name": "c", "row_count": 4,
         "primary_key": {"column": "id", "base": 0, "stride": 1},
         "columns": [
             {"name": "p", "type": "integer",
              "reference": {"table": "empty", "select": "hash",
                            "nullable": False}}]},
        empty_parent]}
    try:
        compile_recipe(required_doc)
        raise AssertionError("必填引用空表未被拒绝")
    except RecipeError as exc:
        print(f"     拒绝信息: {exc}")
    ok("必填引用指向空表在开始生成前拒绝")


def section_overflow_and_text() -> None:
    print("== 3. 整数溢出定位；中文长度与 UTF-8 字节上限")
    overflow = {"tables": [
        {"name": "t", "row_count": 3,
         "primary_key": {"column": "id", "base": 0, "stride": 1},
         "columns": [
             {"name": "big", "type": "integer",
              "generator": {"kind": "int_range",
                            "min": 9223372036854775800,
                            "max": 9223372036854775807}},
             {"name": "boom", "type": "integer",
              "expression": {"op": "add",
                             "args": [{"op": "col", "name": "big"},
                                      {"op": "col", "name": "big"}]}}]}]}
    factory = RowFactory(compile_recipe(overflow), SEED)
    try:
        factory.row("t", 1)
        raise AssertionError("溢出未被报告")
    except GenerationError as exc:
        assert exc.table == "t" and exc.row == 1
        assert "t[1].boom" in exc.path
        print(f"     定位信息: {exc}")
    ok("整数溢出定位到表、行号、列与依赖路径，不转浮点")

    chinese = {"tables": [
        {"name": "t", "row_count": 2,
         "primary_key": {"column": "id", "base": 0, "stride": 1},
         "columns": [
             {"name": "s", "type": "text",
              "generator": {"kind": "text", "prefix": "客户-",
                            "length": 5},
              "max_utf8_bytes": 9}]}]}
    row = RowFactory(compile_recipe(chinese), SEED).row("t", 0)
    assert len(row["s"]) == 5                      # Unicode 码点
    assert len(row["s"].encode("utf-8")) == 9      # 客户- 7 字节 + 2 字母
    too_tight = json.loads(json.dumps(chinese))
    too_tight["tables"][0]["columns"][0]["max_utf8_bytes"] = 8
    try:
        compile_recipe(too_tight)
        raise AssertionError("字节上限未被强制")
    except RecipeError as exc:
        print(f"     拒绝信息: {exc}")
    ok("中文前缀按码点计长、按 UTF-8 字节限长（9 字节放行，8 字节拒绝）")


def section_identical_payload() -> None:
    print("== 4. 完全相同载荷但不同主键")
    doc = {"tables": [
        {"name": "t", "row_count": 4,
         "primary_key": {"column": "id", "base": 100, "stride": 5},
         "columns": [
             {"name": "fixed_int", "type": "integer",
              "generator": {"kind": "int_range", "min": 8, "max": 8}},
             {"name": "fixed_text", "type": "text",
              "generator": {"kind": "choice", "values": ["only"]}}]}]}
    recipe = compile_recipe(doc)
    factory = RowFactory(recipe, SEED)
    rows = [factory.row("t", i) for i in range(4)]
    payloads = [{k: v for k, v in r.items() if k != "id"} for r in rows]
    assert payloads == [payloads[0]] * 4
    assert [r["id"] for r in rows] == [100, 105, 110, 115]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "same.sqlite"
        generate_database(recipe, SEED, out)
        conn = sqlite3.connect(out)
        try:
            count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            distinct_payloads = conn.execute(
                "SELECT COUNT(DISTINCT fixed_int || '|' || fixed_text) "
                "FROM t").fetchone()[0]
        finally:
            conn.close()
    assert count == 4 and distinct_payloads == 1
    ok("四行载荷完全相同、主键各异，全部保留在库中")


def section_determinism() -> None:
    print("== 5. seed 敏感性 / 声明顺序 / PYTHONHASHSEED / 分段等价")
    recipe = load_recipe(RECIPE_PATH)
    f1, f2 = RowFactory(recipe, 1), RowFactory(recipe, 2)
    seed_independent = {("customer", "customer_id"),
                        ("handover_order", "order_id"),
                        ("handover_order", "customer_id"),
                        ("box", "box_id"), ("box", "order_id"),
                        ("detail", "detail_id")}
    differences = set()
    for table in recipe.tables:
        for i in range(table.row_count):
            r1, r2 = f1.row(table.name, i), f2.row(table.name, i)
            for column in r1:
                key = (table.name, column)
                if key in seed_independent:
                    assert r1[column] == r2[column], key
                elif r1[column] != r2[column]:
                    differences.add(key)
    assert differences, "换 seed 没有任何随机列变化"
    ok(f"换 seed：主键与 round_robin 引用不变，"
       f"{len(differences)} 个随机/派生列发生变化")

    shuffled = json.loads(json.dumps(json.loads(
        RECIPE_PATH.read_text(encoding="utf-8"))))
    shuffled["tables"].reverse()
    for table in shuffled["tables"]:
        table["columns"].reverse()
    f3 = RowFactory(compile_recipe(shuffled), 1)
    for table in recipe.tables:
        for i in range(table.row_count):
            assert f1.row(table.name, i) == f3.row(table.name, i)
    ok("打乱表与列的声明顺序，全部 31 行数据不变")

    outputs = []
    for hashseed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        proc = subprocess.run(
            [sys.executable, "-I", str(MAIN), "gen-sample",
             "--recipe", str(RECIPE_PATH), "--seed", str(SEED),
             "--table", "detail", "--rows", "0:12"],
            capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    ok("PYTHONHASHSEED=0/1/12345 的子进程输出逐字节一致")

    factory = RowFactory(recipe, SEED)
    one_shot = [row for _, row in factory.rows("detail", 0, 12)]
    fresh = RowFactory(recipe, SEED, cache_rows=3)
    segments = {}
    segments[(9, 12)] = [row for _, row in fresh.rows("detail", 9, 12)]
    list(fresh.rows("box", 0, 9))            # 穿插别表取样
    segments[(0, 5)] = [row for _, row in fresh.rows("detail", 0, 5)]
    fresh.row("customer", 2)                 # 再穿插单行
    segments[(5, 9)] = [row for _, row in fresh.rows("detail", 5, 9)]
    stitched = segments[(0, 5)] + segments[(5, 9)] + segments[(9, 12)]
    assert stitched == one_shot
    try:
        list(factory.rows("detail", 10, 13))
        raise AssertionError("越界区间未被拒绝")
    except GenerationError:
        pass
    ok("反向分段 + 穿插取样后拼回与一次生成相同；越界区间报错")


def section_database_and_existing_commands(work: Path) -> None:
    print("== 6. 整库落盘核对 + 现有 assemble/index 命令")
    recipe = load_recipe(RECIPE_PATH)
    db = work / "handover.sqlite"
    result = generate_database(recipe, SEED, db)
    assert result.table_rows == {"customer": 4, "handover_order": 6,
                                 "box": 9, "detail": 12}
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        fk = conn.execute("PRAGMA foreign_key_list(detail)").fetchall()
        assert fk and fk[0][2] == "box"
        for i in (0, 5, 11):
            got = conn.execute(
                "SELECT amount, route, tag FROM detail WHERE detail_id = ?",
                (90000 + i,)).fetchone()
            want = EXPECTED["detail"][i]
            assert got == (want["amount"], want["route"], want["tag"])
    finally:
        conn.close()
    ok("行数、真实外键、foreign_key_check 与派生值抽样全部核对通过")

    plan = {"nodes": [
        {"name": "order", "table": "handover_order",
         "identity": ["order_id"],
         "scalars": [{"column": "order_no", "alias": "orderNo"},
                     {"column": "fee", "alias": "fee"}],
         "order_by": ["order_id"]},
        {"name": "box", "table": "box", "identity": ["box_id"],
         "parent": "order", "array": "boxes",
         "foreign_key": ["order_id"],
         "scalars": [{"column": "box_id", "alias": "boxId"},
                     {"column": "order_customer", "alias": "customer"}],
         "order_by": ["box_id"]},
        {"name": "detail", "table": "detail", "identity": ["detail_id"],
         "parent": "box", "array": "details",
         "foreign_key": ["box_id"],
         "scalars": [{"column": "amount", "alias": "amount"},
                     {"column": "tag", "alias": "tag"}],
         "order_by": ["detail_id"]}]}
    plan_path = work / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    out_jsonl = work / "nested.jsonl"
    proc = subprocess.run(
        [sys.executable, "-I", str(MAIN), "assemble",
         "--database", str(db), "--plan", str(plan_path),
         "--output", str(out_jsonl)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["roots"] == 6
    assert summary["node_rows"] == {"order": 6, "box": 9, "detail": 12}
    ok("assemble 命令在生成的库上组装出 6 个根、27 行节点")

    import zipfile
    archive = work / "sample.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "alpha")
        zf.writestr("dir/b.txt", "beta")
    proc = subprocess.run([sys.executable, "-I", str(MAIN), "index",
                           str(archive)], capture_output=True, text=True)
    assert proc.returncode == 0 and "a.txt" in proc.stdout
    ok("index 命令照常工作（现有功能未受影响）")


# --------------------------------------------------------------------------
# 7. 压力：五十万行 / 四张关联表 / 峰值 RSS <= 96 MiB
# --------------------------------------------------------------------------

def stress_recipe(rows: int) -> dict:
    customers = max(4, rows // 100)
    orders = max(6, rows * 9 // 100)
    boxes = max(9, rows * 3 // 10)
    details = rows - customers - orders - boxes
    return {"tables": [
        {"name": "customer", "row_count": customers,
         "primary_key": {"column": "customer_id", "base": 1000,
                         "stride": 10},
         "columns": [
             {"name": "tier", "type": "integer",
              "generator": {"kind": "int_range", "min": 1, "max": 3}},
             {"name": "label", "type": "text",
              "generator": {"kind": "choice",
                            "values": ["金牌", "银牌", "铜牌"]},
              "null_ratio": {"numerator": 1, "denominator": 4}},
             {"name": "code", "type": "text",
              "generator": {"kind": "text", "prefix": "客户-",
                            "length": 12}}]},
        {"name": "handover_order", "row_count": orders,
         "primary_key": {"column": "order_id", "base": 5000, "stride": 1},
         "columns": [
             {"name": "customer_id", "type": "integer",
              "reference": {"table": "customer", "select": "round_robin",
                            "nullable": False}},
             {"name": "order_no", "type": "text",
              "generator": {"kind": "text", "prefix": "H-", "length": 10}},
             {"name": "fee", "type": "integer",
              "generator": {"kind": "int_range", "min": 10, "max": 99}},
             {"name": "customer_label", "type": "text",
              "expression": {"op": "ref", "column": "customer_id",
                             "target_column": "label"}}]},
        {"name": "box", "row_count": boxes,
         "primary_key": {"column": "box_id", "base": 7000, "stride": 2},
         "columns": [
             {"name": "order_id", "type": "integer",
              "reference": {"table": "handover_order",
                            "select": "round_robin", "nullable": False}},
             {"name": "label", "type": "text",
              "generator": {"kind": "choice",
                            "values": ["冷链", "普货", "急件"]}},
             {"name": "order_customer", "type": "text",
              "expression": {"op": "ref", "column": "order_id",
                             "target_column": "customer_label"}}]},
        {"name": "detail", "row_count": details,
         "primary_key": {"column": "detail_id", "base": 90000,
                         "stride": 1},
         "columns": [
             {"name": "box_id", "type": "integer",
              "reference": {"table": "box", "select": "hash",
                            "nullable": False}},
             {"name": "qty", "type": "integer",
              "generator": {"kind": "int_range", "min": 1, "max": 5}},
             {"name": "unit_price", "type": "integer",
              "generator": {"kind": "int_range", "min": 100, "max": 999}},
             {"name": "amount", "type": "integer",
              "expression": {"op": "mul",
                             "args": [{"op": "col", "name": "qty"},
                                      {"op": "col",
                                       "name": "unit_price"}]}},
             {"name": "tag", "type": "text",
              "expression": {"op": "coalesce",
                             "args": [{"op": "ref", "column": "box_id",
                                       "target_column": "order_customer"},
                                      {"op": "lit",
                                       "value": "无标签"}]}}]}]}


STRESS_CHILD = r"""
import json, resource, sys, time
sys.path.insert(0, sys.argv[1])
from archindex.relgen import compile_recipe, generate_database
recipe = json.loads(sys.argv[2])
started = time.monotonic()
result = generate_database(compile_recipe(recipe), int(sys.argv[3]),
                           sys.argv[4])
rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"table_rows": result.table_rows,
                  "verified_rows": result.verified_rows,
                  "elapsed_seconds": round(time.monotonic() - started, 3),
                  "peak_rss_kb": rss_kb}))
"""


def section_stress(rows: int, work: Path) -> None:
    print(f"== 7. 压力：{rows} 行 / 四张关联表 / 峰值 RSS ≤ {RSS_LIMIT_MIB} "
          "MiB（子进程落库）")
    recipe = stress_recipe(rows)
    db = work / "stress.sqlite"
    proc = subprocess.run(
        [sys.executable, "-I", "-c", STRESS_CHILD, str(ROOT),
         json.dumps(recipe), str(SEED), str(db)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    metrics = json.loads(proc.stdout)
    total = sum(metrics["table_rows"].values())
    peak_mib = metrics["peak_rss_kb"] / 1024
    print(f"     行数分布: {metrics['table_rows']} (合计 {total})")
    print(f"     耗时 {metrics['elapsed_seconds']}s, "
          f"峰值 RSS {peak_mib:.1f} MiB, "
          f"抽样核对 {metrics['verified_rows']} 行")
    assert total == rows
    assert peak_mib <= RSS_LIMIT_MIB, peak_mib

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, want in metrics["table_rows"].items():
            got = conn.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            assert got == want
    finally:
        conn.close()

    # 用生成器（非数据库自身）抽样核对末端派生值。
    factory = RowFactory(compile_recipe(recipe), SEED)
    detail_n = metrics["table_rows"]["detail"]
    conn = sqlite3.connect(db)
    try:
        for i in (0, detail_n // 2, detail_n - 1):
            want = factory.row("detail", i)
            got = conn.execute(
                "SELECT box_id, amount, tag FROM detail "
                "WHERE detail_id = ?", (90000 + i,)).fetchone()
            assert got == (want["box_id"], want["amount"], want["tag"]), i
    finally:
        conn.close()
    ok(f"{rows} 行落库：行数、foreign_key_check、末端派生值核对通过；"
       f"峰值 RSS {peak_mib:.1f} MiB ≤ {RSS_LIMIT_MIB} MiB")

    plan = {"nodes": [
        {"name": "order", "table": "handover_order",
         "identity": ["order_id"],
         "scalars": [{"column": "order_no", "alias": "orderNo"}],
         "order_by": ["order_id"]},
        {"name": "box", "table": "box", "identity": ["box_id"],
         "parent": "order", "array": "boxes",
         "foreign_key": ["order_id"],
         "scalars": [{"column": "box_id", "alias": "boxId"}],
         "order_by": ["box_id"]},
        {"name": "detail", "table": "detail", "identity": ["detail_id"],
         "parent": "box", "array": "details",
         "foreign_key": ["box_id"],
         "scalars": [{"column": "amount", "alias": "amount"}],
         "order_by": ["detail_id"]}]}
    plan_path = work / "stress-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    out_jsonl = work / "stress.jsonl"
    proc = subprocess.run(
        [sys.executable, "-I", str(MAIN), "assemble",
         "--database", str(db), "--plan", str(plan_path),
         "--output", str(out_jsonl)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["roots"] == metrics["table_rows"]["handover_order"]
    assert summary["node_rows"]["detail"] == metrics["table_rows"]["detail"]
    ok(f"assemble 在压力库上组装 {summary['roots']} 个根，"
       f"各层行数 {summary['node_rows']}")


def section_billion_parent() -> None:
    print("== 8. 十亿行虚拟父表：取末端十个孩子")
    doc = {"tables": [
        {"name": "big_customer", "row_count": 1_000_000_000,
         "primary_key": {"column": "customer_id",
                         "base": 10_000_000_000, "stride": 3},
         "columns": [
             {"name": "tier", "type": "integer",
              "generator": {"kind": "int_range", "min": 1, "max": 3}},
             {"name": "label", "type": "text",
              "generator": {"kind": "choice",
                            "values": ["金牌", "银牌", "铜牌"]}}]},
        {"name": "big_order", "row_count": 2_000_000_000,
         "primary_key": {"column": "order_id", "base": 1, "stride": 1},
         "columns": [
             {"name": "customer_id", "type": "integer",
              "reference": {"table": "big_customer",
                            "select": "round_robin", "nullable": False}},
             {"name": "customer_label", "type": "text",
              "expression": {"op": "ref", "column": "customer_id",
                             "target_column": "label"}}]}]}
    factory = RowFactory(compile_recipe(doc), SEED)
    started = time.monotonic()
    rows = [factory.row("big_order", i)
            for i in range(1_999_999_990, 2_000_000_000)]
    elapsed = time.monotonic() - started
    for offset, row in enumerate(rows):
        i = 1_999_999_990 + offset
        assert row["order_id"] == 1 + i
        parent_row = i % 1_000_000_000
        want_key = 10_000_000_000 + parent_row * 3
        assert row["customer_id"] == want_key
        assert isinstance(row["customer_id"], int)  # 未经浮点转换
        want_label = ["金牌", "银牌", "铜牌"][
            ind_u64(SEED, "big_customer", "label", parent_row, "value") % 3]
        assert row["customer_label"] == want_label
    print(f"     访问行数 rows_computed = {factory.rows_computed} "
          f"(10 个孩子 + 10 个父行)，耗时 {elapsed * 1000:.1f} ms")
    assert factory.rows_computed == 20
    ok("末端十个孩子瞬时取得：只访问 20 行，大整数行号保持精确整数")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stress-rows", type=int, default=500_000,
                        help="压力演练总行数（默认 500000）")
    args = parser.parse_args()

    section_expected_rows()
    section_cycles_and_empty()
    section_overflow_and_text()
    section_identical_payload()
    section_determinism()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        section_database_and_existing_commands(work)
        section_stress(args.stress_rows, work)
    section_billion_parent()
    print(f"\n全部 {CHECKS} 项演练通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
