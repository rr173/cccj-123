"""Address-driven relational test-data generator.

A *recipe* (a plain JSON document, never SQL or Python) plus a 64-bit
*seed* fully describes a set of related virtual tables.  Any row of any
table can then be produced directly from its address -- the table name and
a zero-based row number -- without reading a business database, without
materialising the whole data set and without depending on which addresses
were generated before.  The same recipe can also be materialised into a
real SQLite database (primary keys and deferrable foreign keys included)
for exercising the existing ingest/assemble commands.

Row addressing
--------------
Each table declares ``name``, ``row_count`` (``N``, zero allowed), an
integer ``primary_key`` column with ``base`` and ``stride``.  Row numbers
are zero-based and the primary key of row ``i`` is ``base + i * stride``;
``stride`` must be positive and every key must fit the signed 64-bit
range.  Primary keys never depend on the seed, and neither table order
nor column order inside the recipe influences any generated value.

Random mapping rules (stable across processes)
----------------------------------------------
Every random decision is a pure function of
``(seed, table name, column name, row number, purpose)``::

    digest = SHA-256( b"archindex-relgen\\x00v1\\x00"
                      || u64be(seed)
                      || u32be(len(table_utf8))  || table_utf8
                      || u32be(len(column_utf8)) || column_utf8
                      || u64be(row_number)
                      || u32be(len(purpose_ascii)) || purpose_ascii )

``u64 = int.from_bytes(digest[:8], "big")`` then maps to values:

* ``int_range [lo, hi]``  -> ``lo + u64 % (hi - lo + 1)``      (purpose ``value``)
* ``choice`` of ``k``      -> ``values[u64 % k]``              (purpose ``value``)
* null ratio ``num/den``   -> NULL iff ``u64 % den < num``     (purpose ``null``
  for value/derived columns, ``refnull`` for nullable references;
  ``num == 0`` never yields NULL, ``num == den`` always does)
* ``hash`` reference       -> parent row ``u64 % target_rows`` (purpose ``ref``)
* ``text`` suffix of ``m`` lowercase ASCII letters: block ``j`` of a byte
  stream is ``SHA-256(digest || u32be(j))``; bytes are consumed in order
  and each byte ``b`` yields letter ``chr(ord("a") + b % 26)``.
  (purpose ``value``)

A ``round_robin`` reference uses no digest at all: the parent row number
is ``row_number % target_rows``.  Parent row numbers are turned into keys
with the target table's ``base``/``stride``.  The mapping is deliberately
simple modulo arithmetic -- deterministic and documented, not
cryptographically uniform.  Nothing ever uses Python's ``hash()``, a
shared random cursor, call order or the process id, so results are
identical under any ``PYTHONHASHSEED`` and any access pattern.

Derived columns
---------------
A column may instead carry an explicit JSON expression tree with node
types ``lit`` / ``col`` / ``ref`` / ``add`` / ``mul`` / ``concat`` /
``coalesce`` (see :func:`compile_recipe`).  ``ref`` follows one declared
reference column of the current row and reads one column of the target
row; a NULL reference reads as NULL.  ``add``/``mul``/``concat`` yield
NULL when any operand is NULL; ``coalesce`` yields the first non-NULL
operand.  Integer results must stay inside the signed 64-bit range --
overflow, type mismatches, unknown names and illegal operations are
reported with the table, row number, column and dependency path; nothing
is silently converted to float.

Foreign-key references may form cycles (row identity needs no data), but
the *value* dependency graph of derived columns -- with cross-table reads
expanded to the target ``(table, column)`` -- must be acyclic.  Cycles are
reported with their full path when the recipe is compiled, before any row
is generated, even if a ``coalesce`` might have short-circuited them at
run time.

Resource profile
----------------
Row generation is per-address with a bounded LRU row cache
(``cache_rows`` entries); reading one child row only ever computes the
parent rows it actually depends on, so sampling the tail of a
billion-row virtual parent touches a handful of rows.  Large row numbers
stay Python ``int``\\ s end to end and are never routed through floats.
``generate_database`` streams rows in ``batch_size`` batches into SQLite
with a bounded page cache (``cache_size_kb``); no whole table is ever
held in Python memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

from .errors import GenerationError, RecipeError

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
SEED_MAX = 2 ** 64 - 1
UINT64_MOD = 2 ** 64

#: Domain separator prepended to every digest input.
_DOMAIN = b"archindex-relgen\x00v1\x00"

#: Digest purpose tags.  The null decision and the value selection of one
#: column deliberately use different tags so their draws are independent.
_PURPOSE_VALUE = "value"
_PURPOSE_NULL = "null"
_PURPOSE_REF = "ref"
_PURPOSE_REFNULL = "refnull"

_LETTERS = "abcdefghijklmnopqrstuvwxyz"

DEFAULT_CACHE_ROWS = 4096
DEFAULT_CACHE_SIZE_KB = 8192
DEFAULT_BATCH_SIZE = 1000
DEFAULT_MAX_UTF8_BYTES = 1024

_SELECT_ROUND_ROBIN = "round_robin"
_SELECT_HASH = "hash"

_OP_LIT = "lit"
_OP_COL = "col"
_OP_REF = "ref"
_OP_ADD = "add"
_OP_MUL = "mul"
_OP_CONCAT = "concat"
_OP_COALESCE = "coalesce"
_FOLD_OPS = (_OP_ADD, _OP_MUL, _OP_CONCAT)

_TYPE_INT = "integer"
_TYPE_TEXT = "text"
_TYPE_NULL = "null"  # static type of a NULL literal only


# --------------------------------------------------------------------------
# Digest primitives (documented in the module docstring)
# --------------------------------------------------------------------------

def _u32be(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _u64be(value: int) -> bytes:
    return value.to_bytes(8, "big")


def _digest(seed: int, table: str, column: str, row_number: int,
            purpose: str) -> bytes:
    table_b = table.encode("utf-8")
    column_b = column.encode("utf-8")
    purpose_b = purpose.encode("ascii")
    h = hashlib.sha256()
    h.update(_DOMAIN)
    h.update(_u64be(seed))
    h.update(_u32be(len(table_b)))
    h.update(table_b)
    h.update(_u32be(len(column_b)))
    h.update(column_b)
    h.update(_u64be(row_number))
    h.update(_u32be(len(purpose_b)))
    h.update(purpose_b)
    return h.digest()


def _draw_u64(seed: int, table: str, column: str, row_number: int,
              purpose: str) -> int:
    return int.from_bytes(
        _digest(seed, table, column, row_number, purpose)[:8], "big")


def _draw_letters(seed: int, table: str, column: str, row_number: int,
                  count: int) -> str:
    base = _digest(seed, table, column, row_number, _PURPOSE_VALUE)
    out: list[str] = []
    block_no = 0
    block = b""
    pos = 0
    while len(out) < count:
        if pos >= len(block):
            block = hashlib.sha256(base + _u32be(block_no)).digest()
            block_no += 1
            pos = 0
        out.append(_LETTERS[block[pos] % 26])
        pos += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Recipe model
# --------------------------------------------------------------------------

class IntRangeGen:
    """Closed-interval integer generator ``[lo, hi]``."""

    __slots__ = ("lo", "hi")

    def __init__(self, lo: int, hi: int):
        self.lo = lo
        self.hi = hi


class ChoiceGen:
    """Pick one of ``values`` (all ``int`` or all ``str``, non-empty)."""

    __slots__ = ("values",)

    def __init__(self, values: tuple):
        self.values = values


class TextGen:
    """Fixed prefix plus a pseudo-random lowercase ASCII suffix.

    ``length`` counts Unicode code points of the whole value (prefix
    included); the column's ``max_utf8_bytes`` caps the UTF-8 length.
    """

    __slots__ = ("prefix", "length")

    def __init__(self, prefix: str, length: int):
        self.prefix = prefix
        self.length = length


class NullGen:
    """Constant NULL generator."""

    __slots__ = ()


class RefSpec:
    """A reference to another table's primary key."""

    __slots__ = ("target", "select", "nullable", "null_num", "null_den")

    def __init__(self, target: str, select: str, nullable: bool,
                 null_num: int, null_den: int):
        self.target = target
        self.select = select
        self.nullable = nullable
        self.null_num = null_num
        self.null_den = null_den


# -- expression tree nodes --------------------------------------------------

class Lit:
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value


class ColRef:
    """Read a column of the current row."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class RefRead:
    """Follow reference ``column`` of the current row, read target column."""

    __slots__ = ("column", "target_column")

    def __init__(self, column: str, target_column: str):
        self.column = column
        self.target_column = target_column


class Fold:
    """Left fold of ``add`` / ``mul`` / ``concat`` over >= 2 args."""

    __slots__ = ("op", "args")

    def __init__(self, op: str, args: tuple):
        self.op = op
        self.args = args


class Coalesce:
    __slots__ = ("args",)

    def __init__(self, args: tuple):
        self.args = args


class Column:
    __slots__ = ("name", "type", "generator", "reference", "expression",
                 "null_num", "null_den", "max_utf8_bytes")

    def __init__(self, name: str, type_: str, *, generator=None,
                 reference: RefSpec | None = None, expression=None,
                 null_num: int = 0, null_den: int = 1,
                 max_utf8_bytes: int = DEFAULT_MAX_UTF8_BYTES):
        self.name = name
        self.type = type_
        self.generator = generator
        self.reference = reference
        self.expression = expression
        self.null_num = null_num
        self.null_den = null_den
        self.max_utf8_bytes = max_utf8_bytes

    @property
    def is_derived(self) -> bool:
        return self.expression is not None


class Table:
    __slots__ = ("name", "row_count", "pk_column", "pk_base", "pk_stride",
                 "columns", "column_map")

    def __init__(self, name: str, row_count: int, pk_column: str,
                 pk_base: int, pk_stride: int, columns: tuple):
        self.name = name
        self.row_count = row_count
        self.pk_column = pk_column
        self.pk_base = pk_base
        self.pk_stride = pk_stride
        self.columns = columns
        self.column_map = {c.name: c for c in columns}

    def key_of(self, row_number: int) -> int:
        return self.pk_base + row_number * self.pk_stride


class Recipe:
    """A compiled, fully validated generation recipe."""

    __slots__ = ("tables", "table_map")

    def __init__(self, tables: tuple):
        self.tables = tables
        self.table_map = {t.name: t for t in tables}

    def table(self, name: str) -> Table:
        try:
            return self.table_map[name]
        except KeyError:
            raise GenerationError(
                f"unknown table {name!r}; recipe defines "
                f"{sorted(self.table_map)!r}") from None


# --------------------------------------------------------------------------
# Recipe parsing / validation
# --------------------------------------------------------------------------

def _err(message: str) -> RecipeError:
    return RecipeError(message)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise _err(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _req_int(value: Any, label: str, lo: int | None = None,
             hi: int | None = None) -> int:
    if not _is_int(value):
        raise _err(f"{label}: expected an integer, got {value!r}")
    if lo is not None and value < lo:
        raise _err(f"{label}: {value} is below the minimum {lo}")
    if hi is not None and value > hi:
        raise _err(f"{label}: {value} exceeds the maximum {hi}")
    return value


def _req_int64(value: Any, label: str) -> int:
    return _req_int(value, label, INT64_MIN, INT64_MAX)


def _req_str(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and value == ""):
        raise _err(f"{label}: expected a non-empty string, got {value!r}")
    return value


def _req_obj(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise _err(f"{label}: expected an object, got {value!r}")
    return value


def _no_extra(obj: dict, allowed: set, label: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise _err(f"{label}: unknown key(s) {extra!r}")


def _parse_null_ratio(value: Any, label: str) -> tuple[int, int]:
    if value is None:
        return 0, 1
    obj = _req_obj(value, label)
    _no_extra(obj, {"numerator", "denominator"}, label)
    num = _req_int(obj.get("numerator"), f"{label}.numerator", 0)
    den = _req_int(obj.get("denominator"), f"{label}.denominator", 1)
    _require(num <= den,
             f"{label}: numerator {num} exceeds denominator {den}")
    return num, den


def _parse_generator(obj: Any, label: str, col_type: str,
                     max_utf8_bytes: int):
    obj = _req_obj(obj, label)
    kind = obj.get("kind")
    if kind == "int_range":
        _no_extra(obj, {"kind", "min", "max"}, label)
        _require(col_type == _TYPE_INT,
                 f"{label}: int_range requires column type \"integer\"")
        lo = _req_int64(obj.get("min"), f"{label}.min")
        hi = _req_int64(obj.get("max"), f"{label}.max")
        _require(lo <= hi, f"{label}: min {lo} is greater than max {hi}")
        return IntRangeGen(lo, hi)
    if kind == "choice":
        _no_extra(obj, {"kind", "values"}, label)
        values = obj.get("values")
        _require(isinstance(values, list) and len(values) > 0,
                 f"{label}.values: expected a non-empty list")
        if all(_is_int(v) for v in values):
            _require(col_type == _TYPE_INT,
                     f"{label}: integer choices require type \"integer\"")
            for i, v in enumerate(values):
                _req_int64(v, f"{label}.values[{i}]")
            return ChoiceGen(tuple(values))
        if all(isinstance(v, str) for v in values):
            _require(col_type == _TYPE_TEXT,
                     f"{label}: text choices require column type \"text\"")
            for i, v in enumerate(values):
                size = len(v.encode("utf-8"))
                _require(size <= max_utf8_bytes,
                         f"{label}.values[{i}]: {size} UTF-8 bytes exceed "
                         f"the column cap {max_utf8_bytes}")
            return ChoiceGen(tuple(values))
        raise _err(f"{label}.values: entries must be all integers or all "
                   "text (booleans and null are not allowed)")
    if kind == "text":
        _no_extra(obj, {"kind", "prefix", "length"}, label)
        _require(col_type == _TYPE_TEXT,
                 f"{label}: text generator requires column type \"text\"")
        prefix = _req_str(obj.get("prefix", ""), f"{label}.prefix",
                          allow_empty=True)
        length = _req_int(obj.get("length"), f"{label}.length", 0)
        _require(length >= len(prefix),
                 f"{label}: length {length} (Unicode code points) is "
                 f"smaller than the prefix {prefix!r} "
                 f"({len(prefix)} code points)")
        suffix = length - len(prefix)
        worst = len(prefix.encode("utf-8")) + suffix  # suffix is ASCII
        _require(worst <= max_utf8_bytes,
                 f"{label}: value needs up to {worst} UTF-8 bytes "
                 f"({len(prefix.encode('utf-8'))} for the prefix plus "
                 f"{suffix} suffix letters) which exceeds the column cap "
                 f"{max_utf8_bytes}")
        return TextGen(prefix, length)
    if kind == "null":
        _no_extra(obj, {"kind"}, label)
        return NullGen()
    raise _err(f"{label}.kind: expected one of \"int_range\", \"choice\", "
               f"\"text\", \"null\"; got {kind!r}")


def _parse_reference(obj: Any, label: str) -> RefSpec:
    obj = _req_obj(obj, label)
    _no_extra(obj, {"table", "select", "nullable", "null_ratio"}, label)
    target = _req_str(obj.get("table"), f"{label}.table")
    select = obj.get("select")
    _require(select in (_SELECT_ROUND_ROBIN, _SELECT_HASH),
             f"{label}.select: expected \"round_robin\" or \"hash\", "
             f"got {select!r}")
    nullable = obj.get("nullable", False)
    _require(isinstance(nullable, bool),
             f"{label}.nullable: expected a boolean, got {nullable!r}")
    null_num, null_den = 0, 1
    if "null_ratio" in obj:
        _require(nullable,
                 f"{label}.null_ratio: only meaningful when "
                 "\"nullable\" is true")
        null_num, null_den = _parse_null_ratio(
            obj["null_ratio"], f"{label}.null_ratio")
    return RefSpec(target, select, nullable, null_num, null_den)


def _parse_expression(node: Any, label: str):
    obj = _req_obj(node, label)
    op = obj.get("op")
    if op == _OP_LIT:
        _no_extra(obj, {"op", "value"}, label)
        value = obj.get("value")
        if value is None or isinstance(value, str):
            return Lit(value)
        if _is_int(value):
            _req_int64(value, f"{label}.value")
            return Lit(value)
        raise _err(f"{label}.value: literals must be integers, text or "
                   f"null; got {value!r}")
    if op == _OP_COL:
        _no_extra(obj, {"op", "name"}, label)
        return ColRef(_req_str(obj.get("name"), f"{label}.name"))
    if op == _OP_REF:
        _no_extra(obj, {"op", "column", "target_column"}, label)
        return RefRead(_req_str(obj.get("column"), f"{label}.column"),
                       _req_str(obj.get("target_column"),
                                f"{label}.target_column"))
    if op in _FOLD_OPS:
        _no_extra(obj, {"op", "args"}, label)
        args = obj.get("args")
        _require(isinstance(args, list) and len(args) >= 2,
                 f"{label}.args: \"{op}\" needs at least two operands")
        return Fold(op, tuple(_parse_expression(a, f"{label}.args[{i}]")
                              for i, a in enumerate(args)))
    if op == _OP_COALESCE:
        _no_extra(obj, {"op", "args"}, label)
        args = obj.get("args")
        _require(isinstance(args, list) and len(args) >= 1,
                 f"{label}.args: \"coalesce\" needs at least one operand")
        return Coalesce(tuple(_parse_expression(a, f"{label}.args[{i}]")
                              for i, a in enumerate(args)))
    raise _err(f"{label}.op: expected one of \"lit\", \"col\", \"ref\", "
               f"\"add\", \"mul\", \"concat\", \"coalesce\"; got {op!r}")


def _parse_column(obj: Any, label: str) -> Column:
    obj = _req_obj(obj, label)
    _no_extra(obj, {"name", "type", "generator", "reference", "expression",
                    "null_ratio", "max_utf8_bytes"}, label)
    name = _req_str(obj.get("name"), f"{label}.name")
    col_type = obj.get("type")
    _require(col_type in (_TYPE_INT, _TYPE_TEXT),
             f"{label}.type: expected \"integer\" or \"text\", "
             f"got {col_type!r}")
    kinds = [k for k in ("generator", "reference", "expression")
             if k in obj]
    _require(len(kinds) == 1,
             f"{label}: exactly one of \"generator\", \"reference\" or "
             f"\"expression\" must be given, found {kinds or 'none'}")
    cap = DEFAULT_MAX_UTF8_BYTES
    if "max_utf8_bytes" in obj:
        cap = _req_int(obj["max_utf8_bytes"], f"{label}.max_utf8_bytes", 1)
        _require(col_type == _TYPE_TEXT,
                 f"{label}.max_utf8_bytes: only meaningful for text "
                 "columns")
    generator = reference = expression = None
    null_num, null_den = 0, 1
    if "generator" in obj:
        generator = _parse_generator(obj["generator"], f"{label}.generator",
                                     col_type, cap)
        null_num, null_den = _parse_null_ratio(
            obj.get("null_ratio"), f"{label}.null_ratio")
    elif "reference" in obj:
        _require(col_type == _TYPE_INT,
                 f"{label}: reference columns must have type \"integer\" "
                 "(primary keys are integers)")
        _require("null_ratio" not in obj,
                 f"{label}.null_ratio: a reference column takes its null "
                 "ratio inside the \"reference\" object; it cannot also "
                 "define another value generator")
        reference = _parse_reference(obj["reference"],
                                     f"{label}.reference")
    else:
        expression = _parse_expression(obj["expression"],
                                       f"{label}.expression")
        null_num, null_den = _parse_null_ratio(
            obj.get("null_ratio"), f"{label}.null_ratio")
    return Column(name, col_type, generator=generator, reference=reference,
                  expression=expression, null_num=null_num,
                  null_den=null_den, max_utf8_bytes=cap)


def _parse_table(obj: Any, label: str) -> Table:
    obj = _req_obj(obj, label)
    _no_extra(obj, {"name", "row_count", "primary_key", "columns"}, label)
    name = _req_str(obj.get("name"), f"{label}.name")
    row_count = _req_int(obj.get("row_count"), f"{label}.row_count", 0,
                         UINT64_MOD)
    pk = _req_obj(obj.get("primary_key"), f"{label}.primary_key")
    _no_extra(pk, {"column", "base", "stride"}, f"{label}.primary_key")
    pk_column = _req_str(pk.get("column"), f"{label}.primary_key.column")
    base = _req_int64(pk.get("base"), f"{label}.primary_key.base")
    stride = _req_int64(pk.get("stride"), f"{label}.primary_key.stride")
    _require(stride > 0,
             f"{label}.primary_key.stride: must be positive, got {stride}")
    if row_count > 0:
        last = base + (row_count - 1) * stride
        _require(INT64_MIN <= last <= INT64_MAX,
                 f"{label}: last primary key {last} (base {base} + "
                 f"{row_count - 1} * stride {stride}) leaves the signed "
                 "64-bit range")
    raw_columns = obj.get("columns")
    _require(isinstance(raw_columns, list),
             f"{label}.columns: expected a list")
    columns = tuple(_parse_column(c, f"{label}.columns[{i}]")
                    for i, c in enumerate(raw_columns))
    seen: set[str] = set()
    for col in columns:
        _require(col.name not in seen,
                 f"{label}: duplicate column name {col.name!r}")
        seen.add(col.name)
    _require(pk_column not in seen,
             f"{label}: primary key column {pk_column!r} also appears in "
             "\"columns\"")
    return Table(name, row_count, pk_column, base, stride, columns)


# -- static expression analysis ----------------------------------------------

def _infer_type(expr, table: Table, recipe: Recipe, label: str) -> str:
    """Statically infer ``integer`` / ``text`` / ``null`` for *expr*."""
    if isinstance(expr, Lit):
        if expr.value is None:
            return _TYPE_NULL
        return _TYPE_INT if _is_int(expr.value) else _TYPE_TEXT
    if isinstance(expr, ColRef):
        if expr.name == table.pk_column:
            return _TYPE_INT
        col = table.column_map.get(expr.name)
        _require(col is not None,
                 f"{label}: unknown column {expr.name!r} in table "
                 f"{table.name!r}")
        return col.type
    if isinstance(expr, RefRead):
        ref_col = table.column_map.get(expr.column)
        _require(ref_col is not None,
                 f"{label}: unknown reference column {expr.column!r} in "
                 f"table {table.name!r}")
        _require(ref_col.reference is not None,
                 f"{label}: column {expr.column!r} of table "
                 f"{table.name!r} is not a reference column")
        target = recipe.table_map[ref_col.reference.target]
        if expr.target_column == target.pk_column:
            return _TYPE_INT
        tcol = target.column_map.get(expr.target_column)
        _require(tcol is not None,
                 f"{label}: unknown column {expr.target_column!r} in "
                 f"target table {target.name!r}")
        return tcol.type
    if isinstance(expr, Fold):
        want = _TYPE_INT if expr.op in (_OP_ADD, _OP_MUL) else _TYPE_TEXT
        for i, arg in enumerate(expr.args):
            got = _infer_type(arg, table, recipe, f"{label}.args[{i}]")
            _require(got in (want, _TYPE_NULL),
                     f"{label}: \"{expr.op}\" requires {want} operands, "
                     f"but operand {i} is {got}")
        return want
    if isinstance(expr, Coalesce):
        seen: set[str] = set()
        for i, arg in enumerate(expr.args):
            got = _infer_type(arg, table, recipe, f"{label}.args[{i}]")
            if got != _TYPE_NULL:
                seen.add(got)
        _require(len(seen) <= 1,
                 f"{label}: \"coalesce\" operands disagree on type "
                 f"{sorted(seen)}")
        return seen.pop() if seen else _TYPE_NULL
    raise AssertionError(f"unhandled expression node {expr!r}")


def _expression_edges(expr, table: Table, recipe: Recipe,
                      out: list) -> None:
    """Append ((target_table, target_column), label) dependency edges."""
    if isinstance(expr, ColRef):
        out.append(((table.name, expr.name), f"col {expr.name}"))
    elif isinstance(expr, RefRead):
        ref_col = table.column_map[expr.column]  # validated by _infer_type
        target = ref_col.reference.target
        out.append(((table.name, expr.column), f"col {expr.column}"))
        out.append(((target, expr.target_column),
                    f"ref {expr.column} -> {target}.{expr.target_column}"))
    elif isinstance(expr, (Fold, Coalesce)):
        for arg in expr.args:
            _expression_edges(arg, table, recipe, out)


def _check_derived_cycles(recipe: Recipe) -> None:
    """Reject cycles in the derived-column value dependency graph.

    Only derived columns have outgoing edges; plain and reference columns
    are sinks.  Foreign-key references themselves create no edges, so
    referential cycles between tables are fine as long as no derived
    value depends on itself across them.
    """
    edges: dict[tuple, list] = {}
    for table in recipe.tables:
        for col in table.columns:
            if col.is_derived:
                found: list = []
                _expression_edges(col.expression, table, recipe, found)
                edges[(table.name, col.name)] = found

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[tuple, int] = {}
    stack: list[tuple] = []

    def visit(node: tuple) -> None:
        color[node] = GRAY
        stack.append(node)
        for (target, label) in edges.get(node, ()):
            state = color.get(target, WHITE)
            if state == GRAY:
                idx = stack.index(target)
                parts = [f"{t}.{c}" for t, c in stack[idx:]]
                parts.append(f"{target[0]}.{target[1]}")
                raise _err(
                    "derived-column dependency cycle: " + " -> ".join(parts)
                    + " (derived values may not depend on themselves, even "
                      "across tables or through coalesce)")
            if state == WHITE:
                visit(target)
        stack.pop()
        color[node] = BLACK

    for node in edges:  # recipe declaration order: deterministic reports
        if color.get(node, WHITE) == WHITE:
            visit(node)


def compile_recipe(document: Any) -> Recipe:
    """Validate a recipe document and return a compiled :class:`Recipe`.

    ``document`` is the already-parsed JSON object (no SQL, no Python is
    ever executed).  All problems are reported as :class:`RecipeError`
    before any row can be generated.
    """
    obj = _req_obj(document, "recipe")
    _no_extra(obj, {"version", "tables"}, "recipe")
    version = obj.get("version", 1)
    _require(version == 1,
             f"recipe.version: only version 1 is supported, got "
             f"{version!r}")
    raw_tables = obj.get("tables")
    _require(isinstance(raw_tables, list) and len(raw_tables) > 0,
             "recipe.tables: expected a non-empty list")
    tables = tuple(_parse_table(t, f"recipe.tables[{i}]")
                   for i, t in enumerate(raw_tables))
    seen: set[str] = set()
    for table in tables:
        _require(table.name not in seen,
                 f"recipe: duplicate table name {table.name!r}")
        seen.add(table.name)
    recipe = Recipe(tables)

    # Resolve references and required-reference emptiness up front.
    for table in tables:
        for col in table.columns:
            ref = col.reference
            if ref is None:
                continue
            label = f"table {table.name!r} column {col.name!r}"
            target = recipe.table_map.get(ref.target)
            _require(target is not None,
                     f"{label}: reference points at unknown table "
                     f"{ref.target!r}")
            if target.row_count == 0:
                _require(ref.nullable,
                         f"{label}: required reference to empty table "
                         f"{target.name!r}; make the reference nullable "
                         "or give the target table rows")

    # Static typing of derived expressions.
    for table in tables:
        for col in table.columns:
            if not col.is_derived:
                continue
            label = f"table {table.name!r} column {col.name!r} expression"
            inferred = _infer_type(col.expression, table, recipe, label)
            _require(inferred in (col.type, _TYPE_NULL),
                     f"{label}: produces {inferred} but the column is "
                     f"declared {col.type}")

    _check_derived_cycles(recipe)
    return recipe


def load_recipe(path: str | Path) -> Recipe:
    """Load a JSON recipe file and compile it."""
    with open(path, "r", encoding="utf-8") as fh:
        document = json.load(fh)
    return compile_recipe(document)


def _coerce_recipe(recipe: Recipe | dict | str | Path) -> Recipe:
    if isinstance(recipe, Recipe):
        return recipe
    if isinstance(recipe, dict):
        return compile_recipe(recipe)
    if isinstance(recipe, (str, Path)):
        return load_recipe(recipe)
    raise TypeError(f"cannot use {type(recipe).__name__} as a recipe")


def _check_seed(seed: Any) -> int:
    if not _is_int(seed) or not 0 <= seed <= SEED_MAX:
        raise GenerationError(
            f"seed must be an integer in [0, 2**64), got {seed!r}")
    return seed


# --------------------------------------------------------------------------
# Row factory: per-address row generation
# --------------------------------------------------------------------------

_IN_PROGRESS = object()


class RowFactory:
    """Generates rows of a compiled recipe by address.

    ``cache_rows`` bounds the LRU cache of partially/fully computed rows;
    the cache only ever speeds up repeated access and never changes
    values.  ``rows_computed`` counts how many row addresses were actually
    visited, which makes dependency behaviour observable.
    """

    def __init__(self, recipe: Recipe, seed: int,
                 cache_rows: int = DEFAULT_CACHE_ROWS):
        self.recipe = recipe
        self.seed = _check_seed(seed)
        if not _is_int(cache_rows) or cache_rows < 1:
            raise GenerationError(
                f"cache_rows must be a positive integer, got "
                f"{cache_rows!r}")
        self.cache_rows = cache_rows
        self._memos: OrderedDict[tuple, dict] = OrderedDict()
        self.rows_computed = 0

    # -- public -------------------------------------------------------------

    def row(self, table: str, row_number: int) -> dict:
        """Return the full row ``{column: value}`` at one address."""
        table_obj = self._locate(table, row_number)
        memo = self._memo(table_obj, row_number)
        for col in table_obj.columns:
            self._column_value(table_obj, row_number, col, memo, [])
        return self._ordered_row(table_obj, memo)

    def rows(self, table: str, start: int, stop: int) -> Iterator:
        """Yield ``(row_number, row)`` for ``start <= row < stop``."""
        table_obj = self.recipe.table(table)
        if not (_is_int(start) and _is_int(stop)
                and 0 <= start <= stop <= table_obj.row_count):
            raise GenerationError(
                f"row range [{start!r}, {stop!r}) is outside table "
                f"{table!r} with {table_obj.row_count} rows")
        for row_number in range(start, stop):
            yield row_number, self.row(table, row_number)

    # -- addressing ---------------------------------------------------------

    def _locate(self, table: str, row_number: int) -> Table:
        table_obj = self.recipe.table(table)
        if not _is_int(row_number) or not 0 <= row_number < table_obj.row_count:
            raise GenerationError(
                f"row number {row_number!r} is outside table "
                f"{table_obj.name!r} with {table_obj.row_count} rows "
                f"(valid: 0..{table_obj.row_count - 1})",
                table=table_obj.name,
                row=row_number if _is_int(row_number) else None)
        return table_obj

    def _memo(self, table: Table, row_number: int) -> dict:
        key = (table.name, row_number)
        memo = self._memos.get(key)
        if memo is None:
            self.rows_computed += 1
            memo = {table.pk_column: table.key_of(row_number)}
            self._memos[key] = memo
            self._memos.move_to_end(key)
            while len(self._memos) > self.cache_rows:
                self._memos.popitem(last=False)
        else:
            self._memos.move_to_end(key)
        return memo

    @staticmethod
    def _ordered_row(table: Table, memo: dict) -> dict:
        row = {table.pk_column: memo[table.pk_column]}
        for col in table.columns:
            row[col.name] = memo[col.name]
        return row

    # -- column values --------------------------------------------------------

    def _column_value(self, table: Table, row_number: int, col: Column,
                      memo: dict, stack: list):
        if col.name in memo:
            value = memo[col.name]
            if value is _IN_PROGRESS:
                raise GenerationError(
                    f"internal error: unguarded dependency cycle at "
                    f"{table.name!r}[{row_number}].{col.name!r}",
                    table=table.name, row=row_number, column=col.name,
                    path=tuple(stack))
            return value
        memo[col.name] = _IN_PROGRESS
        stack.append(f"{table.name}[{row_number}].{col.name}")
        try:
            if col.reference is not None:
                value = self._reference_value(table, row_number, col)
            elif col.null_num == col.null_den:  # ratio 1: always NULL
                value = None
            elif col.null_num and self._draw_null(table, row_number, col):
                value = None
            elif col.generator is not None:
                value = self._generated_value(table, row_number, col)
            else:
                value = self._eval(col.expression, table, row_number, memo,
                                   stack)
            if isinstance(value, str):
                size = len(value.encode("utf-8"))
                if size > col.max_utf8_bytes:
                    raise GenerationError(
                        f"value has {size} UTF-8 bytes, exceeding the "
                        f"column cap of {col.max_utf8_bytes}: {value!r}",
                        table=table.name, row=row_number, column=col.name,
                        path=tuple(stack))
            memo[col.name] = value
            return value
        finally:
            stack.pop()

    def _draw_null(self, table: Table, row_number: int, col: Column) -> bool:
        draw = _draw_u64(self.seed, table.name, col.name, row_number,
                         _PURPOSE_NULL)
        return draw % col.null_den < col.null_num

    def _generated_value(self, table: Table, row_number: int, col: Column):
        gen = col.generator
        if isinstance(gen, NullGen):
            return None
        if isinstance(gen, IntRangeGen):
            draw = _draw_u64(self.seed, table.name, col.name, row_number,
                             _PURPOSE_VALUE)
            return gen.lo + draw % (gen.hi - gen.lo + 1)
        if isinstance(gen, ChoiceGen):
            draw = _draw_u64(self.seed, table.name, col.name, row_number,
                             _PURPOSE_VALUE)
            return gen.values[draw % len(gen.values)]
        if isinstance(gen, TextGen):
            suffix_len = gen.length - len(gen.prefix)
            return gen.prefix + _draw_letters(
                self.seed, table.name, col.name, row_number, suffix_len)
        raise AssertionError(f"unhandled generator {gen!r}")

    def _reference_value(self, table: Table, row_number: int, col: Column):
        ref = col.reference
        target = self.recipe.table_map[ref.target]
        if ref.nullable:
            if target.row_count == 0:
                return None
            if ref.null_num == ref.null_den:
                return None
            if ref.null_num:
                draw = _draw_u64(self.seed, table.name, col.name,
                                 row_number, _PURPOSE_REFNULL)
                if draw % ref.null_den < ref.null_num:
                    return None
        # A required reference to an empty table was rejected at compile
        # time, so target.row_count is never zero here.
        if ref.select == _SELECT_ROUND_ROBIN:
            parent_row = row_number % target.row_count
        else:
            draw = _draw_u64(self.seed, table.name, col.name, row_number,
                             _PURPOSE_REF)
            parent_row = draw % target.row_count
        return target.key_of(parent_row)

    # -- expression evaluation --------------------------------------------------

    def _eval(self, expr, table: Table, row_number: int, memo: dict,
              stack: list):
        if isinstance(expr, Lit):
            return expr.value
        if isinstance(expr, ColRef):
            if expr.name == table.pk_column:
                return memo[table.pk_column]
            return self._column_value(
                table, row_number, table.column_map[expr.name], memo, stack)
        if isinstance(expr, RefRead):
            ref_col = table.column_map[expr.column]
            fk = self._column_value(table, row_number, ref_col, memo, stack)
            if fk is None:
                return None
            target = self.recipe.table_map[ref_col.reference.target]
            parent_row = (fk - target.pk_base) // target.pk_stride
            stack.append(f"via {table.name}.{ref_col.name} -> "
                         f"{target.name}[{parent_row}]")
            try:
                tmemo = self._memo(target, parent_row)
                if expr.target_column == target.pk_column:
                    return tmemo[target.pk_column]
                return self._column_value(
                    target, parent_row,
                    target.column_map[expr.target_column], tmemo, stack)
            finally:
                stack.pop()
        if isinstance(expr, Fold):
            return self._eval_fold(expr, table, row_number, memo, stack)
        if isinstance(expr, Coalesce):
            result = None
            for arg in expr.args:
                result = self._eval(arg, table, row_number, memo, stack)
                if result is not None:
                    break
            return result
        raise AssertionError(f"unhandled expression node {expr!r}")

    def _eval_fold(self, expr: Fold, table: Table, row_number: int,
                   memo: dict, stack: list):
        op = expr.op
        acc = self._eval(expr.args[0], table, row_number, memo, stack)
        for arg in expr.args[1:]:
            value = self._eval(arg, table, row_number, memo, stack)
            if acc is None or value is None:
                acc = None
                continue
            if op in (_OP_ADD, _OP_MUL):
                if not (_is_int(acc) and _is_int(value)):
                    raise GenerationError(
                        f"\"{op}\" requires integer operands, got "
                        f"{acc!r} and {value!r}",
                        table=table.name, row=row_number,
                        path=tuple(stack))
                acc = acc + value if op == _OP_ADD else acc * value
                if not INT64_MIN <= acc <= INT64_MAX:
                    raise GenerationError(
                        f"integer overflow in \"{op}\": intermediate "
                        f"result {acc} leaves the signed 64-bit range "
                        f"[{INT64_MIN}, {INT64_MAX}]",
                        table=table.name, row=row_number,
                        path=tuple(stack))
            else:
                if not (isinstance(acc, str) and isinstance(value, str)):
                    raise GenerationError(
                        f"\"concat\" requires text operands, got "
                        f"{acc!r} and {value!r}",
                        table=table.name, row=row_number,
                        path=tuple(stack))
                acc = acc + value
        return acc


# --------------------------------------------------------------------------
# Module-level convenience API
# --------------------------------------------------------------------------

def generate_row(recipe: Recipe | dict | str | Path, seed: int, table: str,
                 row_number: int) -> dict:
    """Generate one addressed row as ``{column: value}``."""
    return RowFactory(_coerce_recipe(recipe), seed).row(table, row_number)


def generate_rows(recipe: Recipe | dict | str | Path, seed: int, table: str,
                  start: int, stop: int) -> Iterator:
    """Yield ``(row_number, row)`` for the contiguous range [start, stop).

    Splitting ``[0, N)`` into adjacent segments, generating them in any
    order and interleaving other tables yields byte-identical rows: every
    value is a pure function of the seed and the row address.
    """
    return RowFactory(_coerce_recipe(recipe), seed).rows(table, start, stop)


# --------------------------------------------------------------------------
# SQLite materialisation
# --------------------------------------------------------------------------

def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class GenDbResult:
    """Summary of a successful :func:`generate_database` run."""

    __slots__ = ("database_path", "table_rows", "verified_rows",
                 "elapsed_seconds")

    def __init__(self, database_path: str, table_rows: dict,
                 verified_rows: int, elapsed_seconds: float):
        self.database_path = database_path
        self.table_rows = table_rows
        self.verified_rows = verified_rows
        self.elapsed_seconds = elapsed_seconds

    def to_dict(self) -> dict:
        return {
            "database_path": self.database_path,
            "table_rows": dict(self.table_rows),
            "verified_rows": self.verified_rows,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


def _sample_row_numbers(count: int) -> list[int]:
    if count == 0:
        return []
    picks = {0, count // 2, count - 1}
    return sorted(picks)


def generate_database(recipe: Recipe | dict | str | Path, seed: int,
                      output_path: str | Path, *,
                      cache_size_kb: int = DEFAULT_CACHE_SIZE_KB,
                      batch_size: int = DEFAULT_BATCH_SIZE,
                      cache_rows: int = DEFAULT_CACHE_ROWS) -> GenDbResult:
    """Materialise a recipe into a new SQLite database.

    The schema keeps the recipe's column order and storage types and
    declares a real ``INTEGER PRIMARY KEY`` plus ``DEFERRABLE INITIALLY
    DEFERRED`` foreign keys, so mutually referencing tables are inserted
    in one transaction and checked at commit.  Afterwards the row counts,
    the primary-key series, ``PRAGMA foreign_key_check`` and a sample of
    freshly regenerated rows (derived columns included) are verified
    before the database is atomically moved into place.  Any failure
    removes the incomplete file and raises.
    """
    recipe_obj = _coerce_recipe(recipe)
    factory = RowFactory(recipe_obj, seed, cache_rows=cache_rows)
    output = Path(output_path)
    if output.exists():
        raise FileExistsError(f"output database already exists: {output}")
    if not _is_int(batch_size) or batch_size < 1:
        raise GenerationError(
            f"batch_size must be a positive integer, got {batch_size!r}")
    if not _is_int(cache_size_kb) or cache_size_kb < 1:
        raise GenerationError(
            f"cache_size_kb must be a positive integer, got "
            f"{cache_size_kb!r}")
    tmp = output.with_name(f"{output.name}.tmp-{os.getpid()}")
    started = time.monotonic()
    conn = sqlite3.connect(tmp)
    try:
        conn.isolation_level = None  # explicit transaction control
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA cache_size = -{cache_size_kb}")
        for table in recipe_obj.tables:
            defs = [f"{_quote_ident(table.pk_column)} INTEGER PRIMARY KEY"]
            fks = []
            for col in table.columns:
                sql_type = "INTEGER" if col.type == _TYPE_INT else "TEXT"
                defs.append(f"{_quote_ident(col.name)} {sql_type}")
                if col.reference is not None:
                    target = recipe_obj.table_map[col.reference.target]
                    fks.append(
                        f"FOREIGN KEY ({_quote_ident(col.name)}) "
                        f"REFERENCES {_quote_ident(target.name)}"
                        f"({_quote_ident(target.pk_column)}) "
                        "DEFERRABLE INITIALLY DEFERRED")
            conn.execute(
                f"CREATE TABLE {_quote_ident(table.name)} "
                f"({', '.join(defs + fks)})")

        conn.execute("BEGIN")
        for table in recipe_obj.tables:
            names = [table.pk_column] + [c.name for c in table.columns]
            sql = (f"INSERT INTO {_quote_ident(table.name)} "
                   f"({', '.join(_quote_ident(n) for n in names)}) "
                   f"VALUES ({', '.join('?' for _ in names)})")
            batch: list[tuple] = []
            for _row_number, row in factory.rows(table.name, 0,
                                                 table.row_count):
                batch.append(tuple(row[name] for name in names))
                if len(batch) >= batch_size:
                    conn.executemany(sql, batch)
                    batch.clear()
            if batch:
                conn.executemany(sql, batch)
        try:
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            raise GenerationError(
                f"foreign-key check failed at commit: {exc}") from exc

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise GenerationError(
                f"foreign_key_check reported {len(violations)} "
                f"violation(s), first: {violations[0]!r}")

        table_rows: dict[str, int] = {}
        for table in recipe_obj.tables:
            quoted = _quote_ident(table.name)
            count = conn.execute(
                f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            if count != table.row_count:
                raise GenerationError(
                    f"table {table.name!r}: expected {table.row_count} "
                    f"rows, database holds {count}")
            table_rows[table.name] = count
            if count:
                pk = _quote_ident(table.pk_column)
                lo, hi, total = conn.execute(
                    f"SELECT MIN({pk}), MAX({pk}), "
                    f"SUM({pk}) FROM {quoted}").fetchone()
                want_lo = table.key_of(0)
                want_hi = table.key_of(count - 1)
                want_sum = (count * table.pk_base
                            + table.pk_stride * (count - 1) * count // 2)
                if (lo, hi) != (want_lo, want_hi):
                    raise GenerationError(
                        f"table {table.name!r}: primary key range "
                        f"[{lo}, {hi}] != expected [{want_lo}, {want_hi}]")
                if INT64_MIN <= want_sum <= INT64_MAX and total != want_sum:
                    raise GenerationError(
                        f"table {table.name!r}: primary key sum {total} "
                        f"!= expected series sum {want_sum}")

        verified = 0
        for table in recipe_obj.tables:
            names = [table.pk_column] + [c.name for c in table.columns]
            select = (f"SELECT {', '.join(_quote_ident(n) for n in names)} "
                      f"FROM {_quote_ident(table.name)} "
                      f"WHERE {_quote_ident(table.pk_column)} = ?")
            for row_number in _sample_row_numbers(table.row_count):
                expected = factory.row(table.name, row_number)
                got = conn.execute(
                    select, (table.key_of(row_number),)).fetchone()
                want = tuple(expected[name] for name in names)
                if tuple(got) != want:
                    raise GenerationError(
                        f"table {table.name!r} row {row_number}: database "
                        f"holds {got!r}, generator produced {want!r}")
                verified += 1
        conn.close()
    except Exception:
        try:
            conn.close()
        finally:
            tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, output)
    return GenDbResult(str(output), table_rows, verified,
                       time.monotonic() - started)
