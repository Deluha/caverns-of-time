"""Epochs, run-level snapshots of opted-in tables: the registry, the snapshot, the catalog, as_of, diff and delete.

Input: a DB-API 2.0 connection (autocommit off), the names of a schema, a table and optionally its key columns,
and the name of an epoch. Output: `TrackedTable` values read from and written to `caverns.epoch_table`, the
registry created by Caverns' own step `epochs`; `Epoch` values, written to and read from the other epoch tables.
The module imports no database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from caverns_of_time import __version__, _connection
from caverns_of_time._connection import (
    DEFAULT_LOCK_TIMEOUT,
    EPOCHS_EXISTS_SQL,
    Connection,
    Cursor,
    check_connection,
    lock,
    pin_output_settings,
    pin_search_path,
    read_only,
    session_lock,
    set_lock_timeout,
    transaction,
    validate_limit,
    validate_lock_timeout,
    validate_name,
)
from caverns_of_time.errors import ChangesetError, EpochNotFound, EpochRefused, LedgerNotInstalled

__all__ = [
    "Column",
    "ColumnChange",
    "Epoch",
    "EpochDiff",
    "EpochTable",
    "RowChange",
    "TableAt",
    "TableDiff",
    "TrackedTable",
    "as_of",
    "catalog",
    "delete",
    "diff",
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
# The primary key's columns in index order (`indkey` is a zero-based int2vector; the ordinality keeps its order),
# without the `INCLUDE` columns, which `indkey` lists after the key columns (`indnkeyatts` counts the key ones).
_PRIMARY_KEY_SQL = """
SELECT a.attname::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k (attnum, position)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p') AND k.position <= i.indnkeyatts
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
# bound as a value to `format`, which quotes the identifiers (%I) and the literals (%L); its arguments are, in
# order: 1 schema, 2 table, 3 epoch, 4 schema, 5 table, 6 key columns. The source rows are read once (MATERIALIZED)
# for both the copy and the counts, each with its key (a jsonb object of the key columns' values) computed once;
# `ON CONFLICT DO NOTHING` drops a repeated key, which the counts then reveal. A key with a JSON null value is
# counted over the source, so a repeated null key is a null key, not a duplicate; `e.value = 'null'::jsonb` is
# an exact test (an array holding a null is not a null). `t.*` is the whole row even when the table has a
# column named `t` (a bare `t` would be that column).
_COPY_TEMPLATE = """
WITH source AS MATERIALIZED (
    SELECT r.row, (SELECT jsonb_object_agg(k, r.row -> k) FROM unnest(%6$L::text[]) AS k) AS key
    FROM %1$I.%2$I t CROSS JOIN LATERAL (SELECT to_jsonb(t.*) AS row) r
),
inserted AS (
    INSERT INTO caverns.epoch_row (epoch, schema_name, table_name, key, row)
    SELECT %3$s, %4$L, %5$L, s.key, s.row FROM source s
    ON CONFLICT DO NOTHING
    RETURNING 1
)
SELECT (SELECT count(*) FROM source), (SELECT count(*) FROM inserted),
       (SELECT count(*) FROM source s WHERE EXISTS (SELECT 1 FROM jsonb_each(s.key) e WHERE e.value = 'null'::jsonb))
"""
_COMPOSE_COPY_SQL = "SELECT format(%s::text, %s::text, %s::text, %s::bigint, %s::text, %s::text, %s::text[])"
_INSERT_SOURCE_SQL = (
    "INSERT INTO caverns.epoch_source (epoch, schema_name, table_name, key, columns, row_count)"
    " VALUES (%s, %s, %s, %s::text[], %s::jsonb, %s)"
)
# The select list and the deletion join every epoch read shares; each statement adds its own tail below.
_SELECT_EPOCH_PREFIX = """
SELECT e.epoch, e.name, e.ledger_version, e.taken_at, e.taken_by, e.caverns_version, d.deleted_at
FROM caverns.epoch e
LEFT JOIN caverns.epoch_deletion d ON d.epoch = e.epoch
"""
_SELECT_EPOCHS_SQL = _SELECT_EPOCH_PREFIX + "ORDER BY e.epoch DESC\nLIMIT %s\n"
_SELECT_EPOCHS_BY_ID_SQL = _SELECT_EPOCH_PREFIX + "WHERE e.epoch = ANY(%s::bigint[])\nORDER BY e.epoch DESC\n"
_SELECT_EPOCH_SQL = _SELECT_EPOCH_PREFIX + "WHERE e.name = %s\n"
# The rows of one table of an epoch, ordered by its key columns in declared order: the key's values are collected
# into a jsonb[] per row, which compares element by element (a number as a number, a string as a string).
_SELECT_AT_SQL = """
SELECT r.row FROM caverns.epoch_row r
WHERE r.epoch = %s AND r.schema_name = %s AND r.table_name = %s
ORDER BY (SELECT array_agg(r.row -> k ORDER BY o) FROM unnest(%s::text[]) WITH ORDINALITY AS u (k, o))
LIMIT %s
"""
# The rows of one table that differ between two epochs, compared on the columns both have: each epoch's rows lose the
# columns only it has (`jsonb - text[]` removes keys), then the two are joined FULL on the key. A row only in the new
# epoch has no `o.key`, one only in the old has no `n.key`; the last column lists the common columns whose JSON values
# differ. Ordered like `_SELECT_AT_SQL` (spelled again: the statement cannot be composed, hard rule 3), over the key.
_SELECT_DIFF_SQL = """
WITH o AS (
    SELECT r.key, r.row - %s::text[] AS row FROM caverns.epoch_row r
    WHERE r.epoch = %s AND r.schema_name = %s AND r.table_name = %s
), n AS (
    SELECT r.key, r.row - %s::text[] AS row FROM caverns.epoch_row r
    WHERE r.epoch = %s AND r.schema_name = %s AND r.table_name = %s
)
SELECT o.key IS NULL, n.key IS NULL, coalesce(n.key, o.key), o.row, n.row,
       ARRAY(SELECT k FROM jsonb_object_keys(n.row) AS k WHERE o.row -> k IS DISTINCT FROM n.row -> k)
FROM o FULL JOIN n ON o.key = n.key
WHERE o.row IS DISTINCT FROM n.row
ORDER BY (
    SELECT array_agg(coalesce(n.key, o.key) -> k ORDER BY p) FROM unnest(%s::text[]) WITH ORDINALITY AS u (k, p)
)
"""
# The tombstone first: the trigger of `epoch_row` allows the DELETE only for an epoch that has one.
_INSERT_TOMBSTONE_SQL = "INSERT INTO caverns.epoch_deletion (epoch, caverns_version) VALUES (%s, %s)"
_DELETE_ROWS_SQL = "DELETE FROM caverns.epoch_row WHERE epoch = %s"
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


@dataclass(frozen=True)
class ColumnChange:
    """One column that differs between two epochs: added, removed or changed (its type), with the types as printed.

    `old_type` is None for an added column, `new_type` None for a removed one.
    """

    name: str
    change: str
    old_type: str | None
    new_type: str | None


@dataclass(frozen=True)
class RowChange:
    """One row in both epochs whose values differ: its key and the changed columns' values at each epoch.

    `old` and `new` hold the same columns, in column order, as JSON holds the values.
    """

    key: dict[str, Any]
    old: dict[str, Any]
    new: dict[str, Any]


@dataclass(frozen=True)
class TableDiff:
    """One table that differs between two epochs.

    `change` is "added" (only in the new epoch), "removed" (only in the old) or "changed" (in both): the first two
    list no rows. `key` is the table's key columns; `added` and `removed` are the keys of the rows only in the new
    and only in the old epoch, each a dict in declared key order; `changed` the rows in both whose values differ.
    """

    schema: str
    table: str
    change: str
    key: tuple[str, ...]
    columns: tuple[ColumnChange, ...]
    added: tuple[dict[str, Any], ...]
    removed: tuple[dict[str, Any], ...]
    changed: tuple[RowChange, ...]


@dataclass(frozen=True)
class EpochDiff:
    """The difference between two epochs: both epochs and the tables that differ, ordered by schema and table."""

    old: Epoch
    new: Epoch
    tables: tuple[TableDiff, ...]


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
        cursor.execute(_PRIMARY_KEY_SQL, (schema, table))
        primary = [found[0] for found in cursor.fetchall()]
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
    if nulls:  # first: a repeated null key is a null key, not a duplicate
        raise EpochRefused(f"the key ({columns}) of {schema}.{table} has nulls")
    if inserted < read:
        raise EpochRefused(f"the key ({columns}) of {schema}.{table} is not unique")
    return int(read)


def snapshot(connection: Connection, name: str, *, lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT) -> Epoch:
    """Copy every registered table into a new epoch called *name*, tagged with the latest ledger version.

    The tables are read in one REPEATABLE READ transaction that starts after Caverns' advisory lock is held, so
    the copy is one consistent view, its values printed under pinned settings (UTC, `postgres` interval style,
    `extra_float_digits` 1, hex bytea, `ISO, MDY` dates, `C` money) whatever the session's; a refusal writes
    nothing. Raises `ChangesetError` for a bad *name*, `LedgerNotInstalled` before `install()` and `EpochRefused`
    for a name that is taken (a deleted epoch's too), no registered table, a registered table or key column that no
    longer exists, and a key with duplicates or nulls.

    *lock_timeout* is the seconds the call waits for Caverns' advisory lock (a timeout raises `CavernsError`) and,
    in the copying transaction, for the lock on each source table (a timeout there raises the driver's error, after
    the rollback); None keeps the session's own `lock_timeout`, a number must be positive.
    """
    check_connection(connection)
    validate_name("epoch name", name)
    validate_lock_timeout(lock_timeout)
    with (
        session_lock(connection, lock_timeout=lock_timeout),
        transaction(connection, repeatable_read=True) as cursor,
    ):
        set_lock_timeout(cursor, lock_timeout)
        _require_installed(cursor)
        pin_output_settings(cursor)  # first: a driver reads a timestamptz only in an ISO `DateStyle`
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


def _validate_tables(tables: Collection[tuple[str, str]] | None) -> list[tuple[str, str]] | None:
    """The distinct pairs of *tables*, sorted; None stays None; refuses all but a non-empty collection of pairs."""
    if tables is None:
        return None
    if isinstance(tables, str) or not isinstance(tables, Collection):
        raise ChangesetError(f"tables must be a collection of (schema, table) pairs, not {tables!r}")
    if not tables:
        raise ChangesetError("tables is empty")
    for pair in tables:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ChangesetError(f"table {pair!r} is not a (schema, table) pair")
        _validate_table(*pair)
    return sorted(set(tables))


def _column_changes(old: Sequence[Column], new: Sequence[Column]) -> tuple[ColumnChange, ...]:
    """The columns removed or of another type (old order), then the columns added (new order)."""
    old_types = {column.name: column.type for column in old}
    new_types = {column.name: column.type for column in new}
    changes = [
        ColumnChange(c.name, "removed" if c.name not in new_types else "changed", c.type, new_types.get(c.name))
        for c in old
        if new_types.get(c.name) != c.type
    ]
    changes += [ColumnChange(c.name, "added", None, c.type) for c in new if c.name not in old_types]
    return tuple(changes)


def _typed_key(table: EpochTable) -> str:
    """The key columns of *table* with their types as the epoch recorded them: `id integer, code text`."""
    types = {column.name: column.type for column in table.columns}
    return ", ".join(f"{name} {types[name]}" for name in table.key)


def _table_diff(
    cursor: Cursor, old: Epoch, new: Epoch, old_table: EpochTable | None, new_table: EpochTable | None
) -> TableDiff | None:
    """The difference of one table between *old* and *new*, None when there is none; the table is in at least one."""
    if old_table is None or new_table is None:
        present = old_table or new_table
        assert present is not None  # the caller names only tables of one epoch
        change = "added" if old_table is None else "removed"
        return TableDiff(present.schema, present.table, change, present.key, (), (), (), ())
    schema, table, key = new_table.schema, new_table.table, new_table.key
    old_key, new_key = _typed_key(old_table), _typed_key(new_table)
    if old_key != new_key:
        raise EpochRefused(
            f"the key of {schema}.{table} differs between epochs {old.name!r} ({old_key})"
            f" and {new.name!r} ({new_key}): leave the table out with tables="
        )
    old_names = {column.name for column in old_table.columns}
    new_names = {column.name for column in new_table.columns}
    old_only = [c.name for c in old_table.columns if c.name not in new_names]
    new_only = [c.name for c in new_table.columns if c.name not in old_names]
    cursor.execute(
        _SELECT_DIFF_SQL,
        (old_only, old.epoch, schema, table, new_only, new.epoch, schema, table, list(key)),
    )
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[RowChange] = []
    for is_added, is_removed, stored_key, old_row, new_row, differing in cursor.fetchall():
        values = _json(stored_key)
        row_key = {name: values.get(name) for name in key}
        if is_added:
            added.append(row_key)
        elif is_removed:
            removed.append(row_key)
        else:
            names = [c.name for c in new_table.columns if c.name in differing]
            old_values, new_values = _json(old_row), _json(new_row)
            changed.append(
                RowChange(row_key, {n: old_values.get(n) for n in names}, {n: new_values.get(n) for n in names})
            )
    columns = _column_changes(old_table.columns, new_table.columns)
    if not (columns or added or removed or changed):
        return None
    return TableDiff(schema, table, "changed", key, columns, tuple(added), tuple(removed), tuple(changed))


def diff(connection: Connection, old: str, new: str, *, tables: Collection[tuple[str, str]] | None = None) -> EpochDiff:
    """The rows and columns that differ between the epochs named *old* and *new*, for *tables* (None: every table).

    *tables* is a non-empty collection of `(schema, table)` pairs. A table in one epoch only is reported as added or
    removed, its rows not listed. A table in both is compared on the columns both epochs have, row by row by its
    key; a column added, removed or of another type is reported as well. Runs in one read-only transaction and
    writes nothing. Raises `ChangesetError` for a bad argument, `LedgerNotInstalled` before `install()`,
    `EpochNotFound` for an unknown epoch or a named table that neither epoch has and `EpochRefused` for a deleted
    epoch or a table whose key differs between the epochs, in a column name or in a column's type.
    """
    check_connection(connection)
    validate_name("epoch name", old)
    validate_name("epoch name", new)
    named = _validate_tables(tables)
    with read_only(connection) as cursor:
        _require_installed(cursor)
        old_epoch = _find_epoch(cursor, old)
        new_epoch = _find_epoch(cursor, new)
        old_tables = {(t.schema, t.table): t for t in old_epoch.tables}
        new_tables = {(t.schema, t.table): t for t in new_epoch.tables}
        names = sorted(old_tables.keys() | new_tables.keys()) if named is None else named
        for schema, table in names:
            if (schema, table) not in old_tables and (schema, table) not in new_tables:
                raise EpochNotFound(f"neither epoch {old!r} nor {new!r} has table {schema}.{table}")
        found = (
            _table_diff(cursor, old_epoch, new_epoch, old_tables.get(name), new_tables.get(name)) for name in names
        )
        differing = tuple(d for d in found if d is not None)
    return EpochDiff(old_epoch, new_epoch, differing)


def delete(connection: Connection, names: Collection[str], *, dry_run: bool = False) -> list[Epoch]:
    """Delete the epochs called *names*: a tombstone in `caverns.epoch_deletion`, then their `epoch_row` rows.

    The catalog row and the sources stay, and the name stays taken for good. It is all or nothing: every name is
    resolved before anything is written, in one transaction under Caverns' advisory lock, so one bad name deletes
    nothing. Returns the deleted epochs as read back (`deleted_at` set), newest first. With *dry_run* the names are
    validated and resolved the same way, in one read-only transaction without the lock, and the epochs that would
    be deleted are returned (`deleted_at` None); nothing is written. Raises `ChangesetError` for *names* that is not
    a non-empty collection of valid names (a bare string or None included), `LedgerNotInstalled` before
    `install()`, `EpochNotFound` for an unknown epoch and `EpochRefused` for an epoch that is already deleted.
    """
    check_connection(connection)
    wanted = _connection.names("names", names)
    if not wanted:
        raise ChangesetError("names is empty")
    for name in wanted:
        validate_name("epoch name", name)
    with read_only(connection) if dry_run else transaction(connection) as cursor:
        if not dry_run:
            lock(cursor)
        _require_installed(cursor)
        found = sorted((_find_epoch(cursor, name) for name in wanted), key=lambda e: e.epoch, reverse=True)
        if dry_run:
            return found
        for epoch in found:
            cursor.execute(_INSERT_TOMBSTONE_SQL, (epoch.epoch, __version__))
            cursor.execute(_DELETE_ROWS_SQL, (epoch.epoch,))
        cursor.execute(_SELECT_EPOCHS_BY_ID_SQL, ([epoch.epoch for epoch in found],))
        deleted = _read_epochs(cursor, cursor.fetchall())
    for epoch in deleted:
        _log.info(
            "deleted epoch %s: %s rows in %s tables",
            epoch.name,
            sum(table.row_count for table in epoch.tables),
            len(epoch.tables),
        )
    return deleted
