"""Shared connection plumbing for the components: the connection contract, its guards and transactions.

Not public: the components import it, a consumer imports `Connection` from `nozdormu`. Input: a DB-API 2.0
connection (autocommit off). Output: cursors in transactions Caverns commits or rolls back itself.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from caverns_of_time.errors import CavernsError, ChangesetError

__all__ = [
    "Connection",
    "Cursor",
    "check_connection",
    "cursor",
    "has_more_results",
    "names",
    "read_only",
    "run_check",
    "transaction",
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
_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"


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
def transaction(connection: Connection) -> Iterator[Cursor]:
    """One transaction: committed when the block ends, rolled back when it raises."""
    try:
        with cursor(connection) as cur:
            # The ledger is re-read after the lock: that read needs a snapshot taken after the wait.
            cur.execute(_READ_COMMITTED_SQL)
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
