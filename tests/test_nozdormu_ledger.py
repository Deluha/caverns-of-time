"""Nozdormu against PostgreSQL 18, on both supported drivers: the ledger and what writes it."""

from __future__ import annotations

from typing import Any

import psycopg
import psycopg2
import pytest

from caverns_of_time import __version__, nozdormu
from caverns_of_time.errors import CavernsError
from caverns_of_time.nozdormu import install

DRIVER_ERRORS = (psycopg.Error, psycopg2.Error)
LEDGER_ROWS = "SELECT version, changeset, label, outcome FROM caverns.ledger ORDER BY version"


def _query(conn: Any, sql: str, params: tuple[Any, ...] | None = None) -> list[tuple[Any, ...]]:
    """Rows of a read, with the transaction it opened ended: Caverns wants a connection with no open work."""
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = [tuple(row) for row in cursor.fetchall()]
    conn.commit()
    return rows


def _refused(conn: Any, sql: str, message: str) -> None:
    with pytest.raises(DRIVER_ERRORS, match=message), conn.cursor() as cursor:
        cursor.execute(sql)
    conn.rollback()


# --- install -------------------------------------------------------------------------------


def test_install_creates_the_ledger_and_records_its_own_step(ledger_conn: Any) -> None:
    (entry,) = install(ledger_conn)
    own_step = nozdormu._OWN_CHANGESET.steps[0]
    assert (entry.version, entry.changeset, entry.label, entry.outcome) == (1, "caverns", "ledger", "applied")
    assert entry.checksum == own_step.checksum
    assert (entry.fingerprint, entry.error) == (None, None)
    assert entry.duration_ms is not None and entry.duration_ms >= 0
    assert entry.caverns_version == __version__
    assert entry.recorded_by and entry.recorded_at is not None
    assert _query(ledger_conn, LEDGER_ROWS) == [(1, "caverns", "ledger", "applied")]


def test_install_twice_writes_nothing_the_second_time(ledger_conn: Any) -> None:
    install(ledger_conn)
    assert install(ledger_conn) == []
    assert _query(ledger_conn, LEDGER_ROWS) == [(1, "caverns", "ledger", "applied")]


def test_install_refuses_a_connection_with_autocommit_on(ledger_conn: Any) -> None:
    ledger_conn.autocommit = True
    try:
        with pytest.raises(CavernsError, match="autocommit"):
            install(ledger_conn)
    finally:
        ledger_conn.autocommit = False


# --- the ledger's own rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE caverns.ledger SET label = 'other'",
        "DELETE FROM caverns.ledger",
        "TRUNCATE caverns.ledger",
    ],
)
def test_the_ledger_is_append_only(ledger_conn: Any, statement: str) -> None:
    install(ledger_conn)
    _refused(ledger_conn, statement, "append-only")
    assert _query(ledger_conn, LEDGER_ROWS) == [(1, "caverns", "ledger", "applied")]


@pytest.mark.parametrize(
    "values",
    [
        "'c', 'l', 'not-a-checksum', 'applied', NULL, NULL",
        f"'c', 'l', '{'0' * 64}', 'unknown', NULL, NULL",
        f"'c', 'l', '{'0' * 64}', 'baseline', NULL, NULL",
        f"'c', 'l', '{'0' * 64}', 'failed', NULL, NULL",
        f"'c', 'l', '{'0' * 64}', 'applied', NULL, 'an error'",
    ],
)
def test_the_ledger_refuses_an_inconsistent_row(ledger_conn: Any, values: str) -> None:
    install(ledger_conn)
    insert = (
        "INSERT INTO caverns.ledger (changeset, label, checksum, outcome, fingerprint, error, caverns_version) "
        f"VALUES ({values}, 'v')"
    )
    _refused(ledger_conn, insert, "violates check constraint")
