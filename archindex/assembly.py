"""Assemble nested JSONL delivery files from relational tables.

Given an existing SQLite database and an assembly plan describing a
finite-depth tree of nodes, :func:`assemble_nested` writes one UTF-8 JSON
object per root record (JSONL).  Each object carries the root's projected
scalars plus correctly-attributed child arrays; sibling arrays are organised
independently and never multiply each other the way a flat JOIN would.

Plan format (JSON object with a ``nodes`` list)::

    {
      "nodes": [
        {"name": "order", "table": "handover_order",
         "identity": ["order_id"],
         "scalars": [{"column": "order_no", "alias": "orderNo"}],
         "order_by": ["order_no"]},
        {"name": "box", "table": "box",
         "identity": ["box_id"],
         "parent": "order", "array": "boxes", "foreign_key": ["order_id"],
         "scalars": [{"column": "box_no", "alias": "boxNo"}],
         "order_by": ["sort_key"]}
      ]
    }

* ``name`` uniquely identifies the node; the same table may appear in
  several nodes under different names.
* ``identity`` lists one or more INTEGER/TEXT columns that are non-null and
  unique across the node's whole table.  Components compare by value *and*
  storage type (TEXT ``'01'`` is not INTEGER ``1``); components are never
  trimmed, concatenated or otherwise fused.
* ``scalars`` projects output columns under aliases.  Only declared columns
  are emitted; identity columns are not output unless declared here.
* ``order_by`` (optional, possibly empty) sorts the node's rows inside the
  parent array (or the file, for the root).  Sort components must be
  INTEGER, TEXT or NULL (NULL first); non-null values of one sort column
  must share a single storage type.  Ties are broken by the node identity,
  so output never depends on insertion order or rowid.
* Non-root nodes add ``parent`` (node name), ``array`` (key under which the
  children appear in the parent object) and ``foreign_key`` (columns
  matching the parent identity component-by-component in count and storage
  type).  Exactly one node has no ``parent``.

Every participating row must belong to exactly one parent: duplicate
identities, null or partially-null foreign keys and orphans abort the whole
run before the first business object is written, as do BLOB/NaN/Inf or
otherwise unsupported values.  Configuration never carries SQL fragments:
column references are plain names that are only ever used as safely quoted
identifiers (names may legitimately contain spaces or double quotes).

Consistency and resource profile
--------------------------------
All source rows are read inside a single read transaction on a read-only
connection, so the whole file reflects one consistent snapshot even when
other connections write (WAL) while the export runs.  Rows are streamed
into a private scratch SQLite database created inside the caller-supplied
temporary directory (never into the source database, which stays untouched
-- no tables, no indexes, no journal changes).  Per-node access tables are
materialised there ordered by ``(parent sequence, sort columns, identity)``
via SQLite's disk-backed sorter, so neither a single huge root nor a huge
child array is ever held in Python memory, and no child table is ever
re-scanned per parent.  Emission walks one forward-only cursor per node
(recursion depth equals the plan depth) and writes fragments straight into
a bounded output buffer.

Memory composition (all tunable): the read-only source connection (default
SQLite cache), the scratch connection cache (``cache_size_kb``), one
batched cursor per node (``batch_size`` rows each), SQLite's sorter staging
(disk-backed, spills into the temporary directory) and the JSONL write
buffer (``write_buffer_bytes``).  A single row's projected JSON fragment is
capped by ``max_row_payload_bytes``.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

from .errors import AssemblyDataError, AssemblyPlanError

DEFAULT_CACHE_SIZE_KB = 4096
DEFAULT_BATCH_SIZE = 1000
DEFAULT_MAX_ROW_PAYLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_WRITE_BUFFER_BYTES = 256 * 1024

ProgressCallback = Callable[[str, str, int], None]


# --------------------------------------------------------------------------
# Plan model and parsing
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScalarSpec:
    column: str
    alias: str


@dataclass(frozen=True, slots=True)
class NodeSpec:
    name: str
    table: str
    identity: tuple[str, ...]
    scalars: tuple[ScalarSpec, ...]
    order_by: tuple[str, ...]
    parent: str | None
    array: str | None
    foreign_key: tuple[str, ...]

    @property
    def is_root(self) -> bool:
        return self.parent is None


@dataclass(frozen=True, slots=True)
class AssemblyPlan:
    nodes: tuple[NodeSpec, ...]
    root: str
    children: dict[str, tuple[str, ...]]
    topo: tuple[str, ...]
    by_name: dict[str, NodeSpec] = field(compare=False, repr=False)


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssemblyPlanError(f"{label} must be an object")
    return value


def _non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssemblyPlanError(f"{label} must be a non-empty string")
    return value


def _str_list(value: Any, label: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        expected = "a string list" if allow_empty else "a non-empty string list"
        raise AssemblyPlanError(f"{label} must be {expected}")
    items: list[str] = []
    for index, item in enumerate(value):
        items.append(_non_empty_str(item, f"{label}[{index}]"))
    if len(set(items)) != len(items):
        raise AssemblyPlanError(f"{label} contains duplicate entries")
    return tuple(items)


def parse_assembly_plan(data: Any) -> AssemblyPlan:
    """Validate a decoded JSON assembly plan and return an immutable object."""
    root_obj = _require_object(data, "assembly plan")
    raw_nodes = root_obj.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise AssemblyPlanError("plan.nodes must be a non-empty list")

    nodes: list[NodeSpec] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        item = _require_object(raw, f"plan.nodes[{index}]")
        label = f"plan.nodes[{index}]"
        name = _non_empty_str(item.get("name"), f"{label}.name")
        if name in seen_names:
            raise AssemblyPlanError(f"duplicate node name {name!r}")
        seen_names.add(name)
        table = _non_empty_str(item.get("table"), f"node {name!r}.table")
        identity = _str_list(item.get("identity"), f"node {name!r}.identity",
                             allow_empty=False)

        raw_scalars = item.get("scalars", [])
        if not isinstance(raw_scalars, list):
            raise AssemblyPlanError(f"node {name!r}.scalars must be a list")
        scalars: list[ScalarSpec] = []
        seen_aliases: set[str] = set()
        for s_index, raw_scalar in enumerate(raw_scalars):
            s_item = _require_object(
                raw_scalar, f"node {name!r}.scalars[{s_index}]")
            column = _non_empty_str(
                s_item.get("column"),
                f"node {name!r}.scalars[{s_index}].column")
            alias = _non_empty_str(
                s_item.get("alias"),
                f"node {name!r}.scalars[{s_index}].alias")
            if alias in seen_aliases:
                raise AssemblyPlanError(
                    f"node {name!r}: duplicate scalar alias {alias!r}")
            seen_aliases.add(alias)
            scalars.append(ScalarSpec(column=column, alias=alias))

        order_by = _str_list(item.get("order_by", []),
                             f"node {name!r}.order_by", allow_empty=True)

        parent = item.get("parent")
        array = item.get("array")
        foreign_key = item.get("foreign_key")
        if parent is None:
            if array is not None or foreign_key is not None:
                raise AssemblyPlanError(
                    f"root node {name!r} must not declare 'array' or "
                    "'foreign_key'")
            fk: tuple[str, ...] = ()
        else:
            parent = _non_empty_str(parent, f"node {name!r}.parent")
            array = _non_empty_str(array, f"node {name!r}.array")
            fk = _str_list(foreign_key, f"node {name!r}.foreign_key",
                           allow_empty=False)
        nodes.append(NodeSpec(
            name=name, table=table, identity=identity,
            scalars=tuple(scalars), order_by=order_by, parent=parent,
            array=array, foreign_key=fk))

    by_name = {node.name: node for node in nodes}
    roots = [node.name for node in nodes if node.is_root]
    if len(roots) != 1:
        raise AssemblyPlanError(
            f"plan must declare exactly one root node (a node without "
            f"'parent'); found {len(roots)}: {sorted(roots)!r}")

    children: dict[str, list[str]] = {node.name: [] for node in nodes}
    for node in nodes:
        if node.is_root:
            continue
        assert node.parent is not None
        parent_node = by_name.get(node.parent)
        if parent_node is None:
            raise AssemblyPlanError(
                f"node {node.name!r}: unknown parent node {node.parent!r}")
        if len(node.foreign_key) != len(parent_node.identity):
            raise AssemblyPlanError(
                f"node {node.name!r}: foreign_key has "
                f"{len(node.foreign_key)} column(s) but parent node "
                f"{node.parent!r} identity has {len(parent_node.identity)}")
        children[node.parent].append(node.name)

    # Scalar aliases and child array names share one object namespace.
    for node in nodes:
        taken = {scalar.alias for scalar in node.scalars}
        for child_name in children[node.name]:
            array = by_name[child_name].array
            assert array is not None
            if array in taken:
                raise AssemblyPlanError(
                    f"node {node.name!r}: child array name {array!r} "
                    "conflicts with a scalar alias or another child array")
            taken.add(array)

    # Reachability from the single root; unreachable nodes mean a cycle.
    topo: list[str] = []
    stack = [roots[0]]
    seen: set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        topo.append(name)
        stack.extend(reversed(children[name]))
    if len(seen) != len(nodes):
        missing = sorted(node.name for node in nodes if node.name not in seen)
        raise AssemblyPlanError(
            f"plan contains a cycle or nodes unreachable from the root: "
            f"{missing!r}")

    return AssemblyPlan(
        nodes=tuple(nodes), root=roots[0],
        children={k: tuple(v) for k, v in children.items()},
        topo=tuple(topo), by_name=by_name)


def load_assembly_plan(path: str | Path) -> AssemblyPlan:
    """Load and validate an assembly plan from a JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_assembly_plan(json.load(fh))


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AssemblyResult:
    database_path: str
    output_path: str
    roots: int
    node_rows: dict[str, int]
    output_bytes: int
    elapsed_seconds: float


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _quote_ident(name: str) -> str:
    # Any string is a legitimate identifier once double quotes are doubled;
    # configuration values are never interpolated as SQL text.
    return '"' + name.replace('"', '""') + '"'


def _storage_name(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # pragma: no cover - sqlite never yields bool
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, str):
        return "TEXT"
    if isinstance(value, float):
        return "REAL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "BLOB"
    return type(value).__name__


def _fmt_identity(values: Any) -> str:
    try:
        return json.dumps(list(values), ensure_ascii=False, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return repr(values)


class _NodeQuery:
    """Column layout of one node's extraction query."""

    __slots__ = ("sql", "id_pos", "fk_pos", "sort_pos", "scalar_pos")

    def __init__(self, node: NodeSpec):
        columns: list[str] = []
        positions: dict[str, int] = {}

        def pos(column: str) -> int:
            index = positions.get(column)
            if index is None:
                index = len(columns)
                positions[column] = index
                columns.append(column)
            return index

        self.id_pos = tuple(pos(c) for c in node.identity)
        self.fk_pos = tuple(pos(c) for c in node.foreign_key)
        self.sort_pos = tuple(pos(c) for c in node.order_by)
        self.scalar_pos = tuple(
            (pos(s.column), s.alias, s.column) for s in node.scalars)
        select = ", ".join(_quote_ident(c) for c in columns)
        self.sql = f"SELECT {select} FROM {_quote_ident(node.table)}"


def _check_key_component(value: Any, node: NodeSpec, column: str, kind: str,
                         raw_identity: tuple[Any, ...]) -> None:
    if value is None:
        raise AssemblyDataError(
            f"node {node.name!r}: null {kind} component in column "
            f"{column!r} (row identity {_fmt_identity(raw_identity)}); "
            "every row must have a complete non-null "
            f"{'identity' if kind == 'identity' else 'parent reference'}")
    if type(value) not in (int, str):
        raise AssemblyDataError(
            f"node {node.name!r}: {kind} column {column!r} has unsupported "
            f"storage type {_storage_name(value)} (row identity "
            f"{_fmt_identity(raw_identity)}); identity and foreign-key "
            "columns only allow INTEGER or TEXT")


def _encode_scalar(value: Any, node: NodeSpec, column: str,
                   identity: tuple[Any, ...]) -> str:
    if value is None:
        return "null"
    if type(value) is int:
        # 64-bit integers are rendered exactly, never through a float.
        return str(value)
    if type(value) is str:
        # TEXT stays a JSON string even when it looks like a number.
        return json.dumps(value, ensure_ascii=False)
    if type(value) is float:
        if not math.isfinite(value):
            raise AssemblyDataError(
                f"node {node.name!r}: column {column!r} holds a non-finite "
                f"REAL (row identity {_fmt_identity(identity)}); only "
                "finite REAL values are supported")
        return json.dumps(value)
    raise AssemblyDataError(
        f"node {node.name!r}: column {column!r} has unsupported storage "
        f"type {_storage_name(value)} (row identity "
        f"{_fmt_identity(identity)}); supported scalar values are NULL, "
        "INTEGER, TEXT and finite REAL")


class _PeekCursor:
    """Forward-only batched cursor with one-row lookahead."""

    __slots__ = ("_cursor", "_batch_size", "_batch", "_pos", "_peeked")

    def __init__(self, cursor: sqlite3.Cursor, batch_size: int):
        self._cursor = cursor
        self._batch_size = batch_size
        self._batch: list[tuple] = []
        self._pos = 0
        self._peeked: tuple | None = None

    def peek(self) -> tuple | None:
        if self._peeked is None:
            if self._pos >= len(self._batch):
                self._batch = self._cursor.fetchmany(self._batch_size)
                self._pos = 0
                if not self._batch:
                    return None
            self._peeked = self._batch[self._pos]
            self._pos += 1
        return self._peeked

    def pop(self) -> tuple | None:
        row = self.peek()
        self._peeked = None
        return row


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _coerce_plan(plan: AssemblyPlan | dict[str, Any] | str | Path) -> AssemblyPlan:
    if isinstance(plan, AssemblyPlan):
        return plan
    if isinstance(plan, dict):
        return parse_assembly_plan(plan)
    return load_assembly_plan(plan)


def _check_schema(conn: sqlite3.Connection, plan: AssemblyPlan) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }
    for node in plan.nodes:
        if node.table not in tables:
            raise AssemblyPlanError(
                f"node {node.name!r}: table {node.table!r} does not exist "
                "in the source database")
        columns = {
            row[1]
            for row in conn.execute(
                f"PRAGMA table_info({_quote_ident(node.table)})")
        }
        referenced = (
            [("identity", c) for c in node.identity]
            + [("foreign_key", c) for c in node.foreign_key]
            + [("order_by", c) for c in node.order_by]
            + [("scalars", s.column) for s in node.scalars])
        for role, column in referenced:
            if column not in columns:
                raise AssemblyPlanError(
                    f"node {node.name!r}: {role} column {column!r} does not "
                    f"exist in table {node.table!r}")


def _extract_node(src: sqlite3.Connection, tmp: sqlite3.Connection,
                  node: NodeSpec, staging: str, query: _NodeQuery,
                  batch_size: int, max_row_payload_bytes: int) -> int:
    """Stream one node's whole table into its staging table.

    All per-row validation (identity/foreign-key storage types, sort-column
    type homogeneity, scalar value support, payload cap) happens here, long
    before any output byte is written.
    """
    alias_keys = tuple(
        json.dumps(alias, ensure_ascii=False) + ":"
        for _, alias, _ in query.scalar_pos)
    sort_types: list[str | None] = [None] * len(query.sort_pos)
    insert_sql = (
        f"INSERT INTO {staging} VALUES ("
        + ", ".join("?" for _ in range(
            1 + len(query.id_pos) + len(query.fk_pos) + len(query.sort_pos)))
        + ")")

    count = 0
    cursor = src.execute(query.sql)
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        batch: list[tuple] = []
        for row in rows:
            raw_identity = tuple(row[p] for p in query.id_pos)
            identity: list[Any] = []
            for column, value in zip(node.identity, raw_identity):
                _check_key_component(value, node, column, "identity",
                                     raw_identity)
                identity.append(value)
            identity_t = tuple(identity)

            foreign: list[Any] = []
            for column, value in zip(node.foreign_key,
                                     (row[p] for p in query.fk_pos)):
                _check_key_component(value, node, column, "foreign-key",
                                     identity_t)
                foreign.append(value)

            sort_values: list[Any] = []
            for index, (column, value) in enumerate(
                    zip(node.order_by, (row[p] for p in query.sort_pos))):
                if value is None:
                    sort_values.append(None)
                    continue
                if type(value) not in (int, str):
                    raise AssemblyDataError(
                        f"node {node.name!r}: sort column {column!r} has "
                        f"unsupported storage type {_storage_name(value)} "
                        f"(row identity {_fmt_identity(identity_t)}); sort "
                        "components only allow INTEGER, TEXT or NULL")
                kind = _storage_name(value)
                if sort_types[index] is None:
                    sort_types[index] = kind
                elif sort_types[index] != kind:
                    raise AssemblyDataError(
                        f"node {node.name!r}: sort column {column!r} mixes "
                        f"{sort_types[index]} and {kind} non-null values "
                        f"(row identity {_fmt_identity(identity_t)}); "
                        "non-null values of one sort column must share a "
                        "single storage type")
                sort_values.append(value)

            fragment = ",".join(
                key + _encode_scalar(row[p], node, column, identity_t)
                for key, (p, _, column) in zip(alias_keys, query.scalar_pos))
            payload = fragment.encode("utf-8")
            if len(payload) > max_row_payload_bytes:
                raise AssemblyDataError(
                    f"node {node.name!r}: projected payload of row identity "
                    f"{_fmt_identity(identity_t)} is {len(payload)} bytes, "
                    f"exceeding the single-row limit of "
                    f"{max_row_payload_bytes} bytes")
            batch.append((payload, *identity, *foreign, *sort_values))
        tmp.executemany(insert_sql, batch)
        count += len(batch)
    cursor.close()
    return count


def _finish_node(tmp: sqlite3.Connection, plan: AssemblyPlan,
                 node: NodeSpec, index: int, staging: str,
                 access: str, parent_access: str | None) -> None:
    """Deduplicate, de-orphan and materialise one node's access table."""
    id_cols = [f"id{i}" for i in range(len(node.identity))]
    fk_cols = [f"fk{i}" for i in range(len(node.foreign_key))]
    sort_cols = [f"o{i}" for i in range(len(node.order_by))]
    q_id = ", ".join(_quote_ident(c) for c in id_cols)

    duplicate = tmp.execute(
        f"SELECT {q_id}, COUNT(*) AS n FROM {staging} "
        f"GROUP BY {q_id} HAVING n > 1 LIMIT 1").fetchone()
    if duplicate is not None:
        raise AssemblyDataError(
            f"node {node.name!r}: duplicate row identity "
            f"{_fmt_identity(duplicate[:len(id_cols)])}; identity must be "
            f"unique across the whole table {node.table!r}")

    if parent_access is not None:
        parent_node = plan.by_name[node.parent]  # type: ignore[index]
        join = " AND ".join(
            f"p.{_quote_ident(f'id{i}')} = s.{_quote_ident(f'fk{i}')}"
            for i in range(len(node.foreign_key)))
        orphan = tmp.execute(
            f"SELECT {', '.join('s.' + _quote_ident(c) for c in id_cols)}, "
            f"{', '.join('s.' + _quote_ident(c) for c in fk_cols)} "
            f"FROM {staging} s LEFT JOIN {parent_access} p ON {join} "
            f"WHERE p.{_quote_ident('id0')} IS NULL LIMIT 1").fetchone()
        if orphan is not None:
            half = len(id_cols)
            raise AssemblyDataError(
                f"node {node.name!r}: orphan row identity "
                f"{_fmt_identity(orphan[:half])}; foreign key "
                f"{_fmt_identity(orphan[half:])} has no matching parent "
                f"row in node {parent_node.name!r} (components compare by "
                "value and storage type)")
        tmp.execute(
            f"INSERT INTO {access} (parent_seq, payload, {q_id}) "
            f"SELECT p.rowid, s.payload, "
            f"{', '.join('s.' + _quote_ident(c) for c in id_cols)} "
            f"FROM {staging} s JOIN {parent_access} p ON {join} "
            f"ORDER BY p.rowid"
            + "".join(f", s.{_quote_ident(c)}" for c in sort_cols)
            + "".join(f", s.{_quote_ident(c)}" for c in id_cols))
    else:
        tmp.execute(
            f"INSERT INTO {access} (payload, {q_id}) "
            f"SELECT payload, {q_id} FROM {staging} "
            f"ORDER BY "
            + ", ".join(
                [_quote_ident(c) for c in sort_cols + id_cols] or ["rowid"]))

    if plan.children[node.name]:
        tmp.execute(
            f"CREATE UNIQUE INDEX {_quote_ident(f'x{index}')} ON {access} "
            f"({q_id})")
    tmp.execute(f"DROP TABLE {staging}")


def _emit(ctx: dict[int, dict[str, Any]], node_index: int, seq: int,
          payload: bytes, write: Callable[[bytes], Any]) -> None:
    write(b"{")
    wrote = False
    if payload:
        write(payload)
        wrote = True
    for child_index, array_key in ctx[node_index]["children"]:
        if wrote:
            write(b",")
        write(array_key)
        write(b"[")
        cursor: _PeekCursor = ctx[child_index]["cursor"]
        first = True
        while True:
            row = cursor.peek()
            if row is None or row[1] != seq:
                break
            cursor.pop()
            if not first:
                write(b",")
            _emit(ctx, child_index, row[0], row[2], write)
            first = False
        write(b"]")
        wrote = True
    write(b"}")


def assemble_nested(
    plan: AssemblyPlan | dict[str, Any] | str | Path,
    database: str | Path,
    output: str | Path,
    *,
    temp_dir: str | Path | None = None,
    cache_size_kb: int = DEFAULT_CACHE_SIZE_KB,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_row_payload_bytes: int = DEFAULT_MAX_ROW_PAYLOAD_BYTES,
    write_buffer_bytes: int = DEFAULT_WRITE_BUFFER_BYTES,
    progress: ProgressCallback | None = None,
) -> AssemblyResult:
    """Assemble a nested JSONL delivery file from an existing database.

    ``plan`` may be an :class:`AssemblyPlan`, a decoded JSON dict or a path
    to a JSON plan file.  ``database`` is opened read-only and is never
    modified; ``output`` must not exist yet and is published atomically
    (a sibling temporary file is renamed only after a successful write, so
    a failure never leaves a final JSONL behind).  Scratch structures live
    in a private directory inside ``temp_dir`` (default: the output
    directory) and only files created by this run are removed afterwards.
    ``progress(stage, node, count)`` is invoked after each node's
    extraction (``"extracted"``), access-table build (``"sorted"``) and for
    each written root (``"root"``).
    """
    started = time.monotonic()
    parsed = _coerce_plan(plan)
    if not isinstance(cache_size_kb, int) or cache_size_kb < 256:
        raise ValueError("cache_size_kb must be an integer >= 256")
    if not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if not isinstance(max_row_payload_bytes, int) or max_row_payload_bytes < 1:
        raise ValueError("max_row_payload_bytes must be a positive integer")
    if not isinstance(write_buffer_bytes, int) or write_buffer_bytes < 1024:
        raise ValueError("write_buffer_bytes must be an integer >= 1024")

    database = os.path.abspath(os.fspath(database))
    output = os.path.abspath(os.fspath(output))
    if not os.path.isfile(database):
        raise FileNotFoundError(
            f"source database {database!r} does not exist")
    if os.path.realpath(output) == os.path.realpath(database):
        raise ValueError(
            "output path resolves to the source database itself")
    if os.path.lexists(output):
        raise FileExistsError(
            f"refusing to overwrite existing output {output!r}")

    out_parent = os.path.dirname(output) or "."
    os.makedirs(out_parent, exist_ok=True)
    temp_parent = (os.path.abspath(os.fspath(temp_dir))
                   if temp_dir is not None else out_parent)
    os.makedirs(temp_parent, exist_ok=True)

    work = tempfile.mkdtemp(prefix=".assemble-", dir=temp_parent)
    tmp_out: str | None = None
    tmp: sqlite3.Connection | None = None
    out: Any = None
    try:
        fd, tmp_out = tempfile.mkstemp(
            prefix=".assemble-", suffix=".jsonl", dir=out_parent)
        try:
            out = os.fdopen(fd, "wb", buffering=write_buffer_bytes)
        except BaseException:
            os.close(fd)
            raise

        tmp = sqlite3.connect(os.path.join(work, "scratch.sqlite"))
        tmp.execute("PRAGMA journal_mode = OFF")
        tmp.execute("PRAGMA synchronous = OFF")
        tmp.execute(f"PRAGMA cache_size = -{cache_size_kb}")
        tmp.execute("PRAGMA temp_store = FILE")
        # SQLite's sorter/INDEX staging must stay inside the caller's
        # temporary directory, never in a system default location.
        tmp.execute(
            "PRAGMA temp_store_directory = '"
            + work.replace("'", "''") + "'")

        node_rows: dict[str, int] = {}
        queries = {node.name: _NodeQuery(node) for node in parsed.nodes}
        access = {name: _quote_ident(f"n{i}")
                  for i, name in enumerate(parsed.topo)}
        staging = {name: _quote_ident(f"s{i}")
                   for i, name in enumerate(parsed.topo)}
        for i, name in enumerate(parsed.topo):
            node = parsed.by_name[name]
            cols = (["payload BLOB NOT NULL"]
                    + [f"id{j} BLOB" for j in range(len(node.identity))]
                    + [f"fk{j} BLOB" for j in range(len(node.foreign_key))]
                    + [f"o{j} BLOB" for j in range(len(node.order_by))])
            tmp.execute(f"CREATE TABLE {staging[name]} ({', '.join(cols)})")
            cols_a = (["parent_seq INTEGER", "payload BLOB NOT NULL"]
                      + [f"id{j} BLOB" for j in range(len(node.identity))])
            tmp.execute(f"CREATE TABLE {access[name]} ({', '.join(cols_a)})")

        uri = "file:" + quote(database) + "?mode=ro"
        src = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            src.execute("PRAGMA query_only = ON")
            src.execute("BEGIN")
            _check_schema(src, parsed)
            for name in parsed.topo:
                node = parsed.by_name[name]
                node_rows[name] = _extract_node(
                    src, tmp, node, staging[name], queries[name],
                    batch_size, max_row_payload_bytes)
                if progress is not None:
                    progress("extracted", name, node_rows[name])
        finally:
            # The consistent read view ends here; the source connection is
            # released before the (potentially long) emission phase.
            if src.in_transaction:
                src.execute("ROLLBACK")
            src.close()

        for i, name in enumerate(parsed.topo):
            node = parsed.by_name[name]
            parent_access = (None if node.is_root
                             else access[node.parent])  # type: ignore[index]
            _finish_node(tmp, parsed, node, i, staging[name], access[name],
                         parent_access)
            if progress is not None:
                progress("sorted", name, node_rows[name])

        ctx: dict[int, dict[str, Any]] = {}
        for i, name in enumerate(parsed.topo):
            node = parsed.by_name[name]
            cursor = _PeekCursor(
                tmp.execute(
                    f"SELECT rowid, parent_seq, payload FROM {access[name]} "
                    "ORDER BY rowid"),
                batch_size)
            child_entries = []
            for child_name in parsed.children[name]:
                child_index = parsed.topo.index(child_name)
                array = parsed.by_name[child_name].array
                assert array is not None
                child_entries.append((
                    child_index,
                    json.dumps(array, ensure_ascii=False).encode("utf-8")
                    + b":"))
            ctx[i] = {"cursor": cursor, "children": child_entries}

        roots = 0
        output_bytes = 0
        write = out.write
        root_index = parsed.topo.index(parsed.root)
        root_cursor: _PeekCursor = ctx[root_index]["cursor"]
        while True:
            row = root_cursor.pop()
            if row is None:
                break
            before = out.tell()
            _emit(ctx, root_index, row[0], row[2], write)
            write(b"\n")
            output_bytes += out.tell() - before
            roots += 1
            if progress is not None:
                progress("root", parsed.root, roots)
        out.flush()
        os.fsync(out.fileno())
        out.close()
        out = None
        tmp.close()
        tmp = None
        os.replace(tmp_out, output)
        tmp_out = None
        return AssemblyResult(
            database_path=database,
            output_path=output,
            roots=roots,
            node_rows=node_rows,
            output_bytes=output_bytes,
            elapsed_seconds=time.monotonic() - started,
        )
    finally:
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
        if tmp is not None:
            tmp.close()
        if tmp_out is not None:
            try:
                os.unlink(tmp_out)
            except FileNotFoundError:
                pass
        shutil.rmtree(work, ignore_errors=True)
