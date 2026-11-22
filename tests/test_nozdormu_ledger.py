"""Nozdormu against PostgreSQL 18, on both supported drivers: the ledger and what writes it."""

from __future__ import annotations

from typing import Any

import psycopg
import psycopg2
import pytest

from caverns_of_time import __version__, nozdormu
from caverns_of_time.errors import CavernsError, ChangesetError, LedgerNotInstalled, PreflightRefused, StepFailed
from caverns_of_time.nozdormu import Changeset, Status, Step, apply, install, preflight

DRIVER_ERRORS = (psycopg.Error, psycopg2.Error)
SCHEMA = Step("schema", "CREATE SCHEMA nozdormu_test", check="SELECT to_regnamespace('nozdormu_test') IS NOT NULL")
TABLE = Step(
    "table", "CREATE TABLE nozdormu_test.t (id int)", check="SELECT to_regclass('nozdormu_test.t') IS NOT NULL"
)
INDEX = Step("index", "CREATE INDEX t_id ON nozdormu_test.t (id)")
APP = Changeset("app", [SCHEMA, TABLE, INDEX])
APP_ROWS = "SELECT label, outcome FROM caverns.ledger WHERE changeset = 'app' ORDER BY version"
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


def _exists(conn: Any, name: str) -> bool:
    return bool(_query(conn, "SELECT to_regclass(%s) IS NOT NULL", (name,))[0][0])


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
    assert [e.version for e in entries] == [2, 3, 4]
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
    assert "table (conflict)" in str(refused.value)
    assert not _exists(ledger_conn, "nozdormu_test.t_id")
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied")]


def test_apply_refuses_an_unacknowledged_unknown_step_and_writes_nothing(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    with pytest.raises(PreflightRefused) as refused:
        apply(ledger_conn, APP)
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
    assert [s.step.label for s in refused.value.statuses] == labels
    assert _query(ledger_conn, LEDGER_ROWS) == before


def test_reapply_refuses_a_step_that_is_unknown(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    with pytest.raises(PreflightRefused, match=r"schema \(unknown\)"):
        apply(ledger_conn, APP, reapply=["schema"])


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
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied")]


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
    assert (failed.value.label, failed.value.version) == ("bad", 3)
    assert isinstance(failed.value.__cause__, DRIVER_ERRORS)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("bad", "failed")]
    row = _query(ledger_conn, "SELECT version, error, duration_ms FROM caverns.ledger WHERE outcome = 'failed'")[0]
    assert row[0] == 3 and "division by zero" in row[1] and row[2] >= 0
    assert not _exists(ledger_conn, "nozdormu_test.bad")  # rolled back with its transaction
    assert not _exists(ledger_conn, "nozdormu_test.later")  # later steps did not run
    assert _statuses(ledger_conn, FAILING) == {"schema": Status.APPLIED, "bad": Status.PENDING, "later": Status.PENDING}


def test_a_failed_step_can_be_fixed_and_applied(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(StepFailed):
        apply(ledger_conn, FAILING)
    fixed = Changeset("app", [SCHEMA, Step("bad", "CREATE TABLE nozdormu_test.bad (id int)"), LATER])
    assert [(e.label, e.outcome) for e in apply(ledger_conn, fixed)] == [("bad", "applied"), ("later", "applied")]


def test_apply_skips_a_step_another_runner_applied_after_preflight(ledger_conn: Any) -> None:
    install(ledger_conn)
    stale = preflight(ledger_conn, APP)
    _execute(ledger_conn, SCHEMA.sql)  # the other runner's work; running it again would fail
    _record(ledger_conn, SCHEMA)
    entries = nozdormu._apply_statuses(ledger_conn, APP, stale, frozenset(), frozenset())
    assert [e.label for e in entries] == ["table", "index"]
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied"), ("index", "applied")]


def test_apply_refuses_a_step_that_changed_after_preflight(ledger_conn: Any) -> None:
    install(ledger_conn)
    stale = preflight(ledger_conn, APP)
    _record(ledger_conn, SCHEMA, checksum="0" * 64)  # another runner applied another version
    with pytest.raises(PreflightRefused, match=r"schema \(conflict\)"):
        nozdormu._apply_statuses(ledger_conn, APP, stale, frozenset(), frozenset())
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied")]


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
