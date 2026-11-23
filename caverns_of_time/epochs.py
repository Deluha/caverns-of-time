"""Epochs, run-level snapshots of opted-in tables; this module holds the registry of the tables to snapshot.

Input: a DB-API 2.0 connection (autocommit off) and the names of a schema, a table and optionally its key
columns. Output: `TrackedTable` values read from and written to `caverns.epoch_table`, the registry created by
Caverns' own step `epochs`. The module imports no database driver. Contract and behaviour:
`caverns_of_time/README.md`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from caverns_of_time._connection import Connection, Cursor, check_connection, lock, read_only, transaction
from caverns_of_time.errors import ChangesetError, EpochRefused, LedgerNotInstalled
from caverns_of_time.nozdormu import EPOCHS_EXISTS_SQL

__all__ = ["TrackedTable", "register", "tracked", "unregister"]

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
_LIVE_COLUMNS_SQL = """
SELECT a.attname::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
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


@dataclass(frozen=True)
class TrackedTable:
    """One registered table: its exact schema and table names, its key columns in order, who registered it and when."""

    schema: str
    table: str
    key: tuple[str, ...]
    registered_at: datetime
    registered_by: str


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
    live = set(_column_names(cursor, _LIVE_COLUMNS_SQL, schema, table))
    missing = next((column for column in key if column not in live), None)
    if missing is not None:
        raise EpochRefused(f"{schema}.{table} has no column {missing!r} for the key")
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
