"""Nozdormu against PostgreSQL 18, on both supported drivers: the ledger and what writes it."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import psycopg
import psycopg2
import pytest

from caverns_of_time import __version__, _connection, nozdormu
from caverns_of_time.errors import (
    BaselineRefused,
    CavernsError,
    ChangesetError,
    LedgerNotInstalled,
    PreflightRefused,
    StepFailed,
)
from caverns_of_time.infinite import fingerprint as take_fingerprint
from caverns_of_time.nozdormu import Changeset, Status, Step, apply, baseline, history, install, preflight

DRIVER_ERRORS = (psycopg.Error, psycopg2.Error)
SCHEMA = Step("schema", "CREATE SCHEMA nozdormu_test", check="SELECT to_regnamespace('nozdormu_test') IS NOT NULL")
TABLE = Step(
    "table", "CREATE TABLE nozdormu_test.t (id int)", check="SELECT to_regclass('nozdormu_test.t') IS NOT NULL"
)
INDEX = Step("index", "CREATE INDEX t_id ON nozdormu_test.t (id)")
APP = Changeset("app", [SCHEMA, TABLE, INDEX])
APP_ROWS = "SELECT label, outcome FROM caverns.ledger WHERE changeset = 'app' ORDER BY version"
LEDGER_ROWS = "SELECT version, changeset, label, outcome FROM caverns.ledger ORDER BY version"
# What `install` writes: one row for each step of Caverns' own changeset.
OWN_ROWS = [
    (1, "caverns", "ledger", "applied"),
    (2, "caverns", "fingerprint", "applied"),
    (3, "caverns", "epochs", "applied"),
]
FINGERPRINT_ROWS = "SELECT version, schemas, hash FROM caverns.fingerprint ORDER BY version"
FIRST_TWO = Changeset("app", [SCHEMA, TABLE])


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


def _exists(conn: Any, name: str) -> bool:
    return bool(_query(conn, "SELECT to_regclass(%s) IS NOT NULL", (name,))[0][0])


def _statuses(conn: Any, changeset: Changeset = APP) -> dict[str, Status]:
    return {s.step.label: s.status for s in preflight(conn, changeset)}


def _refused(conn: Any, sql: str, message: str, params: tuple[Any, ...] | None = None) -> None:
    with pytest.raises(DRIVER_ERRORS, match=message), conn.cursor() as cursor:
        cursor.execute(sql, params)
    conn.rollback()


def _assert_idle(conn: Any) -> None:
    """The connection has no open transaction: both drivers refuse to switch autocommit inside one."""
    conn.autocommit = True
    conn.autocommit = False


# --- install -------------------------------------------------------------------------------


def test_install_creates_the_ledger_and_records_its_own_steps(ledger_conn: Any) -> None:
    entries = install(ledger_conn)
    assert [(e.version, e.changeset, e.label, e.outcome) for e in entries] == OWN_ROWS
    assert [e.checksum for e in entries] == [step.checksum for step in nozdormu._OWN_CHANGESET.steps]
    for entry in entries:
        assert (entry.fingerprint, entry.error) == (None, None)
        assert entry.duration_ms is not None and entry.duration_ms >= 0
        assert entry.caverns_version == __version__
        assert entry.recorded_by and entry.recorded_at is not None
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _exists(ledger_conn, "caverns.ledger") and _exists(ledger_conn, "caverns.fingerprint")
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.fingerprint") == [(0,)]  # installing records nothing


def test_install_twice_writes_nothing_the_second_time(ledger_conn: Any) -> None:
    install(ledger_conn)
    assert install(ledger_conn) == []
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS


def test_install_accepts_its_own_step_when_the_table_exists_without_its_row(ledger_conn: Any) -> None:
    _execute(ledger_conn, nozdormu._LEDGER_DDL_SQL)  # a schema-only restore: the table, but no row
    with pytest.raises(LedgerNotInstalled):
        preflight(ledger_conn, APP)
    _assert_idle(ledger_conn)
    entries = install(ledger_conn)
    assert [(e.changeset, e.label, e.outcome) for e in entries] == [
        ("caverns", "ledger", "accepted"),
        ("caverns", "fingerprint", "applied"),
        ("caverns", "epochs", "applied"),
    ]
    assert entries[0].duration_ms is None
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


def test_install_refuses_a_connection_with_autocommit_on(ledger_conn: Any) -> None:
    ledger_conn.autocommit = True
    try:
        with pytest.raises(CavernsError, match="autocommit"):
            install(ledger_conn)
    finally:
        ledger_conn.autocommit = False


def test_install_refuses_a_connection_with_uncommitted_work(ledger_conn: Any) -> None:
    _execute(ledger_conn, "CREATE SCHEMA nozdormu_test")
    with ledger_conn.cursor() as cursor:
        cursor.execute("CREATE TABLE nozdormu_test.caller_work (id int)")
    with pytest.raises(CavernsError, match="uncommitted work"):
        install(ledger_conn)
    ledger_conn.commit()
    assert _exists(ledger_conn, "nozdormu_test.caller_work")
    assert not _exists(ledger_conn, "caverns.ledger")


# --- the ledger's own rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE caverns.ledger SET label = 'other'",
        "DELETE FROM caverns.ledger",
        "TRUNCATE caverns.ledger CASCADE",  # without CASCADE the fingerprint table's foreign key refuses it first
    ],
)
def test_the_ledger_is_append_only(ledger_conn: Any, statement: str) -> None:
    install(ledger_conn)
    _refused(ledger_conn, statement, "append-only")
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS


INSERT_ROW_SQL = (
    "INSERT INTO caverns.ledger (changeset, label, checksum, outcome, fingerprint, error, caverns_version)"
    " VALUES (%s, %s, %s, %s, %s, %s, 'v')"
)
ZEROS = "0" * 64


@pytest.mark.parametrize(
    "values",
    [
        ("c", "l", "not-a-checksum", "applied", None, None),
        ("c", "l", ZEROS, "unknown", None, None),
        ("c", "l", ZEROS, "baseline", None, None),
        ("c", "l", ZEROS, "failed", None, None),
        ("c", "l", ZEROS, "applied", None, "an error"),
    ],
)
def test_the_ledger_refuses_an_inconsistent_row(ledger_conn: Any, values: tuple[Any, ...]) -> None:
    install(ledger_conn)
    _refused(ledger_conn, INSERT_ROW_SQL, "violates check constraint", values)


# --- the fingerprint table, Caverns' second own step ---------------------------------------


def test_a_ledger_of_the_previous_release_upgrades_with_install(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:  # the previous release's own changeset: the `ledger` step alone
        patch.setattr(nozdormu, "_OWN_CHANGESET", Changeset("caverns", nozdormu._OWN_CHANGESET.steps[:1]))
        install(ledger_conn)
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS[:1]
    assert not _exists(ledger_conn, "caverns.fingerprint")
    with pytest.raises(LedgerNotInstalled, match="call install"):
        preflight(ledger_conn, APP)
    _assert_idle(ledger_conn)
    assert [e.label for e in history(ledger_conn)] == ["ledger"]  # the old rows still read
    entries = install(ledger_conn)
    assert [(e.version, e.label, e.outcome) for e in entries] == [
        (2, "fingerprint", "applied"),
        (3, "epochs", "applied"),
    ]
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _exists(ledger_conn, "caverns.fingerprint")
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE caverns.fingerprint SET hash = hash",
        "DELETE FROM caverns.fingerprint",
        "TRUNCATE caverns.fingerprint",
    ],
)
def test_the_fingerprint_table_is_append_only(ledger_conn: Any, statement: str) -> None:
    install(ledger_conn)
    apply(ledger_conn, FIRST_TWO)
    before = _query(ledger_conn, FINGERPRINT_ROWS)
    assert len(before) == 1
    _refused(ledger_conn, statement, "append-only")
    assert _query(ledger_conn, FINGERPRINT_ROWS) == before


@pytest.mark.parametrize(
    ("version", "digest", "message"),
    [(1, "not-a-hash", "violates check constraint"), (999, ZEROS, "violates foreign key constraint")],
)
def test_the_fingerprint_table_refuses_an_inconsistent_row(
    ledger_conn: Any, version: int, digest: str, message: str
) -> None:
    install(ledger_conn)
    _refused(
        ledger_conn,
        "INSERT INTO caverns.fingerprint (version, schemas, document, hash) VALUES (%s, '{}', '{}', %s)",
        message,
        (version, digest),
    )


# --- preflight -----------------------------------------------------------------------------


def test_preflight_refuses_a_ledger_that_is_not_installed(ledger_conn: Any) -> None:
    with pytest.raises(LedgerNotInstalled, match="install"):
        preflight(ledger_conn, APP)
    _assert_idle(ledger_conn)


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
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, "SELECT count(*) FROM nozdormu_test.t") == [(0,)]


def test_a_check_is_one_statement_where_the_driver_reports_a_second(ledger_conn: Any, request: Any) -> None:
    install(ledger_conn)
    one = Changeset("app", [Step("one", "SELECT 1", "SELECT false")])
    assert [s.status for s in preflight(ledger_conn, one)] == [Status.PENDING]
    two = Changeset("app", [Step("two", "SELECT 1", "SELECT false; SELECT true")])
    if request.node.callspec.params["ledger_conn"] == "psycopg":
        with pytest.raises(ChangesetError, match="'two'.*one statement"):
            preflight(ledger_conn, two)
        _assert_idle(ledger_conn)
    else:  # psycopg2 cannot report a further result set: the rule is documented there, not detected
        assert len(preflight(ledger_conn, two)) == 1


def test_preflight_refuses_a_connection_that_was_lost(second_conn: Any) -> None:
    second_conn.close()
    with pytest.raises(CavernsError, match="cannot run a statement") as refused:
        preflight(second_conn, APP)
    assert isinstance(refused.value.__cause__, DRIVER_ERRORS)


def test_preflight_is_read_only_and_leaves_the_connection_clean(ledger_conn: Any) -> None:
    install(ledger_conn)
    preflight(ledger_conn, APP)
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    _assert_idle(ledger_conn)


@pytest.mark.parametrize(
    "call",
    [
        lambda conn: preflight(conn, APP),
        lambda conn: apply(conn, APP),
        lambda conn: baseline(conn, APP),
    ],
    ids=["preflight", "apply", "baseline"],
)
def test_a_connection_with_uncommitted_work_is_refused_and_keeps_its_work(ledger_conn: Any, call: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, "CREATE SCHEMA nozdormu_test")
    with ledger_conn.cursor() as cursor:
        cursor.execute("CREATE TABLE nozdormu_test.caller_work (id int)")
    with pytest.raises(CavernsError, match="uncommitted work"):
        call(ledger_conn)
    ledger_conn.commit()
    assert _exists(ledger_conn, "nozdormu_test.caller_work")
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS


def test_a_connection_with_a_failed_transaction_is_refused_untouched(ledger_conn: Any) -> None:
    install(ledger_conn)
    try:
        with ledger_conn.cursor() as cursor:
            cursor.execute("SELECT 1/0")
    except Exception:  # the driver's own error; the transaction is now aborted
        pass
    with pytest.raises(CavernsError, match="transaction has failed") as refused:
        preflight(ledger_conn, APP)
    assert refused.value.__cause__ is not None
    ledger_conn.rollback()
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


def test_preflight_accepts_a_connection_with_an_open_read_only_transaction(ledger_conn: Any) -> None:
    install(ledger_conn)
    with ledger_conn.cursor() as cursor:
        cursor.execute("SELECT 1")
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


# --- apply ---------------------------------------------------------------------------------

ADD_NOTE = "ALTER TABLE nozdormu_test.t ADD COLUMN note text"
EDITED = Changeset("app", [SCHEMA, Step("table", ADD_NOTE, check=TABLE.check), INDEX])


def _applied_prefix(conn: Any) -> None:
    """Install, then apply the first two steps of `APP`."""
    install(conn)
    apply(conn, Changeset("app", [SCHEMA, TABLE]))


def _columns(conn: Any) -> list[str]:
    rows = _query(conn, "SELECT attname FROM pg_attribute WHERE attrelid = 'nozdormu_test.t'::regclass AND attnum > 0")
    return [row[0] for row in rows]


def test_apply_refuses_a_ledger_that_is_not_installed(ledger_conn: Any) -> None:
    with pytest.raises(LedgerNotInstalled):
        apply(ledger_conn, APP)
    _assert_idle(ledger_conn)


def test_apply_refuses_the_reserved_changeset_name(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match="reserved"):
        apply(ledger_conn, Changeset("caverns", [SCHEMA]))


def test_apply_refuses_a_connection_with_autocommit_on(ledger_conn: Any) -> None:
    install(ledger_conn)
    ledger_conn.autocommit = True
    try:
        with pytest.raises(CavernsError, match="autocommit"):
            apply(ledger_conn, APP)
    finally:
        ledger_conn.autocommit = False


def test_apply_runs_a_whole_changeset_on_an_empty_database(ledger_conn: Any) -> None:
    install(ledger_conn)
    entries = apply(ledger_conn, APP)
    assert [(e.changeset, e.label, e.outcome) for e in entries] == [("app", s.label, "applied") for s in APP.steps]
    assert [e.version for e in entries] == [4, 5, 6]
    assert [e.checksum for e in entries] == [s.checksum for s in APP.steps]
    assert all(e.duration_ms is not None and e.duration_ms >= 0 and e.error is None for e in entries)
    assert _exists(ledger_conn, "nozdormu_test.t") and _exists(ledger_conn, "nozdormu_test.t_id")
    assert set(_statuses(ledger_conn).values()) == {Status.APPLIED}


def test_applying_twice_changes_nothing_the_second_time(ledger_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, APP)
    before = _query(ledger_conn, LEDGER_ROWS)
    assert apply(ledger_conn, APP) == []
    assert _query(ledger_conn, LEDGER_ROWS) == before


def test_apply_runs_only_the_steps_that_are_pending(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    assert [e.label for e in apply(ledger_conn, APP)] == ["index"]


def test_apply_refuses_an_unacknowledged_conflict_and_writes_nothing(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    with pytest.raises(PreflightRefused) as refused:
        apply(ledger_conn, EDITED)
    assert [(s.step.label, s.status) for s in refused.value.statuses] == [("table", Status.CONFLICT)]
    _assert_idle(ledger_conn)
    assert "table (conflict)" in str(refused.value)
    assert not _exists(ledger_conn, "nozdormu_test.t_id")
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied")]


def test_apply_refuses_an_unacknowledged_unknown_step_and_writes_nothing(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    with pytest.raises(PreflightRefused) as refused:
        apply(ledger_conn, APP)
    _assert_idle(ledger_conn)
    assert [(s.step.label, s.status) for s in refused.value.statuses] == [("schema", Status.UNKNOWN)]
    assert _query(ledger_conn, APP_ROWS) == []
    assert not _exists(ledger_conn, "nozdormu_test.t")


def test_reapply_runs_the_edited_sql_and_records_a_new_row(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    entries = apply(ledger_conn, EDITED, reapply=["table"])
    assert [(e.label, e.outcome) for e in entries] == [("table", "applied"), ("index", "applied")]
    assert "note" in _columns(ledger_conn)
    assert _query(ledger_conn, APP_ROWS)[:3] == [("schema", "applied"), ("table", "applied"), ("table", "applied")]
    assert set(_statuses(ledger_conn, EDITED).values()) == {Status.APPLIED}


def test_accept_records_the_edited_step_without_running_it(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    entries = apply(ledger_conn, EDITED, accept=["table"])
    assert [(e.label, e.outcome) for e in entries] == [("table", "accepted"), ("index", "applied")]
    assert entries[0].duration_ms is None and entries[0].checksum == EDITED.steps[1].checksum
    assert "note" not in _columns(ledger_conn)
    assert set(_statuses(ledger_conn, EDITED).values()) == {Status.APPLIED}


def test_accept_covers_a_step_whose_effect_is_already_there(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    entries = apply(ledger_conn, APP, accept=["schema"])
    assert [(e.label, e.outcome) for e in entries] == [
        ("schema", "accepted"),
        ("table", "applied"),
        ("index", "applied"),
    ]


@pytest.mark.parametrize(
    ("kwargs", "labels"),
    [
        ({"reapply": ["schema"]}, ["schema"]),  # pending
        ({"accept": ["schema"]}, ["schema"]),  # pending
        ({"reapply": ["table"]}, ["table"]),  # applied
        ({"accept": ["table"]}, ["table"]),  # applied
    ],
)
def test_apply_refuses_to_acknowledge_a_step_that_is_not_in_conflict(
    ledger_conn: Any, kwargs: dict[str, list[str]], labels: list[str]
) -> None:
    install(ledger_conn)
    if "table" in labels:
        apply(ledger_conn, Changeset("app", [SCHEMA, TABLE]))
    before = _query(ledger_conn, LEDGER_ROWS)
    with pytest.raises(PreflightRefused) as refused:
        apply(ledger_conn, APP, **kwargs)
    _assert_idle(ledger_conn)
    assert [s.step.label for s in refused.value.statuses] == labels
    assert _query(ledger_conn, LEDGER_ROWS) == before


def test_reapply_refuses_a_step_that_is_unknown(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    with pytest.raises(PreflightRefused, match=r"schema \(unknown\)"):
        apply(ledger_conn, APP, reapply=["schema"])
    _assert_idle(ledger_conn)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reapply": ["ghost"]}, "'ghost'"),
        ({"accept": ["ghost"]}, "'ghost'"),
        ({"reapply": ["table"], "accept": ["table"]}, "both"),
        ({"accept": "table"}, "not the string"),
    ],
)
def test_apply_refuses_a_bad_acknowledgement(ledger_conn: Any, kwargs: dict[str, Any], message: str) -> None:
    _applied_prefix(ledger_conn)
    with pytest.raises(ChangesetError, match=message):
        apply(ledger_conn, EDITED, **kwargs)
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied")]


READ_ONLY_SESSION_SQL = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
READ_WRITE_SESSION_SQL = "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE"
BAD = Step(
    "bad",
    "CREATE TABLE nozdormu_test.bad (id int); SELECT 1 / 0",
    check="SELECT to_regclass('nozdormu_test.bad') IS NOT NULL",
)
LATER = Step("later", "CREATE TABLE nozdormu_test.later (id int)")
FAILING = Changeset("app", [SCHEMA, BAD, LATER])


def test_a_failing_step_is_rolled_back_recorded_and_raised(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, FAILING)
    _assert_idle(ledger_conn)
    assert (failed.value.label, failed.value.version) == ("bad", 5)
    assert isinstance(failed.value.__cause__, DRIVER_ERRORS)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("bad", "failed")]
    row = _query(ledger_conn, "SELECT version, error, duration_ms FROM caverns.ledger WHERE outcome = 'failed'")[0]
    assert row[0] == 5 and "division by zero" in row[1] and row[2] >= 0
    assert not _exists(ledger_conn, "nozdormu_test.bad")  # rolled back with its transaction
    assert not _exists(ledger_conn, "nozdormu_test.later")  # later steps did not run
    assert _statuses(ledger_conn, FAILING) == {"schema": Status.APPLIED, "bad": Status.PENDING, "later": Status.PENDING}


def test_a_failed_step_can_be_fixed_and_applied(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(StepFailed):
        apply(ledger_conn, FAILING)
    fixed = Changeset("app", [SCHEMA, Step("bad", "CREATE TABLE nozdormu_test.bad (id int)"), LATER])
    assert [(e.label, e.outcome) for e in apply(ledger_conn, fixed)] == [("bad", "applied"), ("later", "applied")]


def test_a_failing_own_step_raises_a_caverns_error_and_records_nothing(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = Changeset("caverns", [Step("ledger", "SELECT 1/0", check=nozdormu._LEDGER_EXISTS_SQL)])
    monkeypatch.setattr(nozdormu, "_OWN_CHANGESET", failing)
    with pytest.raises(CavernsError, match="could not be recorded") as refused:
        install(ledger_conn)
    _assert_idle(ledger_conn)
    assert not isinstance(refused.value, StepFailed)
    assert isinstance(refused.value.__cause__, DRIVER_ERRORS) and "division by zero" in str(refused.value.__cause__)
    assert not _exists(ledger_conn, "caverns.ledger")  # the connection is usable and nothing was written


def test_a_failure_that_cannot_be_recorded_raises_a_caverns_error(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, "CREATE SCHEMA nozdormu_test")
    _execute(ledger_conn, READ_ONLY_SESSION_SQL)
    try:
        with pytest.raises(CavernsError, match="could not be recorded") as refused:
            apply(ledger_conn, Changeset("app", [Step("ro", "CREATE TABLE nozdormu_test.ro_t ()")]))
    finally:
        _execute(ledger_conn, READ_WRITE_SESSION_SQL)
    _assert_idle(ledger_conn)
    assert not isinstance(refused.value, StepFailed)
    assert isinstance(refused.value.__cause__, DRIVER_ERRORS)
    assert _query(ledger_conn, APP_ROWS) == []
    assert not _exists(ledger_conn, "nozdormu_test.ro_t")


def test_apply_skips_a_step_another_runner_applied_after_preflight(ledger_conn: Any) -> None:
    install(ledger_conn)
    stale = preflight(ledger_conn, APP)
    _execute(ledger_conn, SCHEMA.sql)  # the other runner's work; running it again would fail
    _record(ledger_conn, SCHEMA)
    entries = nozdormu._apply_statuses(ledger_conn, APP, stale, frozenset(), frozenset(), record_fingerprint=False)
    assert [e.label for e in entries] == ["table", "index"]
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied"), ("index", "applied")]


def test_apply_refuses_a_step_that_changed_after_preflight(ledger_conn: Any) -> None:
    install(ledger_conn)
    stale = preflight(ledger_conn, APP)
    _record(ledger_conn, SCHEMA, checksum="0" * 64)  # another runner applied another version
    with pytest.raises(PreflightRefused, match=r"schema \(conflict\)"):
        nozdormu._apply_statuses(ledger_conn, APP, stale, frozenset(), frozenset(), record_fingerprint=False)
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied")]


RACE_STEP = Step("race", "CREATE TABLE nozdormu_test.race_t ()")
RACE = Changeset("race", [RACE_STEP])
RACE_ROWS = "SELECT label, outcome FROM caverns.ledger WHERE changeset = 'race' ORDER BY version"
REPEATABLE_READ_SQL = "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ"
# pg_locks shows a bigint advisory key as its high (classid) and low (objid) 32 bits.
LOCK_KEY_HIGH = _connection.ADVISORY_LOCK_KEY >> 32
LOCK_KEY_LOW = _connection.ADVISORY_LOCK_KEY & 0xFFFFFFFF
WAITING_LOCKS_SQL = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid = %s AND objid = %s"
)
WAIT_SECONDS = 10.0
POLL_SECONDS = 0.02


def _wait_for_a_waiting_lock(conn: Any) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    with conn.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(WAITING_LOCKS_SQL, (LOCK_KEY_HIGH, LOCK_KEY_LOW))
            if cursor.fetchone()[0] == 1:
                return
            time.sleep(POLL_SECONDS)
    pytest.fail(f"no runner waited for the advisory lock within {WAIT_SECONDS} s")


def test_a_runner_waits_for_the_lock_and_reads_the_ledger_after_it(ledger_conn: Any, second_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    _execute(ledger_conn, REPEATABLE_READ_SQL)  # a snapshot taken before the wait would miss the other runner
    with second_conn.cursor() as cursor:
        _connection.lock(cursor)
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["entries"] = apply(ledger_conn, RACE)
        except BaseException as error:  # reported by the assertions below
            result["error"] = error

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    try:
        _wait_for_a_waiting_lock(second_conn)
        with second_conn.cursor() as cursor:  # the other runner's work, under the lock it holds
            cursor.execute(RACE_STEP.sql)
            nozdormu._insert(cursor, RACE.name, RACE_STEP, "applied")
        second_conn.commit()
    finally:
        second_conn.rollback()  # releases the lock when the test failed before the commit
        runner.join(WAIT_SECONDS)
    assert not runner.is_alive()
    assert result == {"entries": []}
    assert _query(ledger_conn, RACE_ROWS) == [("race", "applied")]


def test_a_percent_sign_in_a_step_is_literal_on_both_drivers(ledger_conn: Any) -> None:
    percent = Step("percent", "CREATE TABLE nozdormu_test.pct AS SELECT 'a%s' AS v, 100 % 7 AS m")
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA, percent]))
    assert _query(ledger_conn, "SELECT v, m FROM nozdormu_test.pct") == [("a%s", 2)]


def test_a_step_with_several_statements_applies(ledger_conn: Any) -> None:
    several = Step(
        "several",
        "CREATE TABLE nozdormu_test.one (id int); CREATE TABLE nozdormu_test.two (id int);\n"
        "INSERT INTO nozdormu_test.two VALUES (1);",
    )
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA, several]))
    assert _query(ledger_conn, "SELECT count(*) FROM nozdormu_test.two") == [(1,)]


# --- baseline ------------------------------------------------------------------------------

FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def _existing_database(conn: Any) -> None:
    """The objects of the first two steps of `APP`, made by hand: a database that predates the ledger."""
    _execute(conn, SCHEMA.sql)
    _execute(conn, TABLE.sql)


def test_baseline_refuses_a_ledger_that_is_not_installed(ledger_conn: Any) -> None:
    with pytest.raises(LedgerNotInstalled):
        baseline(ledger_conn, APP)
    _assert_idle(ledger_conn)


def test_baseline_refuses_the_reserved_changeset_name(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match="reserved"):
        baseline(ledger_conn, Changeset("caverns", [SCHEMA]))


def test_baseline_refuses_a_connection_with_autocommit_on(ledger_conn: Any) -> None:
    install(ledger_conn)
    ledger_conn.autocommit = True
    try:
        with pytest.raises(CavernsError, match="autocommit"):
            baseline(ledger_conn, APP)
    finally:
        ledger_conn.autocommit = False


def test_baseline_records_one_row_for_the_last_step_and_runs_nothing(ledger_conn: Any) -> None:
    install(ledger_conn)
    entry = baseline(ledger_conn, APP)
    assert (entry.changeset, entry.label, entry.outcome) == ("app", "index", "baseline")
    assert entry.checksum == INDEX.checksum
    assert FINGERPRINT.fullmatch(entry.fingerprint or "")
    assert (entry.duration_ms, entry.error, entry.caverns_version) == (None, None, __version__)
    assert _query(ledger_conn, APP_ROWS) == [("index", "baseline")]
    assert not _exists(ledger_conn, "nozdormu_test.t")
    assert set(_statuses(ledger_conn).values()) == {Status.APPLIED}
    assert apply(ledger_conn, APP) == []


def test_baseline_through_a_step_leaves_the_later_steps_pending(ledger_conn: Any) -> None:
    install(ledger_conn)
    _existing_database(ledger_conn)
    entry = baseline(ledger_conn, APP, through="table")
    assert (entry.label, entry.checksum) == ("table", TABLE.checksum)
    assert _statuses(ledger_conn) == {"schema": Status.APPLIED, "table": Status.APPLIED, "index": Status.PENDING}
    assert [(e.label, e.outcome) for e in apply(ledger_conn, APP)] == [("index", "applied")]


def test_baseline_refuses_a_step_the_changeset_lacks(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match="'ghost'"):
        baseline(ledger_conn, APP, through="ghost")
    assert _query(ledger_conn, APP_ROWS) == []


def test_a_second_baseline_is_refused(ledger_conn: Any) -> None:
    install(ledger_conn)
    baseline(ledger_conn, APP)
    with pytest.raises(BaselineRefused, match="already has ledger rows"):
        baseline(ledger_conn, APP, through="table")
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == [("index", "baseline")]


def test_a_baseline_is_refused_for_a_changeset_that_was_applied(ledger_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA]))
    with pytest.raises(BaselineRefused):
        baseline(ledger_conn, APP)
    _assert_idle(ledger_conn)


def test_another_changeset_does_not_block_a_baseline(ledger_conn: Any) -> None:
    install(ledger_conn)
    baseline(ledger_conn, Changeset("other", [SCHEMA]))
    assert baseline(ledger_conn, APP).changeset == "app"


def test_baseline_refuses_a_schema_that_does_not_exist(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(BaselineRefused, match="'nozdormu_missing'"):
        baseline(ledger_conn, APP, schemas=["public", "nozdormu_missing"])
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == []


@pytest.mark.parametrize(("schemas", "message"), [([], "empty"), ("public", "not the string")])
def test_baseline_refuses_schemas_that_are_not_a_list_of_names(ledger_conn: Any, schemas: Any, message: str) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match=message):
        baseline(ledger_conn, APP, schemas=schemas)


def _fingerprint(conn: Any, name: str, **kwargs: Any) -> str:
    entry = baseline(conn, Changeset(name, [SCHEMA]), **kwargs)
    assert entry.fingerprint is not None
    return entry.fingerprint


def test_the_fingerprint_is_stable_and_follows_the_tables(ledger_conn: Any) -> None:
    install(ledger_conn)
    _existing_database(ledger_conn)
    first = _fingerprint(ledger_conn, "one", schemas=["nozdormu_test"])
    assert _fingerprint(ledger_conn, "two", schemas=["nozdormu_test"]) == first
    _execute(ledger_conn, ADD_NOTE)
    with_column = _fingerprint(ledger_conn, "three", schemas=["nozdormu_test"])
    assert with_column != first
    _execute(ledger_conn, "CREATE TABLE nozdormu_test.bare ()")  # a table without columns still counts
    with_bare = _fingerprint(ledger_conn, "four", schemas=["nozdormu_test"])
    assert with_bare != with_column
    _execute(ledger_conn, INDEX.sql)  # the widened fingerprint of Infinite sees an index too
    assert _fingerprint(ledger_conn, "five", schemas=["nozdormu_test"]) != with_bare


def test_the_fingerprint_does_not_depend_on_the_search_path(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    _execute(ledger_conn, "CREATE TABLE nozdormu_test.t (id serial)")
    first = _fingerprint(ledger_conn, "fp_a", schemas=["nozdormu_test"])
    _execute(ledger_conn, "SET search_path = nozdormu_test, public")
    try:
        second = _fingerprint(ledger_conn, "fp_b", schemas=["nozdormu_test"])
    finally:
        _execute(ledger_conn, "RESET search_path")
    assert second == first


def test_the_fingerprint_ignores_a_schema_that_was_not_asked_for(ledger_conn: Any) -> None:
    install(ledger_conn)
    _existing_database(ledger_conn)
    public_only = _fingerprint(ledger_conn, "one", schemas=["public"])
    _execute(ledger_conn, ADD_NOTE)
    assert _fingerprint(ledger_conn, "two", schemas=["public"]) == public_only


def test_the_default_schemas_are_every_one_but_the_system_ones_and_caverns(ledger_conn: Any) -> None:
    install(ledger_conn)
    _existing_database(ledger_conn)
    names = _query(
        ledger_conn,
        "SELECT nspname::text FROM pg_namespace"
        " WHERE nspname NOT LIKE 'pg\\_%' AND nspname NOT IN ('information_schema', 'caverns')",
    )
    default = _fingerprint(ledger_conn, "one")
    assert default == _fingerprint(ledger_conn, "two", schemas=[row[0] for row in names])
    assert default != _fingerprint(ledger_conn, "three", schemas=[row[0] for row in names] + ["caverns"])


# --- the fingerprint recorded with apply and baseline ---------------------------------------


def test_apply_records_one_fingerprint_at_the_last_ledger_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    entries = apply(ledger_conn, FIRST_TWO)
    assert [e.version for e in entries] == [4, 5]
    live = take_fingerprint(ledger_conn)
    assert _query(ledger_conn, FINGERPRINT_ROWS) == [(5, list(live.schemas), live.hash)]
    assert _query(ledger_conn, "SELECT document FROM caverns.fingerprint") == [(live.document,)]
    assert "nozdormu_test" in live.schemas and live.document["nozdormu_test"]["table"] == {"t": {"kind": "r"}}


def test_a_run_with_nothing_to_do_records_no_fingerprint_and_no_warning(
    ledger_conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    install(ledger_conn)
    apply(ledger_conn, FIRST_TWO)
    before = _query(ledger_conn, FINGERPRINT_ROWS)
    _execute(ledger_conn, ADD_NOTE)  # drift, but a run that writes nothing does not look for it
    with caplog.at_level(logging.WARNING):
        assert apply(ledger_conn, FIRST_TWO) == []
    assert _query(ledger_conn, FINGERPRINT_ROWS) == before
    assert "drift before" not in caplog.text


def test_a_failing_step_records_the_fingerprint_with_its_failed_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, FAILING)
    assert failed.value.version == 5
    live = take_fingerprint(ledger_conn)  # the failed step was rolled back: this is what the first step left
    assert _query(ledger_conn, FINGERPRINT_ROWS) == [(5, list(live.schemas), live.hash)]
    assert "nozdormu_test" in live.schemas and live.document["nozdormu_test"]["table"] == {}


def test_a_failure_that_cannot_be_recorded_records_no_fingerprint(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, "CREATE SCHEMA nozdormu_test")
    _execute(ledger_conn, READ_ONLY_SESSION_SQL)
    try:
        with pytest.raises(CavernsError, match="could not be recorded"):
            apply(ledger_conn, Changeset("app", [Step("ro", "CREATE TABLE nozdormu_test.ro_t ()")]))
    finally:
        _execute(ledger_conn, READ_WRITE_SESSION_SQL)
    assert _query(ledger_conn, FINGERPRINT_ROWS) == []


def test_a_baseline_records_its_fingerprint_at_its_version(ledger_conn: Any) -> None:
    install(ledger_conn)
    _existing_database(ledger_conn)
    entry = baseline(ledger_conn, APP, through="table")
    assert entry.fingerprint is not None
    live = take_fingerprint(ledger_conn)
    assert _query(ledger_conn, FINGERPRINT_ROWS) == [(entry.version, list(live.schemas), entry.fingerprint)]
    assert entry.fingerprint == live.hash


def test_apply_after_a_baseline_with_schemas_records_only_those_schemas(ledger_conn: Any) -> None:
    install(ledger_conn)
    _existing_database(ledger_conn)
    first = baseline(ledger_conn, APP, through="table", schemas=["nozdormu_test"])
    (second,) = apply(ledger_conn, APP)
    rows = _query(ledger_conn, FINGERPRINT_ROWS)
    assert [(version, schemas) for version, schemas, _ in rows] == [
        (first.version, ["nozdormu_test"]),
        (second.version, ["nozdormu_test"]),  # not the default schemas: the baseline's choice stands
    ]
    assert rows[1][2] == take_fingerprint(ledger_conn, schemas=["nozdormu_test"]).hash


def test_history_refuses_a_limit_above_the_bigint_maximum(ledger_conn: Any) -> None:
    install(ledger_conn)
    assert history(ledger_conn, limit=2**63 - 1) == history(ledger_conn)
    with pytest.raises(ChangesetError, match="positive integer up to 9223372036854775807, not 9223372036854775808"):
        history(ledger_conn, limit=2**63)
    _assert_idle(ledger_conn)


def test_apply_warns_of_drift_before_it_runs(ledger_conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    install(ledger_conn)
    with caplog.at_level(logging.WARNING):
        apply(ledger_conn, FIRST_TWO)  # no fingerprint yet, so nothing to compare with
        _execute(ledger_conn, ADD_NOTE)
        assert "drift before" not in caplog.text
        apply(ledger_conn, APP)
    warnings = [r.getMessage() for r in caplog.records if r.name == "caverns_of_time.nozdormu"]
    assert warnings == ["drift before applying changeset app: added column nozdormu_test.t.note"]
    assert [row[0] for row in _query(ledger_conn, FINGERPRINT_ROWS)] == [5, 6]  # the run went ahead
    caplog.clear()
    with caplog.at_level(logging.WARNING):  # the new fingerprint holds the column: no difference left
        apply(ledger_conn, Changeset("app", [*APP.steps, Step("more", "CREATE TABLE nozdormu_test.more ()")]))
    assert "drift before" not in caplog.text


# --- history -------------------------------------------------------------------------------


def _history_ledger(conn: Any) -> None:
    """The ledger with its own rows (1 to 3), then app's three steps (4 to 6) and one other changeset's row."""
    install(conn)
    apply(conn, APP)
    _execute(
        conn,
        "INSERT INTO caverns.ledger (changeset, label, checksum, outcome, caverns_version)"
        " VALUES ('other', 'x', %s, 'applied', 'test')",
        (SCHEMA.checksum,),
    )


def test_history_is_the_whole_ledger_newest_first(ledger_conn: Any) -> None:
    _history_ledger(ledger_conn)
    entries = history(ledger_conn)
    assert [e.version for e in entries] == [7, 6, 5, 4, 3, 2, 1]
    assert [(e.changeset, e.label) for e in entries] == [
        ("other", "x"), ("app", "index"), ("app", "table"), ("app", "schema"),
        ("caverns", "epochs"), ("caverns", "fingerprint"), ("caverns", "ledger"),
    ]  # fmt: skip
    _assert_idle(ledger_conn)


def test_history_of_one_changeset(ledger_conn: Any) -> None:
    _history_ledger(ledger_conn)
    assert [e.label for e in history(ledger_conn, changeset="app")] == ["index", "table", "schema"]
    assert [e.label for e in history(ledger_conn, changeset="caverns")] == ["epochs", "fingerprint", "ledger"]
    assert history(ledger_conn, changeset="nobody") == []


def test_history_limit_keeps_the_newest_rows(ledger_conn: Any) -> None:
    _history_ledger(ledger_conn)
    assert [e.version for e in history(ledger_conn, limit=2)] == [7, 6]
    assert [e.version for e in history(ledger_conn, changeset="app", limit=1)] == [6]
    assert len(history(ledger_conn, limit=100)) == 7


def test_history_refuses_a_ledger_that_is_not_installed(ledger_conn: Any) -> None:
    with pytest.raises(LedgerNotInstalled, match="call install"):
        history(ledger_conn)
    _assert_idle(ledger_conn)


@pytest.mark.parametrize("limit", [0, -1, True, "3", 1.5])
def test_history_refuses_a_limit_that_is_not_a_positive_integer(ledger_conn: Any, limit: Any) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match="limit must be a positive integer"):
        history(ledger_conn, limit=limit)
    _assert_idle(ledger_conn)


@pytest.mark.parametrize("name", ["", " app", "a\nb", 3])
def test_history_refuses_an_invalid_changeset_name(ledger_conn: Any, name: Any) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match="changeset name"):
        history(ledger_conn, changeset=name)
    _assert_idle(ledger_conn)


def test_history_writes_nothing(ledger_conn: Any) -> None:
    _history_ledger(ledger_conn)
    before = _query(ledger_conn, LEDGER_ROWS)
    history(ledger_conn, changeset="app", limit=2)
    assert _query(ledger_conn, LEDGER_ROWS) == before
    _assert_idle(ledger_conn)
