"""Shared connection plumbing for the components: the connection contract, its guards and transactions.

Not public: the components import it, a consumer imports `Connection` from `nozdormu`. Input: a DB-API 2.0
connection (autocommit off). Output: cursors in transactions Caverns commits or rolls back itself, the advisory
lock every Caverns write takes (per transaction, or per session ahead of a REPEATABLE READ transaction), and the
validation and catalog helpers the components share (names, limits, the pinned search path and settings).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from caverns_of_time.errors import CavernsError, ChangesetError

__all__ = [
    "ADVISORY_LOCK_KEY",
    "DEFAULT_LOCK_TIMEOUT",
    "EPOCHS_EXISTS_SQL",
    "MILLISECONDS",
    "Connection",
    "Cursor",
    "check_connection",
    "check_writable",
    "lock",
    "names",
    "pin_definition_settings",
    "pin_output_settings",
    "pin_search_path",
    "read_only",
    "run_check",
    "session_lock",
    "set_lock_timeout",
    "sqlstate",
    "transaction",
    "validate_limit",
    "validate_lock_timeout",
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


# What a write needs of the session: a primary (not in recovery) whose transactions may write.
_WRITABLE_SQL = "SELECT pg_is_in_recovery(), current_setting('transaction_read_only')"
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
# Seconds a Caverns write waits for any lock (its own advisory lock, and for a step the locks its SQL takes).
DEFAULT_LOCK_TIMEOUT = 5.0
# Transaction-local: it ends with the transaction, so the session's own setting is never changed.
_LOCK_TIMEOUT_SQL = "SELECT set_config('lock_timeout', %s, true)"
_NO_LOCK_TIMEOUT = "0"  # lock_timeout 0 waits for ever
_LOCK_NOT_AVAILABLE = "55P03"  # SQLSTATE lock_not_available, what a lock_timeout raises
_LOCK_TIMEOUT_MAX_MS = 2**31 - 1  # an integer of milliseconds, what `lock_timeout` takes
MILLISECONDS = 1000
# Lets a catalog read name an object by its schema-qualified type regardless of the caller's search_path.
_PIN_SEARCH_PATH_SQL = "SELECT set_config('search_path', 'pg_catalog', true)"
# The settings that change how a value prints (`to_jsonb` of a timestamptz, a range of them, interval, float8,
# bytea or money).
_PIN_OUTPUT_SETTINGS_SQL = (
    "SELECT set_config('TimeZone', 'UTC', true), set_config('IntervalStyle', 'postgres', true),"
    " set_config('extra_float_digits', '1', true), set_config('bytea_output', 'hex', true),"
    " set_config('DateStyle', 'ISO, MDY', true), set_config('lc_monetary', 'C', true)"
)
# On top of the output settings, the ones that change how a definition prints: `pg_get_viewdef`, `pg_get_expr` and
# `pg_get_functiondef` quote every identifier under `quote_all_identifiers`, and read a string literal by
# `standard_conforming_strings`.
_PIN_DEFINITION_SETTINGS_SQL = (
    "SELECT set_config('quote_all_identifiers', 'off', true), set_config('standard_conforming_strings', 'on', true)"
)
_LIMIT_MAX = 2**63 - 1  # a bigint, what `LIMIT` takes
# True only when all five epoch tables exist. Used by the `epochs` own step's check (nozdormu) and by the epoch
# functions (`epochs`), which refuse with it before that step ran.
EPOCHS_EXISTS_SQL = (
    "SELECT to_regclass('caverns.epoch_table') IS NOT NULL AND to_regclass('caverns.epoch') IS NOT NULL"
    " AND to_regclass('caverns.epoch_source') IS NOT NULL AND to_regclass('caverns.epoch_row') IS NOT NULL"
    " AND to_regclass('caverns.epoch_deletion') IS NOT NULL"
)


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


def check_writable(connection: Connection) -> None:
    """Refuse a connection to a standby or to a session that cannot write; leave no open transaction.

    Read in a plain transaction, never `read_only`, which would make the read itself read-only. Called by every
    write after `check_connection`, so nothing is read or written first.
    """
    with cursor(connection) as cur:
        cur.execute(_WRITABLE_SQL)
        row = cur.fetchone()
    connection.rollback()
    if row is None:
        raise CavernsError("the connection cannot tell whether the database is writable")
    in_recovery, read_only_setting = row
    if in_recovery:
        raise CavernsError("the database is a standby (in recovery): Caverns writes only to a primary")
    if read_only_setting == "on":
        raise CavernsError(
            "the session is read-only (default_transaction_read_only is on): Caverns needs a session that can write"
        )


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


def sqlstate(error: BaseException) -> str | None:
    """The SQLSTATE of a driver's error (psycopg 3 `sqlstate`, psycopg2 `pgcode`), read without importing a driver."""
    code = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    return code if isinstance(code, str) and code else None


def _milliseconds(seconds: float) -> int:
    return max(1, round(seconds * MILLISECONDS))


def _set_lock_timeout_setting(cursor: Cursor, value: str) -> None:
    cursor.execute(_LOCK_TIMEOUT_SQL, (value,))


def set_lock_timeout(cursor: Cursor, seconds: float | None) -> None:
    """Bound every lock wait of the transaction to *seconds*; None leaves the session's own setting."""
    if seconds is not None:
        _set_lock_timeout_setting(cursor, f"{_milliseconds(seconds)}ms")


def _take_advisory_lock(cursor: Cursor, sql: str, lock_timeout: float | None, *, unbounded_wait: bool = False) -> None:
    """Run the advisory lock statement *sql* under *lock_timeout*; a wait that times out is a `CavernsError`.

    With *unbounded_wait* (and a *lock_timeout*) the statement waits with no timeout and *lock_timeout* is set
    after it, for the transaction's later lock waits.
    """
    unbounded_wait = unbounded_wait and lock_timeout is not None
    if unbounded_wait:
        _set_lock_timeout_setting(cursor, _NO_LOCK_TIMEOUT)
    else:
        set_lock_timeout(cursor, lock_timeout)
    try:
        cursor.execute(sql, (ADVISORY_LOCK_KEY,))
    except Exception as error:
        if sqlstate(error) != _LOCK_NOT_AVAILABLE:
            raise
        waited = "" if lock_timeout is None else f" after {_seconds_text(lock_timeout)}"
        raise CavernsError(
            f"another Caverns run holds Caverns' advisory lock: gave up waiting{waited} (lock_timeout)"
        ) from error
    if unbounded_wait:
        set_lock_timeout(cursor, lock_timeout)


def _seconds_text(seconds: float) -> str:
    text = str(int(seconds)) if seconds == int(seconds) else str(seconds)
    return f"{text} second" if text == "1" else f"{text} seconds"


def lock(cursor: Cursor, *, lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT, unbounded_wait: bool = False) -> None:
    """Take Caverns' advisory lock until the transaction ends.

    *lock_timeout* seconds (None: the session's own setting) bound the wait, and every later lock wait of the
    transaction; a wait that times out raises `CavernsError`. With *unbounded_wait* the wait for Caverns' lock
    has no timeout (for a run that has written already and must not stop before it is complete), and
    *lock_timeout* bounds only the waits after it; with None the session's own setting governs both.
    """
    _take_advisory_lock(cursor, _LOCK_SQL, lock_timeout, unbounded_wait=unbounded_wait)


@contextmanager
def session_lock(
    connection: Connection, *, committed: str, lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT
) -> Iterator[None]:
    """Hold Caverns' advisory lock for the session, taken and committed before the block begins.

    For a block that runs a REPEATABLE READ `transaction`: the transaction-level `lock` statement would take
    that transaction's snapshot before it waits (see `_READ_COMMITTED_SQL`), while this one commits first, so
    the snapshot comes after the lock is held. *lock_timeout* bounds the wait as for `lock`. The lock is
    released when the block ends. A session lock survives a rollback, so a failure while taking it (the server
    may have granted it) and a block that raised release it best-effort and let the error propagate (a lost
    connection frees the lock). A release that fails after the block succeeded (an error, or `false` from
    `pg_advisory_unlock`: the lock was not held, as behind a transaction-mode pooler) raises `CavernsError`
    that names *committed*, the block's work, which stands, and the cause.
    """
    try:
        with cursor(connection) as cur:
            _take_advisory_lock(cur, _SESSION_LOCK_SQL, lock_timeout)
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
        held = _release_session_lock(connection)
    except Exception as error:
        raise CavernsError(_release_failure(committed, str(error).strip())) from error
    if not held:
        raise CavernsError(_release_failure(committed, "the lock was not held"))


def _release_failure(committed: str, reason: str) -> str:
    return (
        f"{committed}, but releasing Caverns' advisory lock failed ({reason}): "
        "Caverns needs a direct connection or a session-mode pooler, not a transaction-mode one"
    )


def _release_session_lock(connection: Connection) -> bool:
    """Release the session lock; whether it was held (`pg_advisory_unlock` returns false when it was not)."""
    connection.rollback()
    with cursor(connection) as cur:
        cur.execute(_SESSION_UNLOCK_SQL, (ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
    connection.commit()
    return bool(row and row[0])


def _release_session_lock_quietly(connection: Connection) -> None:
    try:
        _release_session_lock(connection)
    except Exception:
        pass  # the caller re-raises the error that matters; a lost connection frees the lock


def pin_search_path(cursor: Cursor) -> None:
    """Pin the search path to `pg_catalog` until the transaction ends, so `format_type` qualifies every name."""
    cursor.execute(_PIN_SEARCH_PATH_SQL)


def pin_output_settings(cursor: Cursor) -> None:
    """Pin the settings that shape how values print (time zone, interval and date style, float digits, bytea, money).

    Until the transaction ends, so what a snapshot stores does not depend on the session that took it.
    """
    cursor.execute(_PIN_OUTPUT_SETTINGS_SQL)


def pin_definition_settings(cursor: Cursor) -> None:
    """Pin the settings that shape how a catalog prints a definition: the output settings, identifier quoting and
    string literals.

    Until the transaction ends; the caller's session is untouched. The fingerprint reads under them, so its hash
    does not depend on the session that took it.
    """
    pin_output_settings(cursor)
    cursor.execute(_PIN_DEFINITION_SETTINGS_SQL)


def validate_limit(limit: object) -> None:
    """A row limit: None (all) or a positive integer up to the bigint maximum, never a bool."""
    if limit is None:
        return
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _LIMIT_MAX:
        raise ChangesetError(f"limit must be a positive integer up to {_LIMIT_MAX}, not {limit!r}")


def validate_lock_timeout(value: object) -> None:
    """A lock timeout: None (the session's own) or a positive finite number of seconds, 1 ms to the integer limit."""
    if value is None:
        return
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 < value
        or _milliseconds(value) > _LOCK_TIMEOUT_MAX_MS
    ):
        raise ChangesetError(
            f"lock_timeout must be None or a positive number of seconds up to {_LOCK_TIMEOUT_MAX_MS // MILLISECONDS},"
            f" not {value!r}"
        )


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
