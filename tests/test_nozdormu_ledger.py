"""Nozdormu against PostgreSQL 18, on both supported drivers: the ledger and what writes it."""

from __future__ import annotations

from typing import Any

import psycopg
import psycopg2
import pytest

from caverns_of_time import __version__, nozdormu
from caverns_of_time.errors import CavernsError, ChangesetError, LedgerNotInstalled
from caverns_of_time.nozdormu import Changeset, Status, Step, install, preflight

DRIVER_ERRORS = (psycopg.Error, psycopg2.Error)
SCHEMA = Step("schema", "CREATE SCHEMA nozdormu_test", check="SELECT to_regnamespace('nozdormu_test') IS NOT NULL")
TABLE = Step(
    "table", "CREATE TABLE nozdormu_test.t (id int)", check="SELECT to_regclass('nozdormu_test.t') IS NOT NULL"
)
INDEX = Step("index", "CREATE INDEX t_id ON nozdormu_test.t (id)")
APP = Changeset("app", [SCHEMA, TABLE, INDEX])
LEDGER_ROWS = "SELECT version, changeset, label, outcome FROM caverns.ledger ORDER BY version"


def _query(conn: Any, sql: str, params: tuple[Any, ...] | None = None) -> list[tuple[Any, ...]]:
    """Rows of a read, with the transaction it opened ended: Caverns wants a connection with no open work."""
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = [tuple(row) for row in cursor.fetchall()]
    conn.commit()
    return rows


def _execute(conn: Any, sql: str, params: tuple[Any, ...] | None = None) -> None:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
    conn.commit()


def _record(conn: Any, step: Step, outcome: str = "applied", *, checksum: str | None = None) -> None:
    """Write a ledger row by hand, as another runner or an earlier release would have."""
    _execute(
        conn,
        "INSERT INTO caverns.ledger (changeset, label, checksum, outcome, fingerprint, error, caverns_version)"
        " VALUES ('app', %s, %s, %s, %s, %s, 'test')",
        (
            step.label,
            checksum or step.checksum,
            outcome,
            "fingerprint" if outcome == "baseline" else None,
            "boom" if outcome == "failed" else None,
        ),
    )


def _statuses(conn: Any, changeset: Changeset = APP) -> dict[str, Status]:
    return {s.step.label: s.status for s in preflight(conn, changeset)}


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


# --- preflight -----------------------------------------------------------------------------


def test_preflight_refuses_a_ledger_that_is_not_installed(ledger_conn: Any) -> None:
    with pytest.raises(LedgerNotInstalled, match="install"):
        preflight(ledger_conn, APP)


def test_preflight_refuses_the_reserved_changeset_name(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match="reserved"):
        preflight(ledger_conn, Changeset("caverns", [SCHEMA]))


def test_preflight_refuses_a_connection_with_autocommit_on(ledger_conn: Any) -> None:
    install(ledger_conn)
    ledger_conn.autocommit = True
    try:
        with pytest.raises(CavernsError, match="autocommit"):
            preflight(ledger_conn, APP)
    finally:
        ledger_conn.autocommit = False


def test_preflight_of_an_empty_ledger_is_pending(ledger_conn: Any) -> None:
    install(ledger_conn)
    statuses = preflight(ledger_conn, APP)
    assert [(s.step, s.status, s.entry) for s in statuses] == [
        (SCHEMA, Status.PENDING, None),
        (TABLE, Status.PENDING, None),
        (INDEX, Status.PENDING, None),
    ]


def test_preflight_reads_applied_conflict_and_the_ledger_first(ledger_conn: Any) -> None:
    install(ledger_conn)
    _record(ledger_conn, SCHEMA)
    _record(ledger_conn, TABLE, checksum="0" * 64)
    statuses = {s.step.label: s for s in preflight(ledger_conn, APP)}
    assert statuses["schema"].status is Status.APPLIED and statuses["schema"].entry is not None
    assert statuses["schema"].entry.label == "schema"
    assert statuses["table"].status is Status.CONFLICT and statuses["table"].entry is not None
    assert statuses["index"].status is Status.PENDING


def test_preflight_ignores_a_failed_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    _record(ledger_conn, SCHEMA, "failed")
    assert _statuses(ledger_conn)["schema"] is Status.PENDING


def test_preflight_finds_an_effect_the_ledger_never_recorded(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    assert _statuses(ledger_conn) == {"schema": Status.UNKNOWN, "table": Status.PENDING, "index": Status.PENDING}


def test_preflight_trusts_the_ledger_over_a_check(ledger_conn: Any) -> None:
    install(ledger_conn)
    _record(ledger_conn, SCHEMA)  # recorded, though the schema does not exist
    assert _statuses(ledger_conn)["schema"] is Status.APPLIED


def test_preflight_covers_the_steps_up_to_the_baseline(ledger_conn: Any) -> None:
    install(ledger_conn)
    _record(ledger_conn, TABLE, "baseline")
    assert _statuses(ledger_conn) == {"schema": Status.APPLIED, "table": Status.APPLIED, "index": Status.PENDING}


def test_preflight_refuses_a_baseline_at_a_step_the_changeset_lacks(ledger_conn: Any) -> None:
    install(ledger_conn)
    _record(ledger_conn, Step("ghost", "SELECT 1"), "baseline")
    with pytest.raises(ChangesetError, match="'ghost'"):
        preflight(ledger_conn, APP)


@pytest.mark.parametrize(
    "check",
    [
        "SELECT 1",
        "SELECT true, true",
        "SELECT true WHERE false",
        "SELECT 1 / 0 = 0",
        "WITH i AS (INSERT INTO nozdormu_test.t VALUES (1) RETURNING 1) SELECT true FROM i",
    ],
)
def test_preflight_names_the_step_of_a_broken_check(ledger_conn: Any, check: str) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    _execute(ledger_conn, TABLE.sql)
    with pytest.raises(ChangesetError, match="'broken'"):
        preflight(ledger_conn, Changeset("app", [Step("broken", "SELECT 1", check)]))
    assert _query(ledger_conn, "SELECT count(*) FROM nozdormu_test.t") == [(0,)]


def test_preflight_is_read_only_and_leaves_the_connection_clean(ledger_conn: Any) -> None:
    install(ledger_conn)
    preflight(ledger_conn, APP)
    assert _query(ledger_conn, LEDGER_ROWS) == [(1, "caverns", "ledger", "applied")]
    ledger_conn.autocommit = True  # only possible when no transaction is open
    ledger_conn.autocommit = False
