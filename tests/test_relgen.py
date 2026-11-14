"""Tests for the address-driven relational test-data generator."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from archindex import (
    GenerationError,
    RecipeError,
    RowFactory,
    compile_recipe,
    generate_database,
    generate_row,
    generate_rows,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "archindex", "__main__.py")
EXAMPLE_RECIPE = os.path.join(ROOT, "examples", "relgen_recipe.example.json")

SEED = 424242


def handover_recipe() -> dict:
    """Four tables, two independent references, multi-hop derivations."""
    return {
        "tables": [
            {"name": "customer", "row_count": 4,
             "primary_key": {"column": "customer_id", "base": 1000,
                             "stride": 10},
             "columns": [
                 {"name": "tier", "type": "integer",
                  "generator": {"kind": "int_range", "min": 1, "max": 3}},
                 {"name": "label", "type": "text",
                  "generator": {"kind": "choice",
                                "values": ["金牌", "银牌", "铜牌"]}}]},
            {"name": "handover_order", "row_count": 6,
             "primary_key": {"column": "order_id", "base": 5000,
                             "stride": 1},
             "columns": [
                 {"name": "customer_id", "type": "integer",
                  "reference": {"table": "customer",
                                "select": "round_robin", "nullable": False}},
                 {"name": "fee", "type": "integer",
                  "generator": {"kind": "int_range", "min": 10, "max": 99}},
                 {"name": "customer_tier", "type": "integer",
                  "expression": {"op": "ref", "column": "customer_id",
                                 "target_column": "tier"}}]},
            {"name": "box", "row_count": 9,
             "primary_key": {"column": "box_id", "base": 7000, "stride": 2},
             "columns": [
                 {"name": "order_id", "type": "integer",
                  "reference": {"table": "handover_order",
                                "select": "round_robin", "nullable": False}},
                 {"name": "order_tier", "type": "integer",
                  "expression": {"op": "ref", "column": "order_id",
                                 "target_column": "customer_tier"}}]},
            {"name": "detail", "row_count": 12,
             "primary_key": {"column": "detail_id", "base": 90000,
                             "stride": 1},
             "columns": [
                 {"name": "box_id", "type": "integer",
                  "reference": {"table": "box", "select": "hash",
                                "nullable": False}},
                 {"name": "qty", "type": "integer",
                  "generator": {"kind": "int_range", "min": 1, "max": 5}},
                 {"name": "price", "type": "integer",
                  "generator": {"kind": "int_range", "min": 100,
                                "max": 999}},
                 {"name": "amount", "type": "integer",
                  "expression": {"op": "mul",
                                 "args": [{"op": "col", "name": "qty"},
                                          {"op": "col", "name": "price"}]}},
                 {"name": "box_tier", "type": "integer",
                  "expression": {"op": "ref", "column": "box_id",
                                 "target_column": "order_tier"}}]},
        ]
    }


def one_table(**overrides) -> dict:
    table = {"name": "t", "row_count": 10,
             "primary_key": {"column": "id", "base": 0, "stride": 1},
             "columns": []}
    table.update(overrides)
    return {"tables": [table]}


class AddressingTests(unittest.TestCase):

    def test_primary_key_formula(self):
        recipe = compile_recipe(one_table(
            row_count=5,
            primary_key={"column": "id", "base": -20, "stride": 7}))
        factory = RowFactory(recipe, SEED)
        for i in range(5):
            self.assertEqual(factory.row("t", i)["id"], -20 + i * 7)

    def test_empty_table_is_legal(self):
        recipe = compile_recipe(one_table(row_count=0))
        factory = RowFactory(recipe, SEED)
        self.assertEqual(list(factory.rows("t", 0, 0)), [])
        with self.assertRaises(GenerationError):
            factory.row("t", 0)

    def test_out_of_range_addresses(self):
        recipe = compile_recipe(one_table(row_count=3))
        factory = RowFactory(recipe, SEED)
        for bad in (-1, 3, 10 ** 20):
            with self.assertRaises(GenerationError):
                factory.row("t", bad)
        with self.assertRaises(GenerationError):
            list(factory.rows("t", 2, 1))
        with self.assertRaises(GenerationError):
            list(factory.rows("t", 0, 4))
        with self.assertRaises(GenerationError):
            list(factory.rows("t", -1, 2))

    def test_unknown_table(self):
        recipe = compile_recipe(one_table())
        with self.assertRaises(GenerationError):
            RowFactory(recipe, SEED).row("nope", 0)

    def test_key_range_validation(self):
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(
                primary_key={"column": "id", "base": 0, "stride": 0}))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(
                primary_key={"column": "id", "base": 0, "stride": -3}))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(
                row_count=3,
                primary_key={"column": "id",
                             "base": 9223372036854775806, "stride": 2}))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(
                primary_key={"column": "id", "base": 2 ** 63, "stride": 1}))
        # Exactly at the boundary is fine.
        compile_recipe(one_table(
            row_count=2,
            primary_key={"column": "id", "base": 9223372036854775806,
                         "stride": 1}))
        compile_recipe(one_table(
            row_count=2,
            primary_key={"column": "id", "base": -9223372036854775808,
                         "stride": 1}))

    def test_negative_row_count_and_bad_seed(self):
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(row_count=-1))
        recipe = compile_recipe(one_table())
        for bad in (-1, 2 ** 64, 1.5, "7", True):
            with self.assertRaises(GenerationError):
                RowFactory(recipe, bad)

    def test_duplicate_names_rejected(self):
        with self.assertRaises(RecipeError):
            compile_recipe({"tables": [one_table()["tables"][0],
                                       one_table()["tables"][0]]})
        dup_col = one_table(columns=[
            {"name": "a", "type": "integer",
             "generator": {"kind": "null"}},
            {"name": "a", "type": "integer",
             "generator": {"kind": "null"}}])
        with self.assertRaises(RecipeError):
            compile_recipe(dup_col)
        pk_dup = one_table(
            primary_key={"column": "id", "base": 0, "stride": 1},
            columns=[{"name": "id", "type": "integer",
                      "generator": {"kind": "null"}}])
        with self.assertRaises(RecipeError):
            compile_recipe(pk_dup)

    def test_names_with_spaces_and_quotes(self):
        recipe = compile_recipe({
            "tables": [
                {"name": "we \"ird\" table", "row_count": 3,
                 "primary_key": {"column": "pk col", "base": 5, "stride": 2},
                 "columns": [
                     {"name": "a \"quoted\" col", "type": "text",
                      "generator": {"kind": "text", "prefix": "x",
                                    "length": 3}},
                     {"name": "ref col", "type": "integer",
                      "reference": {"table": "we \"ird\" table",
                                    "select": "round_robin",
                                    "nullable": False}}]}]})
        factory = RowFactory(recipe, SEED)
        row = factory.row('we "ird" table', 1)
        self.assertEqual(row["pk col"], 7)
        self.assertEqual(row["ref col"], 5 + (1 % 3) * 2)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "q.sqlite")
            generate_database(recipe, SEED, out)
            conn = sqlite3.connect(out)
            try:
                got = conn.execute(
                    'SELECT "pk col", "a ""quoted"" col" FROM '
                    '"we ""ird"" table" ORDER BY "pk col"').fetchall()
                self.assertEqual([g[0] for g in got], [5, 7, 9])
            finally:
                conn.close()

    def test_extra_keys_rejected(self):
        with self.assertRaises(RecipeError):
            compile_recipe({"tables": [], "surprise": 1})
        bad = one_table()
        bad["tables"][0]["bogus"] = 1
        with self.assertRaises(RecipeError):
            compile_recipe(bad)


class GeneratorColumnTests(unittest.TestCase):

    def test_int_range(self):
        recipe = compile_recipe(one_table(row_count=200, columns=[
            {"name": "v", "type": "integer",
             "generator": {"kind": "int_range", "min": -3, "max": 4}}]))
        factory = RowFactory(recipe, SEED)
        values = [factory.row("t", i)["v"] for i in range(200)]
        self.assertTrue(all(-3 <= v <= 4 for v in values))
        self.assertGreater(len(set(values)), 1)
        again = RowFactory(recipe, SEED)
        self.assertEqual(values,
                         [again.row("t", i)["v"] for i in range(200)])

    def test_int_range_constant(self):
        recipe = compile_recipe(one_table(columns=[
            {"name": "v", "type": "integer",
             "generator": {"kind": "int_range", "min": 7, "max": 7}}]))
        factory = RowFactory(recipe, SEED)
        self.assertEqual([factory.row("t", i)["v"] for i in range(10)],
                         [7] * 10)

    def test_int_range_validation(self):
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "v", "type": "integer",
                 "generator": {"kind": "int_range", "min": 5, "max": 2}}]))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "v", "type": "text",
                 "generator": {"kind": "int_range", "min": 1, "max": 2}}]))

    def test_choice(self):
        recipe = compile_recipe(one_table(row_count=100, columns=[
            {"name": "c", "type": "text",
             "generator": {"kind": "choice", "values": ["甲", "乙"]}}]))
        factory = RowFactory(recipe, SEED)
        values = {factory.row("t", i)["c"] for i in range(100)}
        self.assertEqual(values, {"甲", "乙"})

    def test_choice_validation(self):
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "c", "type": "text",
                 "generator": {"kind": "choice", "values": []}}]))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "c", "type": "text",
                 "generator": {"kind": "choice", "values": ["a", 1]}}]))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "c", "type": "text",
                 "generator": {"kind": "choice", "values": [True, False]}}]))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "c", "type": "integer",
                 "generator": {"kind": "choice", "values": ["a"]}}]))

    def test_text_generator(self):
        recipe = compile_recipe(one_table(row_count=50, columns=[
            {"name": "s", "type": "text",
             "generator": {"kind": "text", "prefix": "前缀-", "length": 9},
             "max_utf8_bytes": 64}]))
        factory = RowFactory(recipe, SEED)
        for i in range(50):
            value = factory.row("t", i)["s"]
            self.assertTrue(value.startswith("前缀-"))
            self.assertEqual(len(value), 9)  # Unicode code points
            suffix = value[3:]
            self.assertTrue(suffix.isascii() and suffix.islower()
                            and suffix.isalpha())
            self.assertLessEqual(len(value.encode("utf-8")), 64)

    def test_text_validation(self):
        # length smaller than the prefix (in code points)
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "s", "type": "text",
                 "generator": {"kind": "text", "prefix": "客户-", "length": 2}}]))
        # UTF-8 byte cap: 客户- is 7 bytes, plus 2 suffix letters = 9 > 8
        with self.assertRaises(RecipeError) as ctx:
            compile_recipe(one_table(columns=[
                {"name": "s", "type": "text",
                 "generator": {"kind": "text", "prefix": "客户-",
                               "length": 5},
                 "max_utf8_bytes": 8}]))
        self.assertIn("UTF-8", str(ctx.exception))
        # 9 bytes fits exactly
        compile_recipe(one_table(columns=[
            {"name": "s", "type": "text",
             "generator": {"kind": "text", "prefix": "客户-", "length": 5},
             "max_utf8_bytes": 9}]))

    def test_null_generator(self):
        recipe = compile_recipe(one_table(columns=[
            {"name": "n", "type": "integer",
             "generator": {"kind": "null"}}]))
        factory = RowFactory(recipe, SEED)
        self.assertEqual([factory.row("t", i)["n"] for i in range(10)],
                         [None] * 10)

    def test_null_ratio_extremes(self):
        recipe = compile_recipe(one_table(row_count=300, columns=[
            {"name": "never", "type": "integer",
             "generator": {"kind": "int_range", "min": 1, "max": 9},
             "null_ratio": {"numerator": 0, "denominator": 3}},
            {"name": "always", "type": "integer",
             "generator": {"kind": "int_range", "min": 1, "max": 9},
             "null_ratio": {"numerator": 4, "denominator": 4}}]))
        factory = RowFactory(recipe, SEED)
        for i in range(300):
            row = factory.row("t", i)
            self.assertIsNotNone(row["never"])
            self.assertIsNone(row["always"])

    def test_null_ratio_partial(self):
        recipe = compile_recipe(one_table(row_count=400, columns=[
            {"name": "v", "type": "integer",
             "generator": {"kind": "int_range", "min": 1, "max": 9},
             "null_ratio": {"numerator": 1, "denominator": 4}}]))
        factory = RowFactory(recipe, SEED)
        nulls = sum(factory.row("t", i)["v"] is None for i in range(400))
        self.assertGreater(nulls, 0)
        self.assertLess(nulls, 400)

    def test_null_ratio_validation(self):
        for ratio in ({"numerator": 1, "denominator": 0},
                      {"numerator": 3, "denominator": 2},
                      {"numerator": -1, "denominator": 2}):
            with self.assertRaises(RecipeError):
                compile_recipe(one_table(columns=[
                    {"name": "v", "type": "integer",
                     "generator": {"kind": "null"}, "null_ratio": ratio}]))

    def test_identical_payload_distinct_keys(self):
        recipe = compile_recipe(one_table(row_count=4, columns=[
            {"name": "fixed_int", "type": "integer",
             "generator": {"kind": "int_range", "min": 8, "max": 8}},
            {"name": "fixed_text", "type": "text",
             "generator": {"kind": "choice", "values": ["only"]}}]))
        factory = RowFactory(recipe, SEED)
        rows = [factory.row("t", i) for i in range(4)]
        payloads = [tuple(sorted((k, v) for k, v in r.items() if k != "id"))
                    for r in rows]
        self.assertEqual(payloads, [payloads[0]] * 4)
        self.assertEqual([r["id"] for r in rows], [0, 1, 2, 3])


class ReferenceTests(unittest.TestCase):

    def test_round_robin(self):
        recipe = compile_recipe(handover_recipe())
        factory = RowFactory(recipe, SEED)
        for i in range(6):
            row = factory.row("handover_order", i)
            self.assertEqual(row["customer_id"], 1000 + (i % 4) * 10)
        for i in range(9):
            row = factory.row("box", i)
            self.assertEqual(row["order_id"], 5000 + (i % 6) * 1)

    def test_hash_reference_deterministic_and_independent(self):
        doc = one_table(row_count=50, columns=[
            {"name": "first", "type": "integer",
             "reference": {"table": "t2", "select": "hash",
                           "nullable": False}},
            {"name": "second", "type": "integer",
             "reference": {"table": "t2", "select": "hash",
                           "nullable": False}}])
        doc["tables"].append({"name": "t2", "row_count": 7,
                              "primary_key": {"column": "id", "base": 100,
                                              "stride": 5},
                              "columns": []})
        recipe = compile_recipe(doc)
        factory = RowFactory(recipe, SEED)
        first, second = [], []
        for i in range(50):
            row = factory.row("t", i)
            first.append(row["first"])
            second.append(row["second"])
            self.assertIn((row["first"] - 100) // 5, range(7))
            self.assertEqual(row["first"] % 5, 100 % 5)
        # Two reference columns to the same table are not tied together.
        self.assertNotEqual(first, second)
        # Deterministic across factories.
        again = RowFactory(recipe, SEED)
        self.assertEqual(first, [again.row("t", i)["first"]
                                 for i in range(50)])

    def test_reference_null_ratio(self):
        doc = one_table(row_count=200, columns=[
            {"name": "r", "type": "integer",
             "reference": {"table": "t2", "select": "hash",
                           "nullable": True,
                           "null_ratio": {"numerator": 1,
                                          "denominator": 4}}}])
        doc["tables"].append({"name": "t2", "row_count": 3,
                              "primary_key": {"column": "id", "base": 0,
                                              "stride": 1},
                              "columns": []})
        factory = RowFactory(compile_recipe(doc), SEED)
        values = [factory.row("t", i)["r"] for i in range(200)]
        nulls = values.count(None)
        self.assertGreater(nulls, 0)
        self.assertLess(nulls, 200)
        self.assertTrue(all(v in (None, 0, 1, 2) for v in values))

    def test_empty_target(self):
        base = {"name": "empty", "row_count": 0,
                "primary_key": {"column": "id", "base": 0, "stride": 1},
                "columns": []}
        # Nullable reference to an empty table yields NULL, no modulo zero.
        doc = one_table(row_count=5, columns=[
            {"name": "r", "type": "integer",
             "reference": {"table": "empty", "select": "round_robin",
                           "nullable": True}}])
        doc["tables"].append(base)
        factory = RowFactory(compile_recipe(doc), SEED)
        self.assertEqual([factory.row("t", i)["r"] for i in range(5)],
                         [None] * 5)
        # Required reference to an empty table is rejected up front.
        doc2 = one_table(row_count=5, columns=[
            {"name": "r", "type": "integer",
             "reference": {"table": "empty", "select": "hash",
                           "nullable": False}}])
        doc2["tables"].append(base)
        with self.assertRaises(RecipeError) as ctx:
            compile_recipe(doc2)
        self.assertIn("empty", str(ctx.exception))

    def test_reference_rejects_other_generator(self):
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "r", "type": "integer",
                 "reference": {"table": "t", "select": "hash",
                               "nullable": False},
                 "generator": {"kind": "null"}}]))
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "r", "type": "integer",
                 "reference": {"table": "t", "select": "hash",
                               "nullable": False},
                 "null_ratio": {"numerator": 1, "denominator": 2}}]))

    def test_unknown_reference_target(self):
        with self.assertRaises(RecipeError):
            compile_recipe(one_table(columns=[
                {"name": "r", "type": "integer",
                 "reference": {"table": "ghost", "select": "hash",
                               "nullable": False}}]))

    def test_self_reference(self):
        recipe = compile_recipe(one_table(row_count=6, columns=[
            {"name": "self_rr", "type": "integer",
             "reference": {"table": "t", "select": "round_robin",
                           "nullable": False}},
            {"name": "self_hash", "type": "integer",
             "reference": {"table": "t", "select": "hash",
                           "nullable": False}}]))
        factory = RowFactory(recipe, SEED)
        for i in range(6):
            row = factory.row("t", i)
            self.assertEqual(row["self_rr"], i)
            self.assertIn(row["self_hash"], range(6))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "self.sqlite")
            generate_database(recipe, SEED, out)
            conn = sqlite3.connect(out)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                conn.close()

    def test_foreign_key_cycle_is_legal(self):
        doc = {"tables": [
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
        recipe = compile_recipe(doc)
        factory = RowFactory(recipe, SEED)
        self.assertEqual(factory.row("A", 4)["b_id"], 100 + 4 % 3)
        self.assertEqual(factory.row("B", 2)["a_id"], 2 % 5)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "cycle.sqlite")
            generate_database(recipe, SEED, out)
            conn = sqlite3.connect(out)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                fk_a = conn.execute(
                    "PRAGMA foreign_key_list(A)").fetchall()
                fk_b = conn.execute(
                    "PRAGMA foreign_key_list(B)").fetchall()
                self.assertEqual(len(fk_a), 1)
                self.assertEqual(len(fk_b), 1)
            finally:
                conn.close()


class DerivedColumnTests(unittest.TestCase):

    def test_two_hop_derivation(self):
        recipe = compile_recipe(handover_recipe())
        factory = RowFactory(recipe, SEED)
        for i in range(12):
            row = factory.row("detail", i)
            parent_row = (row["box_id"] - 7000) // 2
            order_row = parent_row % 6
            customer_row = order_row % 4
            expected_tier = factory.row("customer", customer_row)["tier"]
            self.assertEqual(row["box_tier"], expected_tier)
            self.assertEqual(row["amount"], row["qty"] * row["price"])

    def test_null_propagation_and_coalesce(self):
        doc = {"tables": [
            {"name": "p", "row_count": 2,
             "primary_key": {"column": "id", "base": 0, "stride": 1},
             "columns": [
                 {"name": "maybe", "type": "integer",
                  "generator": {"kind": "null"}}]},
            {"name": "c", "row_count": 4,
             "primary_key": {"column": "id", "base": 0, "stride": 1},
             "columns": [
                 {"name": "p_id", "type": "integer",
                  "reference": {"table": "p", "select": "round_robin",
                                "nullable": True,
                                "null_ratio": {"numerator": 1,
                                               "denominator": 1}}},
                 {"name": "via_ref", "type": "integer",
                  "expression": {"op": "ref", "column": "p_id",
                                 "target_column": "maybe"}},
                 {"name": "added", "type": "integer",
                  "expression": {"op": "add",
                                 "args": [{"op": "ref", "column": "p_id",
                                           "target_column": "maybe"},
                                          {"op": "lit", "value": 1}]}},
                 {"name": "glued", "type": "text",
                  "expression": {"op": "concat",
                                 "args": [{"op": "lit", "value": "x"},
                                          {"op": "ref", "column": "p_id",
                                           "target_column": "maybe"}]}},
                 {"name": "rescued", "type": "integer",
                  "expression": {"op": "coalesce",
                                 "args": [{"op": "ref", "column": "p_id",
                                           "target_column": "maybe"},
                                          {"op": "lit", "value": 77}]}}]}]}
        # concat over an integer operand must fail statically, so adjust:
        doc["tables"][1]["columns"][3]["expression"] = {
            "op": "concat",
            "args": [{"op": "lit", "value": "x"},
                     {"op": "coalesce",
                      "args": [{"op": "lit", "value": None},
                               {"op": "lit", "value": "y"}]}]}
        factory = RowFactory(compile_recipe(doc), SEED)
        for i in range(4):
            row = factory.row("c", i)
            self.assertIsNone(row["p_id"])       # ratio 1/1 -> always NULL
            self.assertIsNone(row["via_ref"])    # NULL reference reads NULL
            self.assertIsNone(row["added"])      # add with NULL -> NULL
            self.assertEqual(row["glued"], "xy")
            self.assertEqual(row["rescued"], 77)

    def test_static_type_errors(self):
        def with_expr(expr, col_type="integer"):
            return one_table(columns=[
                {"name": "v", "type": col_type, "expression": expr}])

        with self.assertRaises(RecipeError):
            compile_recipe(with_expr(
                {"op": "add", "args": [{"op": "lit", "value": "txt"},
                                       {"op": "lit", "value": 1}]}))
        with self.assertRaises(RecipeError):
            compile_recipe(with_expr(
                {"op": "concat", "args": [{"op": "lit", "value": 1},
                                          {"op": "lit", "value": "a"}]},
                col_type="text"))
        with self.assertRaises(RecipeError):
            compile_recipe(with_expr(
                {"op": "col", "name": "ghost"}))
        with self.assertRaises(RecipeError):
            compile_recipe(with_expr(
                {"op": "coalesce",
                 "args": [{"op": "lit", "value": 1},
                          {"op": "lit", "value": "a"}]}))
        # expression type must match the declared column type
        with self.assertRaises(RecipeError):
            compile_recipe(with_expr(
                {"op": "lit", "value": "text"}, col_type="integer"))
        # literal outside the signed 64-bit range
        with self.assertRaises(RecipeError):
            compile_recipe(with_expr({"op": "lit", "value": 2 ** 63}))
        # boolean literal is not an integer literal
        with self.assertRaises(RecipeError):
            compile_recipe(with_expr({"op": "lit", "value": True}))
        # unknown op
        with self.assertRaises(RecipeError):
            compile_recipe(with_expr({"op": "substr", "args": []}))

    def test_ref_expression_validation(self):
        # ref through a non-reference column
        doc = one_table(columns=[
            {"name": "plain", "type": "integer",
             "generator": {"kind": "null"}},
            {"name": "v", "type": "integer",
             "expression": {"op": "ref", "column": "plain",
                            "target_column": "id"}}])
        with self.assertRaises(RecipeError):
            compile_recipe(doc)
        # ref to an unknown target column
        doc2 = {"tables": [
            {"name": "p", "row_count": 1,
             "primary_key": {"column": "id", "base": 0, "stride": 1},
             "columns": []},
            {"name": "c", "row_count": 1,
             "primary_key": {"column": "id", "base": 0, "stride": 1},
             "columns": [
                 {"name": "p_id", "type": "integer",
                  "reference": {"table": "p", "select": "round_robin",
                                "nullable": False}},
                 {"name": "v", "type": "integer",
                  "expression": {"op": "ref", "column": "p_id",
                                 "target_column": "ghost"}}]}]}
        with self.assertRaises(RecipeError):
            compile_recipe(doc2)

    def test_overflow_is_located(self):
        doc = one_table(columns=[
            {"name": "big", "type": "integer",
             "generator": {"kind": "int_range",
                           "min": 9223372036854775800,
                           "max": 9223372036854775807}},
            {"name": "boom", "type": "integer",
             "expression": {"op": "add",
                            "args": [{"op": "col", "name": "big"},
                                     {"op": "col", "name": "big"}]}}])
        factory = RowFactory(compile_recipe(doc), SEED)
        with self.assertRaises(GenerationError) as ctx:
            factory.row("t", 0)
        message = str(ctx.exception)
        self.assertIn("overflow", message)
        self.assertEqual(ctx.exception.table, "t")
        self.assertEqual(ctx.exception.row, 0)
        self.assertIn("t[0].boom", ctx.exception.path)

    def test_mul_overflow(self):
        doc = one_table(columns=[
            {"name": "boom", "type": "integer",
             "expression": {"op": "mul",
                            "args": [{"op": "lit",
                                      "value": 9223372036854775807},
                                     {"op": "lit", "value": 2}]}}])
        factory = RowFactory(compile_recipe(doc), SEED)
        with self.assertRaises(GenerationError):
            factory.row("t", 0)

    def test_derived_cycle_rejected(self):
        # A.x reads B.y, B.y reads A.x -- invalid even through coalesce.
        doc = {"tables": [
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
        with self.assertRaises(RecipeError) as ctx:
            compile_recipe(doc)
        message = str(ctx.exception)
        self.assertIn("cycle", message)
        self.assertIn("A.x", message)
        self.assertIn("B.y", message)

    def test_derived_self_cycle_rejected(self):
        doc = one_table(columns=[
            {"name": "parent", "type": "integer",
             "reference": {"table": "t", "select": "round_robin",
                           "nullable": True}},
            {"name": "v", "type": "integer",
             "expression": {"op": "coalesce",
                            "args": [{"op": "ref", "column": "parent",
                                      "target_column": "v"},
                                     {"op": "lit", "value": 0}]}}])
        with self.assertRaises(RecipeError) as ctx:
            compile_recipe(doc)
        self.assertIn("t.v", str(ctx.exception))

    def test_local_column_cycle_rejected(self):
        doc = one_table(columns=[
            {"name": "a", "type": "integer",
             "expression": {"op": "col", "name": "b"}},
            {"name": "b", "type": "integer",
             "expression": {"op": "col", "name": "a"}}])
        with self.assertRaises(RecipeError):
            compile_recipe(doc)

    def test_legal_dag_across_cycle_free_reads(self):
        # A and B reference each other, but only A has a derived column
        # reading B's plain column: no value cycle.
        doc = {"tables": [
            {"name": "A", "row_count": 4,
             "primary_key": {"column": "a_id", "base": 0, "stride": 1},
             "columns": [
                 {"name": "b_id", "type": "integer",
                  "reference": {"table": "B", "select": "round_robin",
                                "nullable": False}},
                 {"name": "b_val", "type": "integer",
                  "expression": {"op": "ref", "column": "b_id",
                                 "target_column": "v"}}]},
            {"name": "B", "row_count": 2,
             "primary_key": {"column": "b_id", "base": 10, "stride": 1},
             "columns": [
                 {"name": "a_id", "type": "integer",
                  "reference": {"table": "A", "select": "round_robin",
                                "nullable": False}},
                 {"name": "v", "type": "integer",
                  "generator": {"kind": "int_range", "min": 1, "max": 1}}]}]}
        factory = RowFactory(compile_recipe(doc), SEED)
        self.assertEqual(factory.row("A", 3)["b_val"], 1)

    def test_derived_text_byte_cap(self):
        doc = one_table(columns=[
            {"name": "v", "type": "text",
             "expression": {"op": "concat",
                            "args": [{"op": "lit", "value": "一二三四"},
                                     {"op": "lit", "value": "五六"}]},
             "max_utf8_bytes": 8}])
        factory = RowFactory(compile_recipe(doc), SEED)
        with self.assertRaises(GenerationError) as ctx:
            factory.row("t", 0)
        self.assertIn("UTF-8", str(ctx.exception))


class DeterminismTests(unittest.TestCase):

    def test_seed_changes_only_random_values(self):
        recipe = compile_recipe(handover_recipe())
        f1, f2 = RowFactory(recipe, 1), RowFactory(recipe, 2)
        random_seen_difference = False
        for table in recipe.tables:
            for i in range(table.row_count):
                r1 = f1.row(table.name, i)
                r2 = f2.row(table.name, i)
                # Primary keys and round-robin references never move.
                self.assertEqual(r1[table.pk_column], r2[table.pk_column])
                for col in table.columns:
                    if col.reference is not None and \
                            col.reference.select == "round_robin":
                        self.assertEqual(r1[col.name], r2[col.name])
                    elif col.name in ("amount",):
                        # derived from random inputs: may differ, never NaN
                        if r1[col.name] != r2[col.name]:
                            random_seen_difference = True
                    else:
                        if r1[col.name] != r2[col.name]:
                            random_seen_difference = True
        self.assertTrue(random_seen_difference)

    def test_seed_pins_exact_rows(self):
        # The same seed reproduces identical rows in a fresh process view.
        recipe = compile_recipe(handover_recipe())
        rows_a = [RowFactory(recipe, 7).row("detail", i) for i in range(12)]
        rows_b = [RowFactory(recipe, 7).row("detail", i) for i in range(12)]
        self.assertEqual(rows_a, rows_b)

    def test_declaration_order_does_not_matter(self):
        doc = handover_recipe()
        shuffled = {"tables": list(reversed(doc["tables"]))}
        for table in shuffled["tables"]:
            table = table  # noqa: B018 -- clarity
        shuffled = json.loads(json.dumps(shuffled))
        for table in shuffled["tables"]:
            table["columns"] = list(reversed(table["columns"]))
        r1 = compile_recipe(doc)
        r2 = compile_recipe(shuffled)
        f1, f2 = RowFactory(r1, SEED), RowFactory(r2, SEED)
        for table in r1.tables:
            for i in range(table.row_count):
                self.assertEqual(f1.row(table.name, i),
                                 f2.row(table.name, i))

    def test_segmented_and_interleaved_generation(self):
        recipe = compile_recipe(handover_recipe())
        one_shot = [row for _, row in RowFactory(recipe, SEED)
                    .rows("detail", 0, 12)]
        # Reverse-order adjacent segments, interleaved with other tables.
        factory = RowFactory(recipe, SEED)
        parts = []
        list(factory.rows("box", 0, 9))          # interleave other table
        parts.append([row for _, row in factory.rows("detail", 8, 12)])
        list(factory.rows("customer", 0, 4))     # more interleaving
        parts.append([row for _, row in factory.rows("detail", 3, 8)])
        parts.append([row for _, row in factory.rows("detail", 0, 3)])
        stitched = parts[2] + parts[1] + parts[0]
        self.assertEqual(one_shot, stitched)
        # A fresh factory with a tiny cache agrees as well.
        tiny = RowFactory(recipe, SEED, cache_rows=2)
        self.assertEqual(one_shot,
                         [row for _, row in tiny.rows("detail", 0, 12)])

    def test_no_shared_random_state_between_calls(self):
        recipe = compile_recipe(handover_recipe())
        factory = RowFactory(recipe, SEED)
        before = factory.row("detail", 5)
        for i in range(12):
            factory.row("detail", i)
            factory.row("box", i % 9)
        self.assertEqual(factory.row("detail", 5), before)

    def test_big_addresses_stay_integer(self):
        doc = {"tables": [
            {"name": "huge", "row_count": 2 ** 40,
             "primary_key": {"column": "id", "base": 2 ** 40,
                             "stride": 3},
             "columns": [
                 {"name": "v", "type": "integer",
                  "generator": {"kind": "int_range", "min": 0, "max": 9}}]}]}
        factory = RowFactory(compile_recipe(doc), SEED)
        row = factory.row("huge", 2 ** 40 - 1)
        self.assertIsInstance(row["id"], int)
        self.assertEqual(row["id"], 2 ** 40 + (2 ** 40 - 1) * 3)
        self.assertIsInstance(row["v"], int)

    def test_pythonhashseed_invariance(self):
        env = dict(os.environ)
        outputs = []
        for hashseed in ("0", "1", "42"):
            env["PYTHONHASHSEED"] = hashseed
            proc = subprocess.run(
                [sys.executable, "-I", MAIN, "gen-sample",
                 "--recipe", EXAMPLE_RECIPE, "--seed", "42",
                 "--table", "detail", "--rows", "0:12"],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(proc.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])


class DatabaseTests(unittest.TestCase):

    def test_materialise_and_verify(self):
        recipe = compile_recipe(handover_recipe())
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "gen.sqlite")
            result = generate_database(recipe, SEED, out)
            self.assertEqual(result.table_rows,
                             {"customer": 4, "handover_order": 6,
                              "box": 9, "detail": 12})
            conn = sqlite3.connect(out)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                # Real foreign keys, not plain unconstrained numbers.
                fk_tables = {row[2] for row in conn.execute(
                    "PRAGMA foreign_key_list(detail)")}
                self.assertEqual(fk_tables, {"box"})
                # Derived values match the generator.
                factory = RowFactory(recipe, SEED)
                for i in range(12):
                    got = conn.execute(
                        "SELECT amount, box_tier FROM detail "
                        "WHERE detail_id = ?", (90000 + i,)).fetchone()
                    want = factory.row("detail", i)
                    self.assertEqual(got,
                                     (want["amount"], want["box_tier"]))
            finally:
                conn.close()

    def test_output_must_not_exist(self):
        recipe = compile_recipe(one_table())
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "gen.sqlite")
            generate_database(recipe, SEED, out)
            with self.assertRaises(FileExistsError):
                generate_database(recipe, SEED, out)

    def test_failure_leaves_no_database(self):
        doc = one_table(row_count=10, columns=[
            {"name": "big", "type": "integer",
             "generator": {"kind": "int_range",
                           "min": 9223372036854775800,
                           "max": 9223372036854775807}},
            {"name": "boom", "type": "integer",
             "expression": {"op": "add",
                            "args": [{"op": "col", "name": "big"},
                                     {"op": "col", "name": "big"}]}}])
        recipe = compile_recipe(doc)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "gen.sqlite")
            with self.assertRaises(GenerationError):
                generate_database(recipe, SEED, out)
            self.assertFalse(os.path.exists(out))
            self.assertEqual(os.listdir(tmp), [])

    def test_generated_db_serves_assembly(self):
        from archindex import assemble_nested
        recipe = compile_recipe(handover_recipe())
        plan = {"nodes": [
            {"name": "order", "table": "handover_order",
             "identity": ["order_id"],
             "scalars": [{"column": "fee", "alias": "fee"}],
             "order_by": ["order_id"]},
            {"name": "box", "table": "box", "identity": ["box_id"],
             "parent": "order", "array": "boxes",
             "foreign_key": ["order_id"],
             "scalars": [{"column": "box_id", "alias": "boxId"}],
             "order_by": ["box_id"]}]}
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "gen.sqlite")
            out = os.path.join(tmp, "out.jsonl")
            generate_database(recipe, SEED, db)
            result = assemble_nested(plan, db, out)
            self.assertEqual(result.roots, 6)
            with open(out, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 6)
            self.assertEqual(sum(len(o["boxes"]) for o in lines), 9)


class CliTests(unittest.TestCase):

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, "-I", MAIN, *args],
            capture_output=True, text=True, env=env)

    def test_gen_sample_single_row(self):
        proc = self.run_cli("gen-sample", "--recipe", EXAMPLE_RECIPE,
                            "--seed", "42", "--table", "customer",
                            "--row", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["rows"][0]["row"], 2)
        self.assertEqual(payload["rows"][0]["values"]["customer_id"], 1020)

    def test_gen_sample_range_and_out_of_range(self):
        proc = self.run_cli("gen-sample", "--recipe", EXAMPLE_RECIPE,
                            "--seed", "0x2a", "--table", "box",
                            "--rows", "1:3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual([r["row"] for r in payload["rows"]], [1, 2])
        proc = self.run_cli("gen-sample", "--recipe", EXAMPLE_RECIPE,
                            "--seed", "42", "--table", "box",
                            "--rows", "8:10")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error", proc.stderr)

    def test_gen_db_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "cli.sqlite")
            proc = self.run_cli("gen-db", "--recipe", EXAMPLE_RECIPE,
                                "--seed", "42", "--output", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["table_rows"]["detail"], 12)
            proc = self.run_cli("gen-db", "--recipe", EXAMPLE_RECIPE,
                                "--seed", "42", "--output", out)
            self.assertEqual(proc.returncode, 2)

    def test_bad_seed_rejected(self):
        proc = self.run_cli("gen-sample", "--recipe", EXAMPLE_RECIPE,
                            "--seed", "not-a-number", "--table", "box",
                            "--row", "0")
        self.assertEqual(proc.returncode, 2)


class IndependentFormulaTests(unittest.TestCase):
    """Cross-check the generator against an independent implementation of
    the documented digest mapping (written here from the spec, not reused
    from archindex.relgen)."""

    @staticmethod
    def digest(seed: int, table: str, column: str, row: int,
               purpose: str) -> bytes:
        h = hashlib.sha256()
        h.update(b"archindex-relgen\x00v1\x00")
        h.update(seed.to_bytes(8, "big"))
        for part in (table.encode("utf-8"), column.encode("utf-8")):
            h.update(len(part).to_bytes(4, "big"))
            h.update(part)
        h.update(row.to_bytes(8, "big"))
        purpose_b = purpose.encode("ascii")
        h.update(len(purpose_b).to_bytes(4, "big"))
        h.update(purpose_b)
        return h.digest()

    def u64(self, seed, table, column, row, purpose):
        return int.from_bytes(
            self.digest(seed, table, column, row, purpose)[:8], "big")

    def letters(self, seed, table, column, row, count):
        base = self.digest(seed, table, column, row, "value")
        out = []
        block_no, block, pos = 0, b"", 0
        while len(out) < count:
            if pos >= len(block):
                block = hashlib.sha256(
                    base + block_no.to_bytes(4, "big")).digest()
                block_no += 1
                pos = 0
            out.append(chr(ord("a") + block[pos] % 26))
            pos += 1
        return "".join(out)

    def test_documented_mapping(self):
        doc = one_table(row_count=64, columns=[
            {"name": "i", "type": "integer",
             "generator": {"kind": "int_range", "min": -10, "max": 10}},
            {"name": "c", "type": "text",
             "generator": {"kind": "choice", "values": ["p", "q", "r"]}},
            {"name": "s", "type": "text",
             "generator": {"kind": "text", "prefix": "预", "length": 6}},
            {"name": "n", "type": "integer",
             "generator": {"kind": "int_range", "min": 0, "max": 100},
             "null_ratio": {"numerator": 3, "denominator": 10}}])
        factory = RowFactory(compile_recipe(doc), SEED)
        for row_no in range(64):
            row = factory.row("t", row_no)
            want_i = -10 + self.u64(SEED, "t", "i", row_no, "value") % 21
            self.assertEqual(row["i"], want_i)
            want_c = "pqr"[self.u64(SEED, "t", "c", row_no, "value") % 3]
            self.assertEqual(row["c"], want_c)
            want_s = "预" + self.letters(SEED, "t", "s", row_no, 5)
            self.assertEqual(row["s"], want_s)
            is_null = self.u64(SEED, "t", "n", row_no, "null") % 10 < 3
            if is_null:
                self.assertIsNone(row["n"])
            else:
                want_n = self.u64(SEED, "t", "n", row_no, "value") % 101
                self.assertEqual(row["n"], want_n)

    def test_hash_reference_formula(self):
        doc = {"tables": [
            {"name": "p", "row_count": 5,
             "primary_key": {"column": "id", "base": 40, "stride": 4},
             "columns": []},
            {"name": "c", "row_count": 32,
             "primary_key": {"column": "id", "base": 0, "stride": 1},
             "columns": [
                 {"name": "p_id", "type": "integer",
                  "reference": {"table": "p", "select": "hash",
                                "nullable": False}}]}]}
        factory = RowFactory(compile_recipe(doc), SEED)
        for row_no in range(32):
            parent_row = self.u64(SEED, "c", "p_id", row_no, "ref") % 5
            self.assertEqual(factory.row("c", row_no)["p_id"],
                             40 + parent_row * 4)


if __name__ == "__main__":
    unittest.main()
