"""Epochs, run-level snapshots of opted-in tables: the registry of the tables, the snapshot and the catalog.

Input: a DB-API 2.0 connection (autocommit off), the names of a schema, a table and optionally its key columns,
and the name of an epoch. Output: `TrackedTable` values read from and written to `caverns.epoch_table`, the
registry created by Caverns' own step `epochs`; `Epoch` values, written to and read from the other epoch tables.
The module imports no database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from caverns_of_time import __version__
from caverns_of_time._connection import (
    Connection,
    Cursor,
    check_connection,
    lock,
    pin_search_path,
    read_only,
    session_lock,
    transaction,
    validate_limit,
    validate_name,
)
from caverns_of_time.errors import ChangesetError, EpochNotFound, EpochRefused, LedgerNotInstalled
from caverns_of_time.nozdormu import EPOCHS_EXISTS_SQL

__all__ = [
    "Column",
    "Epoch",
    "EpochTable",
    "TableAt",
    "TrackedTable",
    "as_of",
    "catalog",
    "register",
    "snapshot",
    "tracked",
    "unregister",
]

_log = logging.getLogger(__name__)

# NOTE: hard rule 3 forbids composing a statement, so `caverns.epoch_table` and its column list are spelled
# literally in each `*_SQL` constant here; every name a consumer supplies is a bound parameter.
_TABLE_EXISTS_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p'))"
)
# The primary key's columns in index order (`indkey` is a zero-based int2vector; the ordinality keeps its order).
_PRIMARY_KEY_SQL = """
SELECT a.attname::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k (attnum, position)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
ORDER BY k.position
"""
# The live columns in column order with their types; `format_type` prints names relative to the search path,
# so a snapshot pins it first (`pin_search_path`).
_LIVE_COLUMNS_SQL = """
SELECT a.attname::text, format_type(a.atttypid, a.atttypmod)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
_SELECT_REGISTERED_SQL = (
    "SELECT schema_name, table_name, key, registered_at, registered_by FROM caverns.epoch_table"
    " WHERE schema_name = %s AND table_name = %s"
)
_SELECT_TRACKED_SQL = (
    "SELECT schema_name, table_name, key, registered_at, registered_by FROM caverns.epoch_table"
    " ORDER BY schema_name, table_name"
)
_UPSERT_SQL = (
    "INSERT INTO caverns.epoch_table (schema_name, table_name, key) VALUES (%s, %s, %s::text[])"
    " ON CONFLICT (schema_name, table_name) DO UPDATE SET key = EXCLUDED.key,"
    " registered_at = clock_timestamp(), registered_by = current_user"
    " RETURNING schema_name, table_name, key, registered_at, registered_by"
)
_DELETE_REGISTERED_SQL = "DELETE FROM caverns.epoch_table WHERE schema_name = %s AND table_name = %s"
_EPOCH_NAME_SQL = "SELECT epoch FROM caverns.epoch WHERE name = %s"
_INSERT_EPOCH_SQL = (
    "INSERT INTO caverns.epoch (name, ledger_version, caverns_version) SELECT %s, max(version), %s FROM caverns.ledger"
    " RETURNING epoch, name, ledger_version, taken_at, taken_by, caverns_version"
)
# NOTE: hard rule 3 forbids composing a statement in Python, so PostgreSQL composes the copy: this template is
# bound as a value to `format`, which quotes the identifiers (%I) and the literals (%L). The source rows are read
# once (MATERIALIZED) for both the copy and the counts; `ON CONFLICT DO NOTHING` drops a repeated key, which the
# counts then reveal. The key is a jsonb object of the key columns' values; a JSON null among them is counted.
# `t.*` is the whole row even when the table has a column named `t` (a bare `t` would be that column).
_COPY_TEMPLATE = """
WITH source AS MATERIALIZED (SELECT to_jsonb(t.*) AS row FROM %I.%I t),
inserted AS (
    INSERT INTO caverns.epoch_row (epoch, schema_name, table_name, key, row)
    SELECT %s, %L, %L, (SELECT jsonb_object_agg(k, s.row -> k) FROM unnest(%L::text[]) AS k), s.row FROM source s
    ON CONFLICT DO NOTHING
    RETURNING key
)
SELECT (SELECT count(*) FROM source), (SELECT count(*) FROM inserted),
       (SELECT count(*) FROM inserted WHERE jsonb_path_exists(key, '$.* ? (@ == null)'))
"""
_COMPOSE_COPY_SQL = "SELECT format(%s::text, %s::text, %s::text, %s::bigint, %s::text, %s::text, %s::text[])"
_INSERT_SOURCE_SQL = (
    "INSERT INTO caverns.epoch_source (epoch, schema_name, table_name, key, columns, row_count)"
    " VALUES (%s, %s, %s, %s::text[], %s::jsonb, %s)"
)
_SELECT_EPOCHS_SQL = """
SELECT e.epoch, e.name, e.ledger_version, e.taken_at, e.taken_by, e.caverns_version, d.deleted_at
FROM caverns.epoch e
LEFT JOIN caverns.epoch_deletion d ON d.epoch = e.epoch
ORDER BY e.epoch DESC
LIMIT %s
"""
_SELECT_EPOCH_SQL = """
SELECT e.epoch, e.name, e.ledger_version, e.taken_at, e.taken_by, e.caverns_version, d.deleted_at
FROM caverns.epoch e
LEFT JOIN caverns.epoch_deletion d ON d.epoch = e.epoch
WHERE e.name = %s
"""
# The rows of one table of an epoch, ordered by its key columns in declared order: the key's values are collected
# into a jsonb[] per row, which compares element by element (a number as a number, a string as a string).
_SELECT_AT_SQL = """
SELECT r.row FROM caverns.epoch_row r
WHERE r.epoch = %s AND r.schema_name = %s AND r.table_name = %s
ORDER BY (SELECT array_agg(r.row -> k ORDER BY o) FROM unnest(%s::text[]) WITH ORDINALITY AS u (k, o))
LIMIT %s
"""
_SELECT_SOURCES_SQL = (
    "SELECT epoch, schema_name, table_name, key, columns, row_count FROM caverns.epoch_source"
    " WHERE epoch = ANY(%s::bigint[]) ORDER BY epoch, schema_name, table_name"
)


@dataclass(frozen=True)
class TrackedTable:
    """One registered table: its exact schema and table names, its key columns in order, who registered it and when."""

    schema: str
    table: str
    key: tuple[str, ...]
    registered_at: datetime
    registered_by: str


@dataclass(frozen=True)
class Column:
    """One column of a snapshotted table: its name and its type as `format_type` prints it, schema-qualified."""

    name: str
    type: str


@dataclass(frozen=True)
class EpochTable:
    """One table of an epoch: its names, the key columns, its columns in column order and the rows copied."""

    schema: str
    table: str
    key: tuple[str, ...]
    columns: tuple[Column, ...]
    row_count: int


@dataclass(frozen=True)
class Epoch:
    """One epoch: its number and name, the ledger version it was taken at, who took it and when, its tables.

    `deleted_at` is the time of the epoch's deletion, None while its rows are kept.
    """

    epoch: int
    name: str
    ledger_version: int
    taken_at: datetime
    taken_by: str
    caverns_version: str
    deleted_at: datetime | None
    tables: tuple[EpochTable, ...]


@dataclass(frozen=True)
class TableAt:
    """One table as it was at an epoch: the epoch, the table's entry in it and the rows read, ordered by key.

    Each row is a dict keyed by the epoch's column names in column order; a value is as JSON holds it (a number,
    a string, a boolean, None, a list or a dict), its SQL type being the matching `Column.type` of the table.
    """

    epoch: Epoch
    table: EpochTable
    rows: tuple[dict[str, Any], ...]


def _tracked_table(row: Sequence[Any]) -> TrackedTable:
    schema, table, key, registered_at, registered_by = row
    return TrackedTable(schema, table, tuple(key), registered_at, registered_by)


def _validate_table(schema: object, table: object) -> None:
    for kind, value in (("schema", schema), ("table", table)):
        if not isinstance(value, str):
            raise ChangesetError(f"{kind} {value!r} is not a string")
        if not value:
            raise ChangesetError(f"{kind} is empty")


def _validate_key(key: Sequence[str] | None) -> list[str] | None:
    """The key columns as a list, or None for the primary key; refuses a bare string, a non-name, none or a repeat."""
    if key is None:
        return None
    if isinstance(key, str):
        raise ChangesetError(f"key must be a sequence of column names, not the string {key!r}")
    try:
        columns = list(key)
    except TypeError:
        raise ChangesetError(f"key must be a sequence of column names, not {key!r}") from None
    for column in columns:
        if not isinstance(column, str):
            raise ChangesetError(f"key column {column!r} is not a string")
    if not columns:
        raise ChangesetError("key is empty")
    duplicates = sorted({column for column in columns if columns.count(column) > 1})
    if duplicates:
        raise ChangesetError(f"key repeats the column {duplicates[0]!r}")
    return columns


def _require_installed(cursor: Cursor) -> None:
    cursor.execute(EPOCHS_EXISTS_SQL)
    row = cursor.fetchone()
    if row is None or not row[0]:
        raise LedgerNotInstalled(
            "the epoch tables are not installed (Caverns' own step epochs is not applied): call install()"
        )


def _column_names(cursor: Cursor, sql: str, schema: str, table: str) -> list[str]:
    cursor.execute(sql, (schema, table))
    return [row[0] for row in cursor.fetchall()]


def _live_columns(cursor: Cursor, schema: str, table: str) -> list[Column]:
    """The table's live columns in column order; empty when the table does not exist or is not a table."""
    cursor.execute(_LIVE_COLUMNS_SQL, (schema, table))
    return [Column(name, type_) for name, type_ in cursor.fetchall()]


def _check_key(schema: str, table: str, key: Sequence[str], live: Sequence[Column]) -> None:
    names = {column.name for column in live}
    missing = next((column for column in key if column not in names), None)
    if missing is not None:
        raise EpochRefused(f"{schema}.{table} has no column {missing!r} for the key")


def _key_columns(cursor: Cursor, schema: str, table: str, key: list[str] | None) -> list[str]:
    """The columns the registry stores for the table: *key* checked against its live columns, else its primary key.

    Raises `EpochRefused` for a table that is missing or not a table, a key column the table lacks, or no key given
    and no primary key.
    """
    cursor.execute(_TABLE_EXISTS_SQL, (schema, table))
    row = cursor.fetchone()
    if row is None or not row[0]:
        raise EpochRefused(f"{schema}.{table} does not exist or is not a table")
    if key is None:
        primary = _column_names(cursor, _PRIMARY_KEY_SQL, schema, table)
        if not primary:
            raise EpochRefused(f"{schema}.{table} has no primary key: pass key=")
        return primary
    _check_key(schema, table, key, _live_columns(cursor, schema, table))
    return key


def register(connection: Connection, schema: str, table: str, *, key: Sequence[str] | None = None) -> TrackedTable:
    """Opt *table* of *schema* in to epochs, keyed by *key* (default: its primary key); the row as stored.

    The names are exact PostgreSQL names, case-sensitive. The same key again writes nothing; another key
    replaces the stored one and logs a warning. Raises `ChangesetError` for a bad *schema*, *table* or *key*,
    `LedgerNotInstalled` before `install()` and `EpochRefused` for a table that does not exist or is not a table,
    a key column it lacks, or no *key* on a table without a primary key.
    """
    check_connection(connection)
    _validate_table(schema, table)
    columns = _validate_key(key)
    with transaction(connection) as cursor:
        lock(cursor)
        _require_installed(cursor)
        columns = _key_columns(cursor, schema, table, columns)
        cursor.execute(_SELECT_REGISTERED_SQL, (schema, table))
        found = cursor.fetchone()
        if found is not None and list(found[2]) == columns:
            return _tracked_table(found)
        if found is not None:
            _log.warning(
                "the key of %s.%s changes from (%s) to (%s)", schema, table, ", ".join(found[2]), ", ".join(columns)
            )
        cursor.execute(_UPSERT_SQL, (schema, table, columns))
        stored = cursor.fetchone()
    assert stored is not None  # an INSERT ... RETURNING always returns its row
    _log.info("registered %s.%s with key (%s)", schema, table, ", ".join(columns))
    return _tracked_table(stored)


def unregister(connection: Connection, schema: str, table: str) -> None:
    """Opt *table* of *schema* out of later epochs; the epochs already taken keep its rows.

    Raises `ChangesetError` for a bad *schema* or *table*, `LedgerNotInstalled` before `install()` and
    `EpochRefused` for a table that is not registered.
    """
    check_connection(connection)
    _validate_table(schema, table)
    with transaction(connection) as cursor:
        lock(cursor)
        _require_installed(cursor)
        cursor.execute(_SELECT_REGISTERED_SQL, (schema, table))
        if cursor.fetchone() is None:
            raise EpochRefused(f"{schema}.{table} is not registered")
        cursor.execute(_DELETE_REGISTERED_SQL, (schema, table))
    _log.info("unregistered %s.%s", schema, table)


def tracked(connection: Connection) -> list[TrackedTable]:
    """The registered tables, ordered by schema and table; writes nothing.

    Raises `LedgerNotInstalled` before `install()`.
    """
    check_connection(connection)
    with read_only(connection) as cursor:
        _require_installed(cursor)
        cursor.execute(_SELECT_TRACKED_SQL)
        return [_tracked_table(row) for row in cursor.fetchall()]


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value  # a driver that leaves jsonb as text


def _epoch_table(row: Sequence[Any]) -> EpochTable:
    _, schema, table, key, columns, row_count = row
    return EpochTable(schema, table, tuple(key), tuple(Column(c["name"], c["type"]) for c in _json(columns)), row_count)


def _read_epochs(cursor: Cursor, rows: Sequence[Sequence[Any]]) -> list[Epoch]:
    """The `Epoch` of each catalog row (epoch, name, ledger_version, taken_at, taken_by, caverns_version, deleted_at).

    Loads the tables of all the epochs in one query; the order of *rows* is kept.
    """
    if not rows:
        return []
    cursor.execute(_SELECT_SOURCES_SQL, ([row[0] for row in rows],))
    tables: dict[int, list[EpochTable]] = {}
    for source in cursor.fetchall():
        tables.setdefault(source[0], []).append(_epoch_table(source))
    return [
        Epoch(epoch, name, version, taken_at, taken_by, caverns_version, deleted_at, tuple(tables.get(epoch, ())))
        for epoch, name, version, taken_at, taken_by, caverns_version, deleted_at in rows
    ]


def _copy_rows(cursor: Cursor, epoch: int, tracked_table: TrackedTable) -> int:
    """Copy the table's rows into `caverns.epoch_row` under *epoch*; the number of rows, or `EpochRefused`."""
    schema, table, key = tracked_table.schema, tracked_table.table, list(tracked_table.key)
    cursor.execute(_COMPOSE_COPY_SQL, (_COPY_TEMPLATE, schema, table, epoch, schema, table, key))
    composed = cursor.fetchone()
    assert composed is not None  # a SELECT without FROM returns its row
    cursor.execute(composed[0])
    counts = cursor.fetchone()
    assert counts is not None  # an aggregate returns its row
    read, inserted, nulls = counts
    columns = ", ".join(key)
    if inserted < read:
        raise EpochRefused(f"the key ({columns}) of {schema}.{table} is not unique")
    if nulls:
        raise EpochRefused(f"the key ({columns}) of {schema}.{table} has nulls")
    return int(read)


def snapshot(connection: Connection, name: str) -> Epoch:
    """Copy every registered table into a new epoch called *name*, tagged with the latest ledger version.

    The tables are read in one REPEATABLE READ transaction that starts after Caverns' advisory lock is held, so
    the copy is one consistent view; a refusal writes nothing. Raises `ChangesetError` for a bad *name*,
    `LedgerNotInstalled` before `install()` and `EpochRefused` for a name that is taken (a deleted epoch's too),
    no registered table, a registered table or key column that no longer exists, and a key with duplicates or nulls.
    """
    check_connection(connection)
    validate_name("epoch name", name)
    with session_lock(connection), transaction(connection, repeatable_read=True) as cursor:
        _require_installed(cursor)
        cursor.execute(_EPOCH_NAME_SQL, (name,))
        taken = cursor.fetchone()
        if taken is not None:
            raise EpochRefused(f"the epoch name {name!r} is taken (epoch {taken[0]})")
        cursor.execute(_SELECT_TRACKED_SQL)
        registered = [_tracked_table(row) for row in cursor.fetchall()]
        if not registered:
            raise EpochRefused("no table is registered: call register() first")
        pin_search_path(cursor)
        cursor.execute(_INSERT_EPOCH_SQL, (name, __version__))
        created = cursor.fetchone()
        assert created is not None  # an INSERT ... RETURNING always returns its row
        for tracked_table in registered:
            schema, table, key = tracked_table.schema, tracked_table.table, tracked_table.key
            live = _live_columns(cursor, schema, table)
            if not live:
                raise EpochRefused(f"{schema}.{table} is registered but does not exist")
            _check_key(schema, table, key, live)
            row_count = _copy_rows(cursor, created[0], tracked_table)
            columns = json.dumps([{"name": c.name, "type": c.type} for c in live])
            cursor.execute(_INSERT_SOURCE_SQL, (created[0], schema, table, list(key), columns, row_count))
        (taken_epoch,) = _read_epochs(cursor, [(*created, None)])
    rows = sum(table.row_count for table in taken_epoch.tables)
    _log.info(
        "took epoch %s at ledger version %s: %s rows in %s tables",
        name,
        taken_epoch.ledger_version,
        rows,
        len(taken_epoch.tables),
    )
    return taken_epoch


def catalog(connection: Connection, *, limit: int | None = None) -> list[Epoch]:
    """The epochs, newest first, deleted ones included, at most *limit* (None: all); writes nothing.

    Raises `ChangesetError` for a *limit* that is not a positive integer up to 9223372036854775807 and
    `LedgerNotInstalled` before `install()`.
    """
    check_connection(connection)
    validate_limit(limit)
    with read_only(connection) as cursor:
        _require_installed(cursor)
        cursor.execute(_SELECT_EPOCHS_SQL, (limit,))
        return _read_epochs(cursor, cursor.fetchall())


def _find_epoch(cursor: Cursor, name: str) -> Epoch:
    """The epoch called *name* with its tables; `EpochNotFound` if unknown, `EpochRefused` if deleted."""
    cursor.execute(_SELECT_EPOCH_SQL, (name,))
    row = cursor.fetchone()
    if row is None:
        raise EpochNotFound(f"no epoch is named {name!r}")
    (found,) = _read_epochs(cursor, [row])
    if found.deleted_at is not None:
        deleted = found.deleted_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        raise EpochRefused(f"epoch {name!r} was deleted at {deleted} UTC")
    return found


def as_of(connection: Connection, epoch: str, schema: str, table: str, *, limit: int | None = None) -> TableAt:
    """The table *schema*.*table* as it was at the epoch named *epoch*, rows ordered by key, at most *limit*.

    The columns are the epoch's own, not the live table's. Runs in one read-only transaction and writes nothing.
    Raises `ChangesetError` for a bad argument (a *limit* that is not a positive integer up to
    9223372036854775807 included), `LedgerNotInstalled` before `install()`, `EpochNotFound` for an unknown epoch or
    an epoch that lacks the table and `EpochRefused` for a deleted epoch.
    """
    check_connection(connection)
    validate_name("epoch name", epoch)
    _validate_table(schema, table)
    validate_limit(limit)
    with read_only(connection) as cursor:
        _require_installed(cursor)
        found = _find_epoch(cursor, epoch)
        entry = next((t for t in found.tables if (t.schema, t.table) == (schema, table)), None)
        if entry is None:
            raise EpochNotFound(f"epoch {epoch!r} has no table {schema}.{table}")
        cursor.execute(_SELECT_AT_SQL, (found.epoch, schema, table, list(entry.key), limit))
        stored = [_json(row[0]) for row in cursor.fetchall()]
    rows = tuple({c.name: values.get(c.name) for c in entry.columns} for values in stored)
    return TableAt(found, entry, rows)
