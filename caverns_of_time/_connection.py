"""Shared connection plumbing for the components: the connection contract, its guards and transactions.

Not public: the components import it, a consumer imports `Connection` from `nozdormu`. Input: a DB-API 2.0
connection (autocommit off). Output: cursors in transactions Caverns commits or rolls back itself, the advisory
lock every Caverns write takes (per transaction, or per session ahead of a REPEATABLE READ transaction), and the
validation and catalog helpers the components share (names, limits, the pinned search path and output settings).
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from caverns_of_time.errors import CavernsError, ChangesetError

__all__ = [
    "ADVISORY_LOCK_KEY",
    "EPOCHS_EXISTS_SQL",
    "Connection",
    "Cursor",
    "check_connection",
    "lock",
    "names",
    "pin_output_settings",
    "pin_search_path",
    "read_only",
    "run_check",
    "session_lock",
    "transaction",
    "validate_limit",
    "validate_name",
]


class Cursor(Protocol):
    """The part of a DB-API 2.0 cursor Caverns uses; psycopg 3 and psycopg2 both fit."""

    def execute(self, query: str, params: Sequence[Any] | None = ..., /) -> object: ...

    def fetchone(self) -> Sequence[Any] | None: ...

    def fetchall(self) -> Sequence[Sequence[Any]]: ...

    def close(self) -> object: ...


class Connection(Protocol):
    """The part of a DB-API 2.0 connection Caverns uses; psycopg 3 and psycopg2 both fit.

    Parameters are `%s` placeholders. Autocommit must be off: Caverns commits and rolls back itself,
    and a connection with uncommitted work is refused.
    """

    @property
    def autocommit(self) -> bool: ...

    def cursor(self) -> Cursor: ...

    def commit(self) -> object: ...

    def rollback(self) -> object: ...


# False when the connection's open transaction has written something (it holds a transaction id).
_CLEAN_TRANSACTION_SQL = "SELECT pg_current_xact_id_if_assigned() IS NULL"
# Under REPEATABLE READ or SERIALIZABLE the lock statement would take the snapshot before it waits.
_READ_COMMITTED_SQL = "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
_REPEATABLE_READ_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"
# One key for every Caverns write, whatever the component: Caverns' writers take turns.
ADVISORY_LOCK_KEY = 0x43617665726E73  # "Caverns"
_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s::bigint)"
_SESSION_LOCK_SQL = "SELECT pg_advisory_lock(%s::bigint)"
_SESSION_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s::bigint)"
# Lets a catalog read name an object by its schema-qualified type regardless of the caller's search_path.
_PIN_SEARCH_PATH_SQL = "SELECT set_config('search_path', 'pg_catalog', true)"
# The settings that change how a value prints (`to_jsonb` of a timestamptz, interval, float8 or bytea).
_PIN_OUTPUT_SETTINGS_SQL = (
    "SELECT set_config('TimeZone', 'UTC', true), set_config('IntervalStyle', 'postgres', true),"
    " set_config('extra_float_digits', '1', true), set_config('bytea_output', 'hex', true)"
)
_LIMIT_MAX = 2**63 - 1  # a bigint, what `LIMIT` takes
# True only when all five epoch tables exist; `epochs` (the registry) reads it too, to refuse before the step ran.
EPOCHS_EXISTS_SQL = (
    "SELECT to_regclass('caverns.epoch_table') IS NOT NULL AND to_regclass('caverns.epoch') IS NOT NULL"
    " AND to_regclass('caverns.epoch_source') IS NOT NULL AND to_regclass('caverns.epoch_row') IS NOT NULL"
    " AND to_regclass('caverns.epoch_deletion') IS NOT NULL"
)
_log = logging.getLogger(__name__)


def check_connection(connection: Connection) -> None:
    """Refuse a connection with autocommit on or with uncommitted writes; leave a clean one with no open transaction."""
    if connection.autocommit:
        raise CavernsError(
            "the connection has autocommit on; Caverns commits and rolls back itself, "
            "so pass a connection with autocommit off"
        )
    try:
        with cursor(connection) as cur:
            cur.execute(_CLEAN_TRANSACTION_SQL)
            row = cur.fetchone()
    except Exception as error:
        # No rollback here either: the caller decides what becomes of its failed transaction.
        raise CavernsError(
            "the connection cannot run a statement: its transaction has failed or the connection is lost; "
            "roll it back or reconnect before calling Caverns"
        ) from error
    if row is None or not row[0]:
        # No rollback: the caller's work must survive the refusal.
        raise CavernsError("the connection has uncommitted work; commit or roll it back before calling Caverns")
    connection.rollback()  # only reads so far: nothing is lost


@contextmanager
def cursor(connection: Connection) -> Iterator[Cursor]:
    cur = connection.cursor()
    try:
        yield cur
    finally:
        cur.close()


@contextmanager
def transaction(connection: Connection, *, repeatable_read: bool = False) -> Iterator[Cursor]:
    """One transaction: committed when the block ends, rolled back when it raises.

    READ COMMITTED unless *repeatable_read*: the caller then holds the lock already (`session_lock`).
    """
    try:
        with cursor(connection) as cur:
            # The ledger is re-read after the lock: that read needs a snapshot taken after the wait.
            cur.execute(_REPEATABLE_READ_SQL if repeatable_read else _READ_COMMITTED_SQL)
            yield cur
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


@contextmanager
def read_only(connection: Connection) -> Iterator[Cursor]:
    """A read-only transaction that is always rolled back."""
    try:
        with cursor(connection) as cur:
            cur.execute(_READ_ONLY_SQL)
            yield cur
    finally:
        connection.rollback()


def lock(cursor: Cursor) -> None:
    """Take Caverns' advisory lock until the transaction ends."""
    cursor.execute(_LOCK_SQL, (ADVISORY_LOCK_KEY,))


@contextmanager
def session_lock(connection: Connection) -> Iterator[None]:
    """Hold Caverns' advisory lock for the session, taken and committed before the block begins.

    For a block that runs a REPEATABLE READ `transaction`: the transaction-level `lock` statement would take
    that transaction's snapshot before it waits (see `_READ_COMMITTED_SQL`), while this one commits first, so
    the snapshot comes after the lock is held. The lock is released when the block ends. A session lock survives
    a rollback, so a failure while taking it (the server may have granted it) and a block that raised release it
    best-effort and let the error propagate (a lost connection frees the lock); a release that fails after the
    block succeeded is logged as a warning, never raised: the block's work is committed.
    """
    try:
        with cursor(connection) as cur:
            cur.execute(_SESSION_LOCK_SQL, (ADVISORY_LOCK_KEY,))
        connection.commit()
    except BaseException:
        _release_session_lock_quietly(connection)
        raise
    try:
        yield
    except BaseException:
        _release_session_lock_quietly(connection)
        raise
    try:
        _release_session_lock(connection)
    except Exception as error:
        _log.warning("could not release Caverns' advisory lock: %s", error)


def _release_session_lock(connection: Connection) -> None:
    connection.rollback()
    with cursor(connection) as cur:
        cur.execute(_SESSION_UNLOCK_SQL, (ADVISORY_LOCK_KEY,))
    connection.commit()


def _release_session_lock_quietly(connection: Connection) -> None:
    try:
        _release_session_lock(connection)
    except Exception:
        pass  # the caller re-raises the error that matters; a lost connection frees the lock


def pin_search_path(cursor: Cursor) -> None:
    """Pin the search path to `pg_catalog` until the transaction ends, so `format_type` qualifies every name."""
    cursor.execute(_PIN_SEARCH_PATH_SQL)


def pin_output_settings(cursor: Cursor) -> None:
    """Pin the settings that shape how values print (time zone, interval style, float digits, bytea output).

    Until the transaction ends, so what a snapshot stores does not depend on the session that took it.
    """
    cursor.execute(_PIN_OUTPUT_SETTINGS_SQL)


def validate_limit(limit: object) -> None:
    """A row limit: None (all) or a positive integer up to the bigint maximum, never a bool."""
    if limit is None:
        return
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _LIMIT_MAX:
        raise ChangesetError(f"limit must be a positive integer up to {_LIMIT_MAX}, not {limit!r}")


def validate_name(kind: str, value: object) -> None:
    """A label or changeset name: a non-empty single line without surrounding whitespace."""
    if not isinstance(value, str):
        raise ChangesetError(f"{kind} {value!r} is not a string")
    if not value:
        raise ChangesetError(f"{kind} is empty")
    if value != value.strip():
        raise ChangesetError(f"{kind} {value!r} has surrounding whitespace")
    if len(value.splitlines()) != 1:
        raise ChangesetError(f"{kind} {value!r} spans more than one line")


def names(argument: str, values: Collection[str]) -> frozenset[str]:
    """*values* as a set; a bare string is refused because it would be read as its characters."""
    if values is None:
        raise ChangesetError(f"{argument} must be a collection of names, not None")
    if isinstance(values, str):
        raise ChangesetError(f"{argument} must be a collection of names, not the string {values!r}")
    return frozenset(values)


def has_more_results(cursor: Cursor) -> bool:
    """Whether the statement the cursor ran produced a further result set (a second statement)."""
    nextset = getattr(cursor, "nextset", None)
    if not callable(nextset):
        return False
    try:
        return bool(nextset())
    except Exception:
        # NOTE: psycopg2 raises NotSupportedError here: such a driver cannot report further result sets,
        # so the one-statement rule is documented rather than detected there.
        return False


def run_check(cursor: Cursor, label: str, check: str) -> bool:
    """Run a step's check; it must be one statement returning one row with one boolean column."""
    try:
        cursor.execute(check)
        rows = cursor.fetchall()
        more = has_more_results(cursor)
    except Exception as error:
        raise ChangesetError(f"the check of step {label!r} failed: {error}") from error
    if more:
        raise ChangesetError(f"the check of step {label!r} must be one statement")
    if len(rows) == 1 and len(rows[0]) == 1 and isinstance(rows[0][0], bool):
        return rows[0][0]
    raise ChangesetError(f"the check of step {label!r} must return one row with one boolean column")
