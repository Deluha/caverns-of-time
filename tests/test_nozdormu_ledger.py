"""Nozdormu against PostgreSQL 18, on both supported drivers: the ledger and what writes it."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import psycopg2
import pytest

from caverns_of_time import __version__, _connection, epochs, infinite, murozond, nozdormu
from caverns_of_time import _fingerprint as fingerprint_store
from caverns_of_time.errors import (
    BaselineRefused,
    CavernsError,
    ChangesetError,
    DriftRefused,
    GuardRefused,
    LedgerNotInstalled,
    PreflightRefused,
    StepFailed,
)
from caverns_of_time.infinite import fingerprint as take_fingerprint
from caverns_of_time.nozdormu import Changeset, Status, Step, apply, baseline, history, install, preflight
from tests.conftest import RUNNER_GRANTS, WAIT_SECONDS, throwaway_roles, wait_for_a_waiting_lock

DRIVER_ERRORS = (psycopg.Error, psycopg2.Error)
SCHEMA = Step("schema", "CREATE SCHEMA nozdormu_test", check="SELECT to_regnamespace('nozdormu_test') IS NOT NULL")
TABLE = Step(
    "table", "CREATE TABLE nozdormu_test.t (id int)", check="SELECT to_regclass('nozdormu_test.t') IS NOT NULL"
)
# NOTE: Murozond calls an index on an existing table a lock hazard, one on a table the same step creates safe.
INDEX = Step("index", "CREATE TABLE nozdormu_test.u (id int); CREATE INDEX t_id ON nozdormu_test.u (id)")
APP = Changeset("app", [SCHEMA, TABLE, INDEX])
APP_ROWS = "SELECT label, outcome FROM caverns.ledger WHERE changeset = 'app' ORDER BY version"
LEDGER_ROWS = "SELECT version, changeset, label, outcome FROM caverns.ledger ORDER BY version"
# What `install` writes: one row for each step of Caverns' own changeset.
OWN_ROWS = [
    (1, "caverns", "ledger", "applied"),
    (2, "caverns", "fingerprint", "applied"),
    (3, "caverns", "epochs", "applied"),
    (4, "caverns", "privileges", "applied"),
    (5, "caverns", "fingerprint_server", "applied"),
    (6, "caverns", "acknowledgement", "applied"),
    (7, "caverns", "drift", "applied"),
]
FINGERPRINT_ROWS = "SELECT version, schemas, hash FROM caverns.fingerprint ORDER BY version"
FIRST_TWO = Changeset("app", [SCHEMA, TABLE])
# Steps Murozond calls safe: one that changes nothing, and one that waits for a lock on `t` but writes no row.
NOOP = Step("noop", "SET LOCAL application_name = 'noop'")
TOUCH = Step("touch", "INSERT INTO nozdormu_test.t SELECT 1 WHERE false")


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
        ("caverns", "privileges", "applied"),
        ("caverns", "fingerprint_server", "applied"),
        ("caverns", "acknowledgement", "applied"),
        ("caverns", "drift", "applied"),
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


# --- privileges, Caverns' fourth own step --------------------------------------------------

PUBLIC_ACL_ROWS = (
    "SELECT (SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) a"
    "  WHERE n.nspname = 'caverns' AND a.grantee = 0),"
    " (SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a"
    "  WHERE c.relnamespace = 'caverns'::regnamespace AND a.grantee = 0)"
)
PUBLIC_FUNCTIONS_SQL = (
    "SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'caverns'::regnamespace"
    " AND has_function_privilege('public', p.oid, 'EXECUTE') ORDER BY 1"
)
FUNCTIONS_SQL = "SELECT count(*) FROM pg_proc WHERE pronamespace = 'caverns'::regnamespace"


def _install_own_steps(conn: Any, patch: pytest.MonkeyPatch, count: int) -> None:
    """An installation of an earlier release: only the first *count* own steps."""
    with patch.context() as earlier:
        earlier.setattr(nozdormu, "_OWN_CHANGESET", Changeset("caverns", nozdormu._OWN_CHANGESET.steps[:count]))
        install(conn)


def _install_first_three_steps(conn: Any, patch: pytest.MonkeyPatch) -> None:
    """An installation of the release before `privileges`: the first three own steps."""
    _install_own_steps(conn, patch, 3)


def test_install_leaves_public_no_privilege_in_the_own_schema(ledger_conn: Any) -> None:
    install(ledger_conn)
    assert _query(ledger_conn, PUBLIC_ACL_ROWS) == [(0, 0)]  # the schema, and every table and sequence in it
    assert _query(ledger_conn, FUNCTIONS_SQL)[0][0] > 0  # the check below is not vacuous
    # A function is executable by PUBLIC unless revoked: a later own step that adds one must revoke it itself.
    assert _query(ledger_conn, PUBLIC_FUNCTIONS_SQL) == []
    assert _query(ledger_conn, nozdormu._PRIVILEGES_REVOKED_SQL) == [(True,)]


def test_the_privileges_check_is_false_without_the_schema_and_before_the_step(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _query(ledger_conn, nozdormu._PRIVILEGES_REVOKED_SQL) == [(False,)]  # false, not an error
    _install_first_three_steps(ledger_conn, monkeypatch)
    assert _query(ledger_conn, nozdormu._PRIVILEGES_REVOKED_SQL) == [(False,)]
    assert _query(ledger_conn, PUBLIC_FUNCTIONS_SQL) != []  # the default the step revokes


def test_an_installation_of_the_previous_release_gets_the_privileges_step_with_install(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_first_three_steps(ledger_conn, monkeypatch)
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS[:3]
    with pytest.raises(LedgerNotInstalled, match="call install"):
        preflight(ledger_conn, APP)
    _assert_idle(ledger_conn)
    assert [e.label for e in history(ledger_conn)] == ["epochs", "fingerprint", "ledger"]  # the old rows still read
    entries = install(ledger_conn)
    assert [(e.version, e.changeset, e.label, e.outcome) for e in entries] == OWN_ROWS[3:]
    assert entries[0].checksum == nozdormu._OWN_CHANGESET.steps[3].checksum
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _query(ledger_conn, PUBLIC_FUNCTIONS_SQL) == []
    assert install(ledger_conn) == []  # applying twice writes nothing the second time
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


def test_install_accepts_the_privileges_step_when_the_revocations_are_already_there(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_first_three_steps(ledger_conn, monkeypatch)
    _execute(ledger_conn, nozdormu._PRIVILEGES_DDL_SQL)  # a restore that kept the grants but not the row
    entries = install(ledger_conn)
    assert [(e.label, e.outcome) for e in entries] == [
        ("privileges", "accepted"),
        ("fingerprint_server", "applied"),
        ("acknowledgement", "applied"),
        ("drift", "applied"),
    ]


# What PUBLIC can do, spelled out: USAGE and CREATE on the schema, SELECT on a table, USAGE on a sequence.
PUBLIC_GRANTS_SQL = (
    "SELECT has_schema_privilege('public', 'caverns', 'USAGE'), has_schema_privilege('public', 'caverns', 'CREATE'),"
    " has_table_privilege('public', 'caverns.ledger', 'SELECT'),"
    " has_sequence_privilege('public', pg_get_serial_sequence('caverns.ledger', 'version'), 'USAGE')"
)


def test_install_revokes_what_public_was_granted_before_the_privileges_step(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_first_three_steps(ledger_conn, monkeypatch)
    _run(
        ledger_conn,
        "GRANT USAGE, CREATE ON SCHEMA caverns TO PUBLIC",
        "GRANT SELECT ON caverns.ledger TO PUBLIC",
        "DO $$ BEGIN EXECUTE format('GRANT USAGE ON SEQUENCE %s TO PUBLIC',"
        " pg_get_serial_sequence('caverns.ledger', 'version')); END $$",
    )
    assert _query(ledger_conn, PUBLIC_GRANTS_SQL) == [(True, True, True, True)]
    assert _query(ledger_conn, nozdormu._PRIVILEGES_REVOKED_SQL) == [(False,)]
    install(ledger_conn)
    assert _query(ledger_conn, PUBLIC_GRANTS_SQL) == [(False, False, False, False)]
    assert _query(ledger_conn, PUBLIC_ACL_ROWS) == [(0, 0)]


def _run(conn: Any, *statements: str) -> None:
    """Several statements, then one commit."""
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


@pytest.fixture
def roles(ledger_conn: Any) -> Iterator[tuple[str, str, str]]:
    """Three roles, dropped after: an owner, a stranger and a member of the owner (inheriting its privileges)."""
    with throwaway_roles(ledger_conn, ("owner", "NOLOGIN"), ("stranger", "NOLOGIN"), ("member", "NOLOGIN")) as made:
        owner, stranger, member = made
        _run(ledger_conn, f"GRANT {owner} TO {member}")
        # Schema creation checks the database privilege even when the schema exists.
        _run(
            ledger_conn,
            "DO $$ BEGIN EXECUTE format('GRANT CREATE ON DATABASE %I TO "
            + ", ".join((stranger, member))
            + "', current_database()); END $$",
        )
        yield owner, stranger, member


def test_install_refuses_a_schema_owned_by_a_role_the_caller_is_not_a_member_of(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    owner, stranger, _ = roles
    _run(ledger_conn, f"CREATE SCHEMA caverns AUTHORIZATION {owner}", f"SET ROLE {stranger}")
    with pytest.raises(CavernsError) as refused:
        install(ledger_conn)
    assert str(refused.value) == (
        f"schema caverns is owned by role {owner}, whose privileges role {stranger} does not inherit: "
        f"run install as {owner} or a member that inherits its privileges"
    )
    _assert_idle(ledger_conn)
    _run(ledger_conn, "RESET ROLE")
    assert not _exists(ledger_conn, "caverns.ledger")  # nothing was written


def test_install_refuses_a_relation_owned_by_a_role_the_caller_is_not_a_member_of(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    _, _, member = roles
    _run(ledger_conn, f"CREATE SCHEMA caverns AUTHORIZATION {member}", "CREATE TABLE caverns.stray (id int)")
    _run(ledger_conn, f"SET ROLE {member}")
    with pytest.raises(
        CavernsError, match="relation caverns.stray is owned by role .*, whose privileges role .* does not inherit"
    ):
        install(ledger_conn)
    _run(ledger_conn, "RESET ROLE")
    assert not _exists(ledger_conn, "caverns.ledger")


def test_install_refuses_an_owner_whose_privileges_the_caller_does_not_inherit(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    """A member that does not inherit the owner's privileges cannot REVOKE or ALTER: refused like a stranger."""
    owner, stranger, _ = roles
    _run(
        ledger_conn,
        f"CREATE SCHEMA caverns AUTHORIZATION {owner}",
        f"GRANT {owner} TO {stranger} WITH INHERIT FALSE",
        f"SET ROLE {stranger}",
    )
    with pytest.raises(CavernsError) as refused:
        install(ledger_conn)
    assert str(refused.value) == (
        f"schema caverns is owned by role {owner}, whose privileges role {stranger} does not inherit: "
        f"run install as {owner} or a member that inherits its privileges"
    )
    _assert_idle(ledger_conn)
    _run(ledger_conn, "RESET ROLE")
    assert not _exists(ledger_conn, "caverns.ledger")  # nothing was written


def test_install_accepts_a_schema_owned_by_a_role_the_caller_is_a_member_of(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    owner, _, member = roles
    _run(ledger_conn, f"CREATE SCHEMA caverns AUTHORIZATION {owner}", f"SET ROLE {member}")
    entries = install(ledger_conn)
    assert [e.label for e in entries] == [label for _, _, label, _ in OWN_ROWS]
    assert {e.changeset for e in entries} == {"caverns"}
    _run(ledger_conn, "RESET ROLE")
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _query(ledger_conn, PUBLIC_ACL_ROWS) == [(0, 0)]
    assert _query(ledger_conn, "SELECT DISTINCT recorded_by FROM caverns.ledger") == [(member,)]


def test_a_runner_role_with_the_documented_grants_applies_and_snapshots(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    """The grants of the README suffice for every write after `install`; an identity sequence needs none."""
    _, _, runner = roles
    install(ledger_conn)
    _run(
        ledger_conn,
        "CREATE SCHEMA nozdormu_test",
        "CREATE TABLE nozdormu_test.t (id int PRIMARY KEY)",
        "INSERT INTO nozdormu_test.t VALUES (1)",
        f"GRANT USAGE ON SCHEMA nozdormu_test TO {runner}",
        f"GRANT SELECT ON nozdormu_test.t TO {runner}",
        *(grant.format(role=runner) for grant in RUNNER_GRANTS),
        f"SET ROLE {runner}",
    )
    assert [e.label for e in apply(ledger_conn, Changeset("app", [NOOP]))] == ["noop"]
    epochs.register(ledger_conn, "nozdormu_test", "t")
    epochs.snapshot(ledger_conn, "first")
    epochs.snapshot(ledger_conn, "second")
    assert epochs.as_of(ledger_conn, "first", "nozdormu_test", "t").rows == ({"id": 1},)
    assert [e.name for e in epochs.delete(ledger_conn, ["first"])] == ["first"]
    epochs.unregister(ledger_conn, "nozdormu_test", "t")
    _run(ledger_conn, "RESET ROLE")
    assert _query(ledger_conn, "SELECT DISTINCT recorded_by FROM caverns.ledger WHERE changeset = 'app'") == [(runner,)]


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
        (4, "privileges", "applied"),
        (5, "fingerprint_server", "applied"),
        (6, "acknowledgement", "applied"),
        (7, "drift", "applied"),
    ]
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _exists(ledger_conn, "caverns.fingerprint")
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE caverns.fingerprint SET hash = hash",
        "DELETE FROM caverns.fingerprint",
        "TRUNCATE caverns.fingerprint CASCADE",  # `caverns.drift` references it: CASCADE reaches the trigger
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


# --- fingerprint_server, Caverns' fifth own step -------------------------------------------

SERVER_VERSION_NUM = "SELECT current_setting('server_version_num')::integer"
RECORDED_SERVER_VERSIONS = "SELECT version, server_version FROM caverns.fingerprint ORDER BY version"


def _latest(conn: Any) -> tuple[int, Any] | None:
    """The newest recorded fingerprint as `latest` reads it."""
    with conn.cursor() as cursor:
        found = fingerprint_store.latest(cursor)
    conn.rollback()
    return found


def test_the_fingerprint_server_check_is_false_until_the_step_and_true_after(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = fingerprint_store.FINGERPRINT_SERVER_SQL
    assert _query(ledger_conn, check) == [(False,)]  # false, not an error, without the table
    _install_own_steps(ledger_conn, monkeypatch, 4)
    assert _query(ledger_conn, check) == [(False,)]
    install(ledger_conn)
    assert _query(ledger_conn, check) == [(True,)]
    assert _query(ledger_conn, nozdormu._PRIVILEGES_REVOKED_SQL) == [(True,)]  # an added column grants nothing
    assert _query(ledger_conn, PUBLIC_ACL_ROWS) == [(0, 0)]


def test_the_fingerprint_server_column_refuses_a_version_that_is_not_positive(ledger_conn: Any) -> None:
    install(ledger_conn)
    _refused(
        ledger_conn,
        "INSERT INTO caverns.fingerprint (version, schemas, document, hash, server_version)"
        " VALUES (1, '{}', '{}', %s, 0)",
        "violates check constraint",
        (ZEROS,),
    )


def test_a_capture_and_the_recorded_row_carry_the_servers_version(ledger_conn: Any) -> None:
    install(ledger_conn)
    server = _query(ledger_conn, SERVER_VERSION_NUM)[0][0]
    assert server >= 180000  # PostgreSQL 18, the version the tests run on
    assert take_fingerprint(ledger_conn).server_version == server
    apply(ledger_conn, FIRST_TWO)
    version, recorded = _latest(ledger_conn) or pytest.fail("no fingerprint was recorded")
    assert recorded.server_version == server
    assert _query(ledger_conn, RECORDED_SERVER_VERSIONS) == [(version, server)]
    baseline(ledger_conn, Changeset("other", [Step("x", "SELECT 1")]))
    assert [row[1] for row in _query(ledger_conn, RECORDED_SERVER_VERSIONS)] == [server, server]


def test_the_server_version_is_outside_the_hash(ledger_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, FIRST_TWO)
    recorded = _latest(ledger_conn)
    assert recorded is not None
    live = take_fingerprint(ledger_conn)
    assert (live.document, live.hash) == (recorded[1].document, recorded[1].hash)
    assert fingerprint_store.hash_document(live.document) == live.hash


def test_an_installation_of_the_previous_release_gets_the_fingerprint_server_step_with_install(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_own_steps(ledger_conn, monkeypatch, 4)
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS[:4]
    # A fingerprint recorded by that release: the columns it knew.
    _execute(
        ledger_conn,
        "INSERT INTO caverns.fingerprint (version, schemas, document, hash) VALUES (4, '{public}', '{}', %s)",
        (ZEROS,),
    )
    with pytest.raises(LedgerNotInstalled, match="call install"):
        preflight(ledger_conn, APP)
    with pytest.raises(LedgerNotInstalled, match="out of date.*call install"):
        infinite.drift(ledger_conn)
    _assert_idle(ledger_conn)
    entries = install(ledger_conn)
    assert [(e.version, e.changeset, e.label, e.outcome) for e in entries] == OWN_ROWS[
        4:
    ]  # exactly the steps from this one
    assert entries[0].checksum == nozdormu._OWN_CHANGESET.steps[4].checksum
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _query(ledger_conn, RECORDED_SERVER_VERSIONS) == [(4, None)]
    old = _latest(ledger_conn)
    assert old is not None and old[1].server_version is None and old[1].hash == ZEROS  # the old row reads back
    assert install(ledger_conn) == []  # installing twice writes nothing the second time
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    apply(ledger_conn, FIRST_TWO)  # a new apply records the version
    server = _query(ledger_conn, SERVER_VERSION_NUM)[0][0]
    assert _query(ledger_conn, RECORDED_SERVER_VERSIONS) == [(4, None), (9, server)]
    assert infinite.drift(ledger_conn).differences == ()


def test_install_accepts_the_fingerprint_server_step_when_the_column_is_already_there(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_own_steps(ledger_conn, monkeypatch, 4)
    _execute(ledger_conn, nozdormu._FINGERPRINT_SERVER_DDL_SQL)  # a restore that kept the column but not the row
    entries = install(ledger_conn)
    assert [(e.label, e.outcome) for e in entries] == [
        ("fingerprint_server", "accepted"),
        ("acknowledgement", "applied"),
        ("drift", "applied"),
    ]


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


def test_preflight_marks_a_step_before_the_baseline_that_has_no_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    _record(ledger_conn, TABLE, "baseline")  # one row only: the schema step has none of its own
    assert _statuses(ledger_conn) == {
        "schema": Status.BEFORE_BASELINE,
        "table": Status.APPLIED,
        "index": Status.PENDING,
    }


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
    assert [e.version for e in entries] == [8, 9, 10]
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


READ_ONLY_GRANTS = ("GRANT USAGE ON SCHEMA caverns TO {role}", "GRANT SELECT ON ALL TABLES IN SCHEMA caverns TO {role}")
UNRECORDABLE = Changeset("app", [Step("boom", "INSERT INTO nozdormu_test.missing VALUES (1 / 0)")])
BAD = Step(
    "bad",
    "CREATE TABLE nozdormu_test.bad (id int); INSERT INTO nozdormu_test.bad SELECT 1 / 0",
    check="SELECT to_regclass('nozdormu_test.bad') IS NOT NULL",
)
LATER = Step("later", "CREATE TABLE nozdormu_test.later (id int)")
FAILING = Changeset("app", [SCHEMA, BAD, LATER])


def test_a_failing_step_is_rolled_back_recorded_and_raised(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, FAILING)
    _assert_idle(ledger_conn)
    assert (failed.value.label, failed.value.version) == ("bad", 9)
    assert isinstance(failed.value.__cause__, DRIVER_ERRORS)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("bad", "failed")]
    row = _query(ledger_conn, "SELECT version, error, duration_ms FROM caverns.ledger WHERE outcome = 'failed'")[0]
    assert row[0] == 9 and row[1] == "22012: division by zero" and row[2] >= 0
    assert not _exists(ledger_conn, "nozdormu_test.bad")  # rolled back with its transaction
    assert not _exists(ledger_conn, "nozdormu_test.later")  # later steps did not run
    assert _statuses(ledger_conn, FAILING) == {"schema": Status.APPLIED, "bad": Status.PENDING, "later": Status.PENDING}


def _failed_error(conn: Any, step: Step, **kwargs: Any) -> str | None:
    """The `error` of the failed row that `step` leaves in a changeset after the schema step."""
    install(conn)
    with pytest.raises(StepFailed) as failed:
        apply(conn, Changeset("app", [SCHEMA, step]), **kwargs)
    assert isinstance(failed.value.__cause__, DRIVER_ERRORS)
    return _query(conn, "SELECT error FROM caverns.ledger WHERE outcome = 'failed'")[0][0]


def test_a_failed_row_keeps_the_sqlstate_and_the_primary_message(ledger_conn: Any) -> None:
    missing = Step("missing", "INSERT INTO nozdormu_test.missing VALUES (1)")
    assert _failed_error(ledger_conn, missing) == '42P01: relation "nozdormu_test.missing" does not exist'


def test_a_failed_row_drops_the_detail_hint_and_context(ledger_conn: Any) -> None:
    raising = Step(
        "raising",
        "DO $$ BEGIN RAISE EXCEPTION 'boom' USING DETAIL = 'secret detail', HINT = 'h'; END $$",
    )
    # The DO block is the subject, and Murozond calls it unknown: acknowledged.
    assert _failed_error(ledger_conn, raising, acknowledge=["raising"]) == "P0001: boom"


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


def _as_a_role_that_cannot_write_the_ledger(conn: Any, role: str) -> None:
    """Install, then become *role*, which may read the ledger but not append to it: a failed row cannot be recorded."""
    install(conn)
    _run(conn, *(grant.format(role=role) for grant in READ_ONLY_GRANTS), f"SET ROLE {role}")


def test_a_failure_that_cannot_be_recorded_raises_a_caverns_error(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    _as_a_role_that_cannot_write_the_ledger(ledger_conn, roles[2])
    with pytest.raises(CavernsError, match="could not be recorded") as refused:
        apply(ledger_conn, UNRECORDABLE)
    _assert_idle(ledger_conn)
    assert not isinstance(refused.value, StepFailed)
    assert isinstance(refused.value.__cause__, DRIVER_ERRORS)
    _run(ledger_conn, "RESET ROLE")
    assert _query(ledger_conn, APP_ROWS) == []


def test_apply_skips_a_step_another_runner_applied_after_preflight(ledger_conn: Any) -> None:
    install(ledger_conn)
    stale = preflight(ledger_conn, APP)
    _execute(ledger_conn, SCHEMA.sql)  # the other runner's work; running it again would fail
    _record(ledger_conn, SCHEMA)
    entries = nozdormu._apply_statuses(ledger_conn, APP, stale, frozenset(), frozenset(), consumer=False)
    assert [e.label for e in entries] == ["table", "index"]
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied"), ("index", "applied")]


def test_apply_refuses_a_step_that_changed_after_preflight(ledger_conn: Any) -> None:
    install(ledger_conn)
    stale = preflight(ledger_conn, APP)
    _record(ledger_conn, SCHEMA, checksum="0" * 64)  # another runner applied another version
    with pytest.raises(PreflightRefused, match=r"schema \(conflict\)"):
        nozdormu._apply_statuses(ledger_conn, APP, stale, frozenset(), frozenset(), consumer=False)
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied")]


RACE_STEP = Step("race", "CREATE TABLE nozdormu_test.race_t ()")
RACE = Changeset("race", [RACE_STEP])
RACE_ROWS = "SELECT label, outcome FROM caverns.ledger WHERE changeset = 'race' ORDER BY version"
REPEATABLE_READ_SQL = "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ"


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
        wait_for_a_waiting_lock(second_conn)
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


# --- lock timeout --------------------------------------------------------------------------

HOLD_LOCK_SQL = "SELECT pg_advisory_lock(%s::bigint)"
RELEASE_LOCK_SQL = "SELECT pg_advisory_unlock(%s::bigint)"
SEEN_STEP = Step("seen", "CREATE TABLE nozdormu_test.seen AS SELECT current_setting('lock_timeout') AS v")


def _hold_caverns_lock(conn: Any) -> None:
    """Another session takes Caverns' advisory lock for its session, as a running Caverns write would."""
    _execute(conn, HOLD_LOCK_SQL, (_connection.ADVISORY_LOCK_KEY,))


def _release_caverns_lock(conn: Any) -> None:
    conn.rollback()
    _execute(conn, RELEASE_LOCK_SQL, (_connection.ADVISORY_LOCK_KEY,))


def test_apply_gives_up_waiting_for_caverns_lock_with_a_caverns_error(ledger_conn: Any, second_conn: Any) -> None:
    install(ledger_conn)
    _hold_caverns_lock(second_conn)
    try:
        started = time.monotonic()
        with pytest.raises(CavernsError, match=r"advisory lock: gave up waiting after 0\.2 seconds") as refused:
            apply(ledger_conn, APP, lock_timeout=0.2)
        assert time.monotonic() - started < WAIT_SECONDS
    finally:
        _release_caverns_lock(second_conn)
    assert not isinstance(refused.value, StepFailed)
    assert _connection.sqlstate(refused.value.__cause__) == "55P03"  # type: ignore[arg-type]
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == []
    assert not _exists(ledger_conn, "nozdormu_test.t")
    assert [e.label for e in apply(ledger_conn, APP, lock_timeout=0.2)] == ["schema", "table", "index"]


def test_the_lock_timeout_message_names_whole_seconds_without_a_decimal(ledger_conn: Any, second_conn: Any) -> None:
    _hold_caverns_lock(second_conn)
    try:
        with ledger_conn.cursor() as cursor, pytest.raises(CavernsError, match="after 1 second "):
            _connection.lock(cursor, lock_timeout=1.0)
    finally:
        ledger_conn.rollback()
        _release_caverns_lock(second_conn)


def test_lock_with_no_argument_bounds_the_wait_by_the_default(ledger_conn: Any) -> None:
    with _connection.transaction(ledger_conn) as cursor:
        _connection.lock(cursor)
        cursor.execute("SELECT current_setting('lock_timeout')")
        assert cursor.fetchone() == ("5s",)
    assert _query(ledger_conn, "SHOW lock_timeout") == [("0",)]  # transaction-local: the session's own is back


@pytest.mark.parametrize(
    ("option", "seen", "after"),
    [(None, "7s", "7s"), (0.2, "200ms", "7s"), (0.001, "1ms", "7s"), ("default", "5s", "7s")],
)
def test_apply_sets_the_lock_timeout_for_each_step_only(ledger_conn: Any, option: Any, seen: str, after: str) -> None:
    install(ledger_conn)
    _execute(ledger_conn, "SET lock_timeout = '7s'")
    changeset = Changeset("app", [SCHEMA, SEEN_STEP])
    if option == "default":
        apply(ledger_conn, changeset)
    else:
        apply(ledger_conn, changeset, lock_timeout=option)
    assert _query(ledger_conn, "SELECT v FROM nozdormu_test.seen") == [(seen,)]
    assert _query(ledger_conn, "SHOW lock_timeout") == [(after,)]


def test_a_step_that_waits_for_a_lock_fails_with_the_lock_error_recorded(ledger_conn: Any, second_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, FIRST_TWO)
    touching = Changeset("app", [SCHEMA, TABLE, TOUCH])
    with second_conn.cursor() as cursor:
        cursor.execute("LOCK TABLE nozdormu_test.t IN ACCESS EXCLUSIVE MODE")  # held by an open transaction
    try:
        with pytest.raises(StepFailed) as failed:
            apply(ledger_conn, touching, lock_timeout=0.2)
    finally:
        second_conn.rollback()
    assert isinstance(failed.value.__cause__, DRIVER_ERRORS)
    assert _connection.sqlstate(failed.value.__cause__) == "55P03"
    rows = _query(
        ledger_conn, "SELECT label, outcome, error FROM caverns.ledger WHERE changeset = 'app' ORDER BY version"
    )
    assert [(r[0], r[1]) for r in rows] == [("schema", "applied"), ("table", "applied"), ("touch", "failed")]
    assert rows[2][2].startswith("55P03: ")
    assert [e.label for e in apply(ledger_conn, touching)] == ["touch"]  # the step is pending again


HELD_SECONDS = 0.5


def _release_caverns_lock_when_a_runner_has_waited(holder: Any, outcome: dict[str, Any]) -> threading.Thread:
    """Release the advisory lock *holder* has taken, but only after a runner is seen waiting for it for a while.

    Deterministic: the thread polls `pg_locks` until a waiter is queued, then lets HELD_SECONDS pass (several
    times any `lock_timeout` the tests use) before it unlocks, so a wait bounded by that timeout has certainly
    failed by then. The caller must not use *holder* until the thread has joined.
    """

    def release() -> None:
        try:
            wait_for_a_waiting_lock(holder)
            time.sleep(HELD_SECONDS)
            _release_caverns_lock(holder)
        except BaseException as error:  # reported by the test
            outcome["error"] = error

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    return thread


def _write_planned_step(conn: Any, label: str, *, first: bool, lock_timeout: float | None) -> Any:
    """What `apply` does for one planned, pending step of APP: `_write_step` in the step's own transaction."""
    status = next(s for s in preflight(conn, APP) if s.step.label == label)
    return nozdormu._write_step(
        conn, APP, status, "applied", consumer=True, first=first, last=True, lock_timeout=lock_timeout
    )


def test_a_later_step_waits_for_caverns_lock_without_a_timeout(ledger_conn: Any, second_conn: Any) -> None:
    """After an earlier step committed, the run's next wait for the lock outlasts `lock_timeout`, then goes on.

    The run is cut at the step: a second session cannot be made to take the lock between two steps of a real
    `apply`, so the per-step writer is called as `apply` calls it for its second step.
    """
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA]))
    _hold_caverns_lock(second_conn)
    outcome: dict[str, Any] = {}
    releaser = _release_caverns_lock_when_a_runner_has_waited(second_conn, outcome)
    try:
        entry = _write_planned_step(ledger_conn, "table", first=False, lock_timeout=0.1)
    finally:
        releaser.join(WAIT_SECONDS)
    assert "error" not in outcome
    assert (entry.label, entry.outcome) == ("table", "applied")
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied")]
    assert _exists(ledger_conn, "nozdormu_test.t")
    stored = _query(ledger_conn, "SELECT count(*) FROM caverns.fingerprint WHERE version = %s", (entry.version,))
    assert stored == [(1,)]


def test_the_first_step_still_gives_up_waiting_for_caverns_lock(ledger_conn: Any, second_conn: Any) -> None:
    install(ledger_conn)
    _hold_caverns_lock(second_conn)
    try:
        with pytest.raises(CavernsError, match=r"advisory lock: gave up waiting after 0\.2 seconds"):
            _write_planned_step(ledger_conn, "schema", first=True, lock_timeout=0.2)
    finally:
        _release_caverns_lock(second_conn)
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == []
    assert not _exists(ledger_conn, "nozdormu_test")


def test_a_failure_is_recorded_after_waiting_for_caverns_lock_without_a_timeout(
    ledger_conn: Any, second_conn: Any
) -> None:
    install(ledger_conn)
    _hold_caverns_lock(second_conn)
    outcome: dict[str, Any] = {}
    releaser = _release_caverns_lock_when_a_runner_has_waited(second_conn, outcome)
    try:
        failed = nozdormu._record_failure(
            ledger_conn, APP, SCHEMA, ValueError("boom"), 1, consumer=False, lock_timeout=0.1
        )
    finally:
        releaser.join(WAIT_SECONDS)
    assert "error" not in outcome
    assert failed.label == "schema"
    assert _query(ledger_conn, "SELECT label, outcome, error FROM caverns.ledger WHERE changeset = 'app'") == [
        ("schema", "failed", "boom")
    ]


def test_a_later_steps_own_lock_wait_is_still_bounded(ledger_conn: Any, second_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, FIRST_TWO)
    touching = Changeset("app", [SCHEMA, TABLE, NOOP, TOUCH])
    with second_conn.cursor() as cursor:
        cursor.execute("LOCK TABLE nozdormu_test.t IN ACCESS EXCLUSIVE MODE")
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            apply(ledger_conn, touching, lock_timeout=0.2)
        except BaseException as error:  # reported below
            outcome["error"] = error

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    try:
        runner.join(WAIT_SECONDS)
        hung = runner.is_alive()
    finally:
        second_conn.rollback()  # releases the table, so a hung run ends
        runner.join(WAIT_SECONDS)
    assert not hung, "the step's own lock wait was not bounded"
    failed = outcome.get("error")
    assert isinstance(failed, StepFailed)
    assert _connection.sqlstate(failed.__cause__) == "55P03"  # type: ignore[arg-type]
    rows = _query(ledger_conn, "SELECT label, outcome FROM caverns.ledger WHERE changeset = 'app' ORDER BY version")
    assert rows[-2:] == [("noop", "applied"), ("touch", "failed")]  # `touch` is the run's second step


def test_the_wait_after_a_skipped_first_step_is_still_bounded(
    ledger_conn: Any, second_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first planned step already applied by the time it is written writes no row: the next wait is bounded."""
    install(ledger_conn)
    statuses = preflight(ledger_conn, APP)
    apply(ledger_conn, Changeset("app", [SCHEMA]))  # another runner got there first
    real = nozdormu._write_step
    calls: list[str] = []

    def write(*args: Any, **kwargs: Any) -> Any:
        entry = real(*args, **kwargs)
        calls.append(args[2].step.label)
        if len(calls) == 1:
            assert entry is None  # skipped: nothing written
            _hold_caverns_lock(second_conn)
        return entry

    monkeypatch.setattr(nozdormu, "_write_step", write)
    try:
        with pytest.raises(CavernsError, match=r"advisory lock: gave up waiting after 0\.2 seconds"):
            nozdormu._apply_statuses(
                ledger_conn, APP, statuses, frozenset(), frozenset(), consumer=True, lock_timeout=0.2
            )
    finally:
        _release_caverns_lock(second_conn)
    assert calls == ["schema"]  # the second call raised while waiting
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied")]


def test_a_lock_timeout_that_is_not_valid_is_refused_before_anything_runs(ledger_conn: Any) -> None:
    install(ledger_conn)
    for value in (0, -1, True, float("nan"), float("inf"), "5", 1e10, 10**400, 1e-9, 0.0005):
        with pytest.raises(ChangesetError, match="lock_timeout"):
            apply(ledger_conn, APP, lock_timeout=value)  # type: ignore[arg-type]
    apply(ledger_conn, APP, lock_timeout=0.001)  # one millisecond is a valid timeout
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied"), ("index", "applied")]


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


def test_baseline_records_a_row_per_step_and_runs_nothing(ledger_conn: Any) -> None:
    install(ledger_conn)
    entry = baseline(ledger_conn, APP)
    assert (entry.changeset, entry.label, entry.outcome) == ("app", "index", "baseline")
    assert entry.checksum == INDEX.checksum
    assert FINGERPRINT.fullmatch(entry.fingerprint or "")
    assert (entry.duration_ms, entry.error, entry.caverns_version) == (None, None, __version__)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "baseline"), ("table", "baseline"), ("index", "baseline")]
    rows = _query(
        ledger_conn, "SELECT label, checksum, fingerprint FROM caverns.ledger WHERE changeset = 'app' ORDER BY version"
    )
    assert [(label, checksum) for label, checksum, _ in rows] == [(step.label, step.checksum) for step in APP.steps]
    assert {fingerprint for _, _, fingerprint in rows} == {entry.fingerprint}  # one hash on every row
    # The fingerprint is stored once, at the last row's version.
    assert _query(ledger_conn, "SELECT version FROM caverns.fingerprint") == [(entry.version,)]
    assert not _exists(ledger_conn, "nozdormu_test.t")
    assert set(_statuses(ledger_conn).values()) == {Status.APPLIED}
    assert apply(ledger_conn, APP) == []


def test_baseline_through_a_step_leaves_the_later_steps_pending(ledger_conn: Any) -> None:
    install(ledger_conn)
    _existing_database(ledger_conn)
    entry = baseline(ledger_conn, APP, through="table")
    assert (entry.label, entry.checksum) == ("table", TABLE.checksum)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "baseline"), ("table", "baseline")]
    assert _statuses(ledger_conn) == {"schema": Status.APPLIED, "table": Status.APPLIED, "index": Status.PENDING}
    assert [(e.label, e.outcome) for e in apply(ledger_conn, APP)] == [("index", "applied")]


def _baselined_with_a_step_inserted_before(conn: Any) -> tuple[Changeset, Step]:
    """Baseline `APP`, then return a changeset with a new step in front of the baseline's step."""
    install(conn)
    baseline(conn, APP)
    early = Step("early", "CREATE TABLE nozdormu_test.early (id int)")
    return Changeset("app", [SCHEMA, early, TABLE, INDEX]), early


def test_a_step_inserted_before_the_baseline_is_before_the_baseline_and_refused(ledger_conn: Any) -> None:
    changeset, early = _baselined_with_a_step_inserted_before(ledger_conn)
    assert _statuses(ledger_conn, changeset) == {
        "schema": Status.APPLIED,
        "early": Status.BEFORE_BASELINE,
        "table": Status.APPLIED,
        "index": Status.APPLIED,
    }
    with pytest.raises(PreflightRefused) as refused:
        apply(ledger_conn, changeset)
    assert [(s.step, s.status) for s in refused.value.statuses] == [(early, Status.BEFORE_BASELINE)]
    _assert_idle(ledger_conn)
    assert len(_query(ledger_conn, APP_ROWS)) == 3


def test_reapply_runs_a_step_before_the_baseline_and_records_applied(ledger_conn: Any) -> None:
    changeset, early = _baselined_with_a_step_inserted_before(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    entries = apply(ledger_conn, changeset, reapply=["early"])
    assert [(e.label, e.outcome) for e in entries] == [("early", "applied")]
    assert _exists(ledger_conn, "nozdormu_test.early")
    assert _statuses(ledger_conn, changeset)["early"] is Status.APPLIED


def test_accept_records_a_step_before_the_baseline_without_running_it(ledger_conn: Any) -> None:
    changeset, early = _baselined_with_a_step_inserted_before(ledger_conn)
    _execute(ledger_conn, SCHEMA.sql)
    entries = apply(ledger_conn, changeset, accept=["early"])
    assert [(e.label, e.outcome, e.checksum) for e in entries] == [("early", "accepted", early.checksum)]
    assert not _exists(ledger_conn, "nozdormu_test.early")
    assert _statuses(ledger_conn, changeset)["early"] is Status.APPLIED


def test_naming_an_applied_step_in_reapply_or_accept_is_refused(ledger_conn: Any) -> None:
    changeset, _ = _baselined_with_a_step_inserted_before(ledger_conn)
    for named in ({"reapply": ["table"]}, {"accept": ["table"]}):
        with pytest.raises(PreflightRefused):
            apply(ledger_conn, changeset, **named)
        _assert_idle(ledger_conn)


def test_an_edited_step_before_the_baseline_is_a_conflict(ledger_conn: Any) -> None:
    install(ledger_conn)
    baseline(ledger_conn, APP)
    edited = Changeset("app", [Step("schema", "CREATE SCHEMA nozdormu_test -- edited"), TABLE, INDEX])
    assert _statuses(ledger_conn, edited)["schema"] is Status.CONFLICT


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
    assert _query(ledger_conn, APP_ROWS) == [("schema", "baseline"), ("table", "baseline"), ("index", "baseline")]


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
    assert [e.version for e in entries] == [8, 9]
    live = take_fingerprint(ledger_conn)
    assert _query(ledger_conn, FINGERPRINT_ROWS) == [(9, list(live.schemas), live.hash)]
    assert _query(ledger_conn, "SELECT document FROM caverns.fingerprint") == [(live.document,)]
    assert "nozdormu_test" in live.schemas and live.document["nozdormu_test"]["table"] == {"t": {"kind": "r"}}


def test_a_run_with_nothing_to_do_records_no_fingerprint_and_no_warning(
    ledger_conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    install(ledger_conn)
    apply(ledger_conn, FIRST_TWO)
    before = _query(ledger_conn, FINGERPRINT_ROWS)
    _execute(ledger_conn, ADD_NOTE)  # drift, but a run that writes nothing neither looks for it nor refuses
    with caplog.at_level(logging.WARNING):
        assert apply(ledger_conn, FIRST_TWO) == []
    assert _query(ledger_conn, FINGERPRINT_ROWS) == before
    assert "drift before" not in caplog.text


def test_a_failing_step_records_the_fingerprint_with_its_failed_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, FAILING)
    assert failed.value.version == 9
    live = take_fingerprint(ledger_conn)  # the failed step was rolled back: this is what the first step left
    assert _query(ledger_conn, FINGERPRINT_ROWS) == [(9, list(live.schemas), live.hash)]
    assert "nozdormu_test" in live.schemas and live.document["nozdormu_test"]["table"] == {}


def test_a_failure_that_cannot_be_recorded_records_no_fingerprint(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    _as_a_role_that_cannot_write_the_ledger(ledger_conn, roles[2])
    with pytest.raises(CavernsError, match="could not be recorded"):
        apply(ledger_conn, UNRECORDABLE)
    _run(ledger_conn, "RESET ROLE")
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
        apply(ledger_conn, APP, on_drift="warn")
    warnings = [r.getMessage() for r in caplog.records if r.name == "caverns_of_time.nozdormu"]
    assert warnings == ["drift before applying changeset app: added column nozdormu_test.t.note"]
    assert [row[0] for row in _query(ledger_conn, FINGERPRINT_ROWS)] == [9, 10]  # the run went ahead
    caplog.clear()
    with caplog.at_level(logging.WARNING):  # the new fingerprint holds the column: no difference left
        apply(ledger_conn, Changeset("app", [*APP.steps, Step("more", "CREATE TABLE nozdormu_test.more ()")]))
    assert "drift before" not in caplog.text


# --- history -------------------------------------------------------------------------------


def _history_ledger(conn: Any) -> None:
    """The ledger with its own rows (1 to 7), then app's three steps (8 to 10) and one other changeset's row."""
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
    assert [e.version for e in entries] == [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    assert [(e.changeset, e.label) for e in entries] == [
        ("other", "x"), ("app", "index"), ("app", "table"), ("app", "schema"),
        ("caverns", "drift"), ("caverns", "acknowledgement"), ("caverns", "fingerprint_server"),
        ("caverns", "privileges"), ("caverns", "epochs"), ("caverns", "fingerprint"), ("caverns", "ledger"),
    ]  # fmt: skip
    _assert_idle(ledger_conn)


def test_history_of_one_changeset(ledger_conn: Any) -> None:
    _history_ledger(ledger_conn)
    assert [e.label for e in history(ledger_conn, changeset="app")] == ["index", "table", "schema"]
    assert [e.label for e in history(ledger_conn, changeset="caverns")] == [
        "drift",
        "acknowledgement",
        "fingerprint_server",
        "privileges",
        "epochs",
        "fingerprint",
        "ledger",
    ]
    assert history(ledger_conn, changeset="nobody") == []


def test_history_limit_keeps_the_newest_rows(ledger_conn: Any) -> None:
    _history_ledger(ledger_conn)
    assert [e.version for e in history(ledger_conn, limit=2)] == [11, 10]
    assert [e.version for e in history(ledger_conn, changeset="app", limit=1)] == [10]
    assert len(history(ledger_conn, limit=100)) == 11


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


# --- Murozond's guard and the acknowledgement of a risky step ------------------------------

DROP_T = Step("drop", "DROP TABLE nozdormu_test.t")
PURGE = Step("purge", "DELETE FROM nozdormu_test.t")
GUARDED = Changeset("app", [SCHEMA, TABLE, DROP_T])
ACKNOWLEDGEMENTS = "SELECT version, risk, findings FROM caverns.acknowledgement ORDER BY version"
ALL_VERSIONS = "SELECT label, version FROM caverns.ledger WHERE changeset = 'app' ORDER BY version"
NOTHING_WRITTEN = "SELECT (SELECT count(*) FROM caverns.fingerprint), (SELECT count(*) FROM caverns.acknowledgement)"


def _assert_nothing_written(conn: Any) -> None:
    _assert_idle(conn)
    assert _query(conn, APP_ROWS) == []
    assert _query(conn, NOTHING_WRITTEN) == [(0, 0)]
    assert not _exists(conn, "nozdormu_test.t")


@pytest.mark.parametrize(
    ("sql", "risk", "reason"),
    [
        ("DROP TABLE nozdormu_test.t", "destructive", "DROP TABLE"),
        ("CREATE INDEX t_id ON nozdormu_test.t (id)", "lock_hazard", "CREATE INDEX without CONCURRENTLY"),
        ("SELECT 1", "unknown", "SELECT"),
    ],
)
def test_the_guard_refuses_a_risky_pending_step_and_writes_nothing(
    ledger_conn: Any, sql: str, risk: str, reason: str
) -> None:
    install(ledger_conn)
    with pytest.raises(PreflightRefused) as refused:
        apply(ledger_conn, Changeset("app", [SCHEMA, TABLE, Step("risky", sql)]))
    assert isinstance(refused.value, GuardRefused)
    assert [s.step.label for s in refused.value.statuses] == ["risky"]
    assert [(c.risk, [f.reason for f in c.findings]) for c in refused.value.classifications] == [(risk, [reason])]
    assert (
        str(refused.value)
        == f"Murozond refuses the run for: risky ({risk}: {reason}): name each in acknowledge to run it"
    )
    _assert_nothing_written(ledger_conn)  # the safe steps before it did not run either


def test_the_guard_names_every_refused_step_with_its_distinct_reasons(ledger_conn: Any) -> None:
    install(ledger_conn)
    index = Step("idx", "CREATE INDEX t_id ON nozdormu_test.t (id)")
    both = Step("both", "DROP TABLE nozdormu_test.t; DROP TABLE nozdormu_test.u; DELETE FROM nozdormu_test.t")
    with pytest.raises(GuardRefused) as refused:
        apply(ledger_conn, Changeset("app", [SCHEMA, TABLE, DROP_T, index, both]))
    assert str(refused.value) == (
        "Murozond refuses the run for: drop (destructive: DROP TABLE), "
        "idx (lock_hazard: CREATE INDEX without CONCURRENTLY), "
        "both (destructive: DROP TABLE, DELETE): name each in acknowledge to run it"
    )
    assert [s.step.label for s in refused.value.statuses] == ["drop", "idx", "both"]
    assert len(refused.value.classifications) == 3
    _assert_nothing_written(ledger_conn)


def test_an_acknowledged_step_runs_and_its_acknowledgement_is_recorded_with_its_row(
    ledger_conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    install(ledger_conn)
    risky = Step(
        "risky", "CREATE TABLE nozdormu_test.u (id int); DROP TABLE nozdormu_test.u; DELETE FROM nozdormu_test.t"
    )
    with caplog.at_level(logging.WARNING, logger="caverns_of_time.nozdormu"):
        entries = apply(ledger_conn, Changeset("app", [SCHEMA, TABLE, risky]), acknowledge=["risky"])
    assert [(e.label, e.outcome) for e in entries] == [
        ("schema", "applied"),
        ("table", "applied"),
        ("risky", "applied"),
    ]
    assert [r.getMessage() for r in caplog.records if "acknowledged" in r.getMessage()] == [
        "step risky of changeset app runs acknowledged as destructive"
    ]
    assert _query(ledger_conn, ACKNOWLEDGEMENTS) == [
        (
            entries[2].version,  # the version of the step's own row
            "destructive",
            [
                {"risk": "destructive", "statement": 2, "reason": "DROP TABLE"},
                {"risk": "destructive", "statement": 3, "reason": "DELETE"},
            ],
        )
    ]
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.acknowledgement") == [(1,)]  # none for a safe step
    with pytest.raises(ChangesetError, match="step 'risky' does not run in this apply"):
        apply(ledger_conn, Changeset("app", [SCHEMA, TABLE, risky]), acknowledge=["risky"])  # applied: strict


def test_a_failed_acknowledged_step_records_its_acknowledgement_with_its_failed_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    bad = Step("bad", "CREATE TABLE nozdormu_test.bad (id int); DELETE FROM nozdormu_test.missing")
    changeset = Changeset("app", [SCHEMA, bad])
    with pytest.raises(GuardRefused):
        apply(ledger_conn, changeset)
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, changeset, acknowledge=["bad"])
    assert _query(ledger_conn, ACKNOWLEDGEMENTS) == [
        (failed.value.version, "destructive", [{"risk": "destructive", "statement": 2, "reason": "DELETE"}])
    ]
    assert _query(ledger_conn, "SELECT outcome FROM caverns.ledger WHERE version = %s", (failed.value.version,)) == [
        ("failed",)
    ]
    assert not _exists(ledger_conn, "nozdormu_test.bad")  # rolled back; the acknowledgement was not


def test_a_reapplied_risky_step_needs_an_acknowledgement(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    edited = Changeset("app", [SCHEMA, Step("table", "DROP TABLE nozdormu_test.t", check=TABLE.check)])
    with pytest.raises(PreflightRefused) as refused:
        apply(ledger_conn, edited)
    assert type(refused.value) is PreflightRefused  # a conflict is a status refusal, not Murozond's
    with pytest.raises(GuardRefused, match=r"table \(destructive: DROP TABLE\)"):
        apply(ledger_conn, edited, reapply=["table"])
    _assert_idle(ledger_conn)
    assert _exists(ledger_conn, "nozdormu_test.t")
    entries = apply(ledger_conn, edited, reapply=["table"], acknowledge=["table"])
    assert [(e.label, e.outcome) for e in entries] == [("table", "applied")]
    assert [row[1:2] for row in _query(ledger_conn, ACKNOWLEDGEMENTS)] == [("destructive",)]
    assert not _exists(ledger_conn, "nozdormu_test.t")


def test_accepting_a_risky_step_needs_no_acknowledgement_and_acknowledging_it_is_refused(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    edited = Changeset("app", [SCHEMA, Step("table", "DROP TABLE nozdormu_test.t", check=TABLE.check)])
    with pytest.raises(ChangesetError, match="step 'table' does not run in this apply: nothing to acknowledge"):
        apply(ledger_conn, edited, accept=["table"], acknowledge=["table"])
    _assert_idle(ledger_conn)
    assert [(e.label, e.outcome) for e in apply(ledger_conn, edited, accept=["table"])] == [("table", "accepted")]
    assert _exists(ledger_conn, "nozdormu_test.t")  # accepted: not run
    assert _query(ledger_conn, ACKNOWLEDGEMENTS) == []


def test_apply_refuses_an_acknowledgement_that_has_nothing_to_acknowledge(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    changeset = Changeset("app", [SCHEMA, TABLE, Step("later", "CREATE TABLE nozdormu_test.later (id int)"), DROP_T])
    cases = [
        (["ghost"], "changeset 'app' has no step 'ghost' to acknowledge"),
        (["schema"], "step 'schema' does not run in this apply: nothing to acknowledge"),  # applied
        (["later"], "step 'later' is safe: nothing to acknowledge"),
        (["later", "schema", "ghost", "zzz"], "changeset 'app' has no step 'ghost' to acknowledge"),
        (["later", "schema"], "step 'schema' does not run in this apply: nothing to acknowledge"),
        (["drop", "later"], "step 'later' is safe: nothing to acknowledge"),
    ]
    for labels, message in cases:
        with pytest.raises(ChangesetError) as refused:
            apply(ledger_conn, changeset, acknowledge=labels)
        assert str(refused.value) == message
        _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied")]
    with pytest.raises(ChangesetError, match="acknowledge must be a collection of names, not the string 'drop'"):
        apply(ledger_conn, changeset, acknowledge="drop")  # type: ignore[arg-type]


def test_the_status_refusal_comes_before_the_guard_and_the_acknowledgement_checks(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    conflict = Changeset("app", [SCHEMA, Step("table", ADD_NOTE, check=TABLE.check), DROP_T])
    for acknowledge in ([], ["ghost"], ["schema"]):
        with pytest.raises(PreflightRefused) as refused:
            apply(ledger_conn, conflict, acknowledge=acknowledge)
        assert type(refused.value) is PreflightRefused
        assert [(s.step.label, s.status) for s in refused.value.statuses] == [("table", Status.CONFLICT)]
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied")]


def test_a_step_another_runner_applied_after_preflight_writes_no_acknowledgement(ledger_conn: Any) -> None:
    _applied_prefix(ledger_conn)
    changeset = Changeset("app", [SCHEMA, TABLE, PURGE])
    stale = preflight(ledger_conn, changeset)
    _record(ledger_conn, PURGE)  # the other runner's row
    entries = nozdormu._apply_statuses(
        ledger_conn,
        changeset,
        stale,
        frozenset(),
        frozenset(),
        consumer=True,
        acknowledge=frozenset({"purge"}),
    )
    assert entries == []
    assert _query(ledger_conn, ACKNOWLEDGEMENTS) == []


def test_the_own_changeset_is_not_guarded(ledger_conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    risky = Step("risky", "DELETE FROM caverns.ledger WHERE false", check="SELECT false")
    own = Changeset("caverns", [*nozdormu._OWN_CHANGESET.steps, risky])
    monkeypatch.setattr(nozdormu, "_OWN_CHANGESET", own)
    assert murozond.classify(risky.sql).risk is not murozond.Risk.SAFE  # the guard would refuse it
    assert [e.label for e in install(ledger_conn)][-1] == "risky"
    assert _query(ledger_conn, ACKNOWLEDGEMENTS) == []


# --- the own step `acknowledgement` --------------------------------------------------------


def test_the_acknowledgement_table_is_append_only(ledger_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA, TABLE, PURGE]), acknowledge=["purge"])
    before = _query(ledger_conn, ACKNOWLEDGEMENTS)
    assert len(before) == 1
    for statement in (
        "UPDATE caverns.acknowledgement SET risk = 'unknown'",
        "DELETE FROM caverns.acknowledgement",
        "TRUNCATE caverns.acknowledgement",
    ):
        _refused(ledger_conn, statement, "caverns.acknowledgement is append-only")
    assert _query(ledger_conn, ACKNOWLEDGEMENTS) == before


@pytest.mark.parametrize(
    ("risk", "findings"),
    [("safe", '[{"risk": "safe"}]'), ("unknown", "[]"), ("unknown", "{}"), ("unknown", "null"), (None, '[{"a": 1}]')],
)
def test_the_acknowledgement_table_holds_only_a_risk_and_a_non_empty_array(
    ledger_conn: Any, risk: str | None, findings: str
) -> None:
    install(ledger_conn)
    insert = "INSERT INTO caverns.acknowledgement (version, risk, findings) VALUES (1, %s, %s::jsonb)"
    _refused(ledger_conn, insert, "acknowledgement", (risk, findings))
    assert _query(ledger_conn, ACKNOWLEDGEMENTS) == []


def test_the_acknowledgement_table_belongs_to_one_ledger_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    insert = "INSERT INTO caverns.acknowledgement (version, risk, findings) VALUES (%s, 'unknown', '[{}]')"
    _execute(ledger_conn, insert, (1,))
    _refused(ledger_conn, insert, "acknowledgement_pkey", (1,))  # one acknowledgement per row
    _refused(ledger_conn, insert, "foreign key", (999,))


def test_an_installation_of_the_previous_release_gets_the_acknowledgement_step_with_install(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_own_steps(ledger_conn, monkeypatch, 5)
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS[:5]
    assert not _exists(ledger_conn, "caverns.acknowledgement")
    # Until the owner runs install, every call that needs the ledger refuses.
    with pytest.raises(LedgerNotInstalled, match="call install"):
        apply(ledger_conn, APP)
    with pytest.raises(LedgerNotInstalled, match="call install"):
        preflight(ledger_conn, APP)
    _assert_idle(ledger_conn)
    assert [e.label for e in history(ledger_conn)][0] == "fingerprint_server"  # the old rows still read
    entries = install(ledger_conn)
    assert [(e.version, e.changeset, e.label, e.outcome) for e in entries] == OWN_ROWS[5:]
    assert entries[0].checksum == nozdormu._OWN_CHANGESET.steps[5].checksum
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _query(ledger_conn, PUBLIC_FUNCTIONS_SQL) == []  # `append_only` revoked itself
    assert _query(ledger_conn, PUBLIC_ACL_ROWS) == [(0, 0)]
    assert install(ledger_conn) == []
    assert [e.label for e in apply(ledger_conn, GUARDED, acknowledge=["drop"])][-1] == "drop"


def test_install_accepts_the_acknowledgement_step_when_the_table_is_already_there(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_own_steps(ledger_conn, monkeypatch, 5)
    _execute(ledger_conn, nozdormu._ACKNOWLEDGEMENT_DDL_SQL)  # a restore that kept the table but not the row
    assert [(e.label, e.outcome) for e in install(ledger_conn)] == [
        ("acknowledgement", "accepted"),
        ("drift", "applied"),
    ]


def test_a_runner_role_with_the_documented_grants_records_an_acknowledgement(
    ledger_conn: Any, roles: tuple[str, str, str]
) -> None:
    _, _, runner = roles
    install(ledger_conn)
    _run(
        ledger_conn,
        "CREATE SCHEMA nozdormu_test",
        "CREATE TABLE nozdormu_test.t (id int)",
        f"GRANT USAGE ON SCHEMA nozdormu_test TO {runner}",
        f"GRANT SELECT, DELETE ON nozdormu_test.t TO {runner}",
        *(grant.format(role=runner) for grant in RUNNER_GRANTS),
        f"SET ROLE {runner}",
    )
    with pytest.raises(GuardRefused):
        apply(ledger_conn, Changeset("app", [PURGE]))
    assert [e.label for e in apply(ledger_conn, Changeset("app", [PURGE]), acknowledge=["purge"])] == ["purge"]
    _run(ledger_conn, "RESET ROLE")
    assert [row[1] for row in _query(ledger_conn, ACKNOWLEDGEMENTS)] == ["destructive"]


# --- steps that run outside a transaction --------------------------------------------------

CONCURRENT_INDEX = Step(
    "cindex", "-- caverns:no-transaction\nCREATE INDEX CONCURRENTLY t_id_c ON nozdormu_test.t (id)", check=None
)
CONCURRENT_APP = Changeset("app", [SCHEMA, TABLE, CONCURRENT_INDEX])
UNIQUE_APP = Changeset(
    "app",
    [
        SCHEMA,
        TABLE,
        Step("dups", "INSERT INTO nozdormu_test.t VALUES (1), (1)"),
        Step("uniq", "-- caverns:no-transaction\nCREATE UNIQUE INDEX CONCURRENTLY t_uniq ON nozdormu_test.t (id)"),
    ],
)
LOCK_TIMEOUT = "SELECT current_setting('lock_timeout')"
HELD_ADVISORY_LOCKS = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
INDEX_VALID = "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)"


def _assert_outside_state_restored(conn: Any, lock_timeout: str = "0") -> None:
    """The connection is as before a non-transactional step: autocommit off, lock_timeout back, no lock held."""
    assert conn.autocommit is False
    _assert_idle(conn)
    assert _query(conn, LOCK_TIMEOUT) == [(lock_timeout,)]
    assert _query(conn, HELD_ADVISORY_LOCKS) == [(0,)]


def test_a_non_transactional_step_applies_and_leaves_the_connection_as_it_was(ledger_conn: Any) -> None:
    install(ledger_conn)
    _execute(ledger_conn, "SET lock_timeout = '7s'")
    entries = apply(ledger_conn, CONCURRENT_APP)
    assert [(e.label, e.outcome) for e in entries] == [
        ("schema", "applied"),
        ("table", "applied"),
        ("cindex", "applied"),
    ]
    assert entries[2].duration_ms is not None
    assert _query(ledger_conn, INDEX_VALID, ("nozdormu_test.t_id_c",)) == [(True,)]
    assert [s.step.label for s in preflight(ledger_conn, CONCURRENT_APP) if s.status is Status.APPLIED] == [
        "schema",
        "table",
        "cindex",
    ]
    _assert_outside_state_restored(ledger_conn, "7s")


def test_a_non_transactional_step_runs_under_the_lock_timeout_of_the_call(ledger_conn: Any) -> None:
    install(ledger_conn)
    seen = Step(
        "seen",
        "-- caverns:no-transaction\nCREATE TABLE nozdormu_test.seen AS SELECT current_setting('lock_timeout') AS v",
    )
    apply(ledger_conn, Changeset("app", [SCHEMA, seen]), lock_timeout=2.5)
    assert _query(ledger_conn, "SELECT v FROM nozdormu_test.seen") == [("2500ms",)]
    _assert_outside_state_restored(ledger_conn)


def test_a_non_transactional_step_that_is_last_records_the_fingerprint_at_its_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    entries = apply(ledger_conn, CONCURRENT_APP)
    stored = _query(ledger_conn, FINGERPRINT_ROWS)
    assert [row[0] for row in stored] == [entries[-1].version]


def test_the_marker_is_what_lets_a_concurrent_index_run(ledger_conn: Any) -> None:
    install(ledger_conn)
    plain = Step("cindex", CONCURRENT_INDEX.sql.split("\n", 1)[1])
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, Changeset("app", [SCHEMA, TABLE, plain]))
    row = _query(ledger_conn, "SELECT error FROM caverns.ledger WHERE version = %s", (failed.value.version,))
    assert "cannot run inside a transaction block" in row[0][0]
    entries = apply(ledger_conn, CONCURRENT_APP)  # the same statement behind the marker
    assert [(e.label, e.outcome) for e in entries] == [("cindex", "applied")]
    assert _query(ledger_conn, INDEX_VALID, ("nozdormu_test.t_id_c",)) == [(True,)]


def test_a_failing_non_transactional_step_is_recorded_and_warns_of_the_invalid_index(
    ledger_conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    install(ledger_conn)
    _execute(ledger_conn, "SET lock_timeout = '7s'")
    with caplog.at_level(logging.WARNING, logger="caverns_of_time.nozdormu"), pytest.raises(StepFailed) as failed:
        apply(ledger_conn, UNIQUE_APP)
    assert failed.value.label == "uniq"
    rows = _query(ledger_conn, "SELECT outcome, error FROM caverns.ledger WHERE version = %s", (failed.value.version,))
    assert rows[0][0] == "failed"
    assert rows[0][1].startswith("23505: ")
    assert _query(ledger_conn, INDEX_VALID, ("nozdormu_test.t_uniq",)) == [(False,)]  # no rollback: it stays
    warnings = [r.getMessage() for r in caplog.records if "outside a transaction" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].startswith("step uniq of changeset app failed outside a transaction; invalid indexes")
    assert "nozdormu_test.t_uniq" in warnings[0]
    assert warnings[0].endswith(
        "drop each with DROP INDEX CONCURRENTLY, then reapply with on_drift='warn' "
        "(those in watched schemas are in the failed row's fingerprint)"
    )
    stored = _query(ledger_conn, FINGERPRINT_ROWS)
    assert [row[0] for row in stored] == [failed.value.version]
    _assert_outside_state_restored(ledger_conn, "7s")


FREE_STEP = Step(
    "free",
    "-- caverns:no-transaction\nCREATE SCHEMA nozdormu_test",
    check="SELECT to_regnamespace('nozdormu_test') IS NOT NULL",
)


def test_a_non_transactional_first_step_gives_up_waiting_for_caverns_lock(ledger_conn: Any, second_conn: Any) -> None:
    install(ledger_conn)
    _hold_caverns_lock(second_conn)
    try:
        started = time.monotonic()
        with pytest.raises(CavernsError, match=r"advisory lock: gave up waiting after 0\.2 seconds"):
            apply(ledger_conn, Changeset("app", [FREE_STEP]), lock_timeout=0.2)
        assert time.monotonic() - started < WAIT_SECONDS
    finally:
        _release_caverns_lock(second_conn)
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, APP_ROWS) == []
    assert not _exists(ledger_conn, "nozdormu_test")


def test_a_non_transactional_step_whose_lock_release_returns_false_raises_a_caverns_error(ledger_conn: Any) -> None:
    """What a transaction-mode pooler does: the unlock reaches a session that never held the lock."""
    install(ledger_conn)
    unlock = Step("unlock", f"-- caverns:no-transaction\nSELECT pg_advisory_unlock({_connection.ADVISORY_LOCK_KEY})")
    with pytest.raises(CavernsError, match="the lock was not held") as failed:
        apply(ledger_conn, Changeset("app", [unlock]), acknowledge=["unlock"])
    assert str(failed.value) == (
        "step 'unlock' of changeset 'app' was recorded, but releasing Caverns' advisory lock failed "
        "(the lock was not held): Caverns needs a direct connection or a session-mode pooler, "
        "not a transaction-mode one"
    )
    assert _query(ledger_conn, APP_ROWS) == [("unlock", "applied")]  # the step stands, as the message says


def test_a_failed_non_transactional_step_can_be_fixed_and_applied(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(StepFailed):
        apply(ledger_conn, UNIQUE_APP)
    _execute(ledger_conn, "DROP INDEX nozdormu_test.t_uniq")
    _execute(ledger_conn, "DELETE FROM nozdormu_test.t WHERE id = 1")
    entries = apply(ledger_conn, UNIQUE_APP, on_drift="warn")  # the manual repair is drift from the recorded failure
    assert [(e.label, e.outcome) for e in entries] == [("uniq", "applied")]
    assert _query(ledger_conn, INDEX_VALID, ("nozdormu_test.t_uniq",)) == [(True,)]


def test_a_risky_non_transactional_step_needs_an_acknowledgement_and_records_it(ledger_conn: Any) -> None:
    install(ledger_conn)
    vacuum = Step("vacuum", "-- caverns:no-transaction\nVACUUM FULL nozdormu_test.t")
    changeset = Changeset("app", [SCHEMA, TABLE, vacuum])
    with pytest.raises(GuardRefused, match=r"vacuum \(lock_hazard: VACUUM FULL\)"):
        apply(ledger_conn, changeset)
    entries = apply(ledger_conn, changeset, acknowledge=["vacuum"])
    assert [(e.label, e.outcome) for e in entries] == [
        ("schema", "applied"),
        ("table", "applied"),
        ("vacuum", "applied"),
    ]
    assert [row[:2] for row in _query(ledger_conn, ACKNOWLEDGEMENTS)] == [(entries[2].version, "lock_hazard")]
    _assert_outside_state_restored(ledger_conn)


def test_a_non_transactional_step_another_runner_applied_first_writes_nothing(
    ledger_conn: Any, second_conn: Any
) -> None:
    _applied_prefix(ledger_conn)
    fingerprints = _query(ledger_conn, FINGERPRINT_ROWS)
    stale = preflight(ledger_conn, CONCURRENT_APP)
    _record(second_conn, CONCURRENT_INDEX)  # the other runner's row
    entries = nozdormu._apply_statuses(ledger_conn, CONCURRENT_APP, stale, frozenset(), frozenset(), consumer=True)
    assert entries == []
    assert _query(ledger_conn, APP_ROWS) == [("schema", "applied"), ("table", "applied"), ("cindex", "applied")]
    assert _query(ledger_conn, "SELECT to_regclass('nozdormu_test.t_id_c') IS NULL") == [(True,)]
    assert _query(ledger_conn, FINGERPRINT_ROWS) == fingerprints
    _assert_outside_state_restored(ledger_conn)


def test_a_changeset_mixes_transactional_and_non_transactional_steps_in_order(ledger_conn: Any) -> None:
    install(ledger_conn)
    after = Step("after", "CREATE TABLE nozdormu_test.after (id int)")
    changeset = Changeset("app", [SCHEMA, TABLE, CONCURRENT_INDEX, after])
    entries = apply(ledger_conn, changeset)
    assert [(e.label, e.outcome) for e in entries] == [
        ("schema", "applied"),
        ("table", "applied"),
        ("cindex", "applied"),
        ("after", "applied"),
    ]
    assert [e.version for e in entries] == sorted(e.version for e in entries)
    assert [row[0] for row in _query(ledger_conn, FINGERPRINT_ROWS)] == [entries[-1].version]
    assert _exists(ledger_conn, "nozdormu_test.after")
    _assert_outside_state_restored(ledger_conn)


# --- drift: refused by default, recorded when a run proceeds past it ------------------------

DRIFT_ROWS = "SELECT version, fingerprint_version, schemas, differences FROM caverns.drift ORDER BY version"
DRIFT_COUNT = "SELECT count(*) FROM caverns.drift"
WARNINGS = "caverns_of_time.nozdormu"
NOTE_ADDED = {"kind": "column", "name": "nozdormu_test.t.note", "change": "added"}
MORE = Step("more", "CREATE TABLE nozdormu_test.more ()")
WITH_MORE = Changeset("app", [SCHEMA, TABLE, INDEX, MORE])


def _drifted(conn: Any) -> None:
    """The first two steps of `APP` applied (a fingerprint recorded at ledger version 9), then an out-of-band column."""
    _applied_prefix(conn)
    _execute(conn, ADD_NOTE)


def _written(conn: Any) -> list[list[tuple[Any, ...]]]:
    """Everything a refused run must leave alone: the ledger, the fingerprints and the drift records."""
    return [_query(conn, sql) for sql in (LEDGER_ROWS, FINGERPRINT_ROWS, DRIFT_ROWS)]


def test_apply_refuses_drift_by_default_and_writes_nothing(ledger_conn: Any) -> None:
    _drifted(ledger_conn)
    before = _written(ledger_conn)
    with pytest.raises(DriftRefused) as refused:
        apply(ledger_conn, APP)
    assert refused.value.fingerprint_version == 9
    assert [(d.change, d.kind, d.name) for d in refused.value.differences] == [
        ("added", "column", "nozdormu_test.t.note")
    ]
    assert isinstance(refused.value.differences, tuple)
    assert str(refused.value) == (
        "the live schema differs from the fingerprint at ledger version 9: added column nozdormu_test.t.note: "
        "apply with on_drift='warn' to proceed and record the drift"
    )
    _assert_idle(ledger_conn)
    assert _written(ledger_conn) == before
    assert not _exists(ledger_conn, "nozdormu_test.u")  # the step did not run
    assert [(e.label, e.outcome) for e in history(ledger_conn, changeset="app")] == [
        ("table", "applied"),
        ("schema", "applied"),
    ]


def test_on_drift_refuse_is_the_default_spelled_out(ledger_conn: Any) -> None:
    _drifted(ledger_conn)
    with pytest.raises(DriftRefused):
        apply(ledger_conn, APP, on_drift="refuse")


@pytest.mark.parametrize("created", [1, 5, 6, 8])
def test_the_refusal_lists_the_first_five_differences_and_counts_the_rest(ledger_conn: Any, created: int) -> None:
    _applied_prefix(ledger_conn)
    tables = [f"nozdormu_test.x{number}" for number in range(1, created + 1)]
    _execute(ledger_conn, "; ".join(f"CREATE TABLE {table} ()" for table in tables))
    with pytest.raises(DriftRefused) as refused:
        apply(ledger_conn, APP)
    assert len(refused.value.differences) == created
    listing = ", ".join(f"added table {table}" for table in tables[:5])
    if created > 5:
        listing += f", and {created - 5} more"
    assert str(refused.value) == (
        f"the live schema differs from the fingerprint at ledger version 9: {listing}: "
        "apply with on_drift='warn' to proceed and record the drift"
    )


def test_warn_proceeds_logs_and_records_the_drift_with_the_first_row(
    ledger_conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _drifted(ledger_conn)
    with caplog.at_level(logging.WARNING):
        entries = apply(ledger_conn, WITH_MORE, on_drift="warn")
    assert [(e.label, e.version) for e in entries] == [("index", 10), ("more", 11)]
    warnings = [r.getMessage() for r in caplog.records if r.name == WARNINGS]
    assert warnings == ["drift before applying changeset app: added column nozdormu_test.t.note"]
    (row,) = _query(ledger_conn, DRIFT_ROWS)  # one record for the run, at its first row
    recorded = _query(ledger_conn, "SELECT schemas FROM caverns.fingerprint WHERE version = 9")[0][0]
    assert row == (10, 9, recorded, [NOTE_ADDED])
    assert "nozdormu_test" in row[2]
    assert [r[0] for r in _query(ledger_conn, FINGERPRINT_ROWS)] == [9, 11]
    assert _query(ledger_conn, DRIFT_COUNT) == [(1,)]
    assert _query(ledger_conn, "SELECT recorded_at IS NOT NULL FROM caverns.drift") == [(True,)]


def test_the_next_apply_after_a_warn_run_sees_no_drift(ledger_conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    _drifted(ledger_conn)
    apply(ledger_conn, APP, on_drift="warn")  # its fingerprint absorbs the column
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        entries = apply(ledger_conn, WITH_MORE)  # the default refuses on drift: none is left
    assert [e.label for e in entries] == ["more"]
    assert "drift before" not in caplog.text
    assert [row[0] for row in _query(ledger_conn, DRIFT_ROWS)] == [10]


def test_drift_is_recorded_with_a_failed_first_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA]))
    _execute(ledger_conn, "CREATE TABLE nozdormu_test.oob ()")
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, FAILING, on_drift="warn")
    assert failed.value.version == 9
    differences = [{"kind": "table", "name": "nozdormu_test.oob", "change": "added"}]
    assert [row[:2] + row[3:] for row in _query(ledger_conn, DRIFT_ROWS)] == [(9, 8, differences)]
    assert [row[0] for row in _query(ledger_conn, FINGERPRINT_ROWS)] == [8, 9]


def test_drift_is_recorded_with_a_failed_first_row_of_a_non_transactional_step(ledger_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", UNIQUE_APP.steps[:3]))  # the duplicate rows the unique index trips on
    _execute(ledger_conn, "CREATE TABLE nozdormu_test.oob ()")
    with pytest.raises(StepFailed) as failed:
        apply(ledger_conn, UNIQUE_APP, on_drift="warn")
    assert [row[:2] for row in _query(ledger_conn, DRIFT_ROWS)] == [(failed.value.version, 10)]


def test_drift_is_recorded_with_an_accepted_first_row(ledger_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA]))
    _execute(ledger_conn, TABLE.sql)  # present without a row: drift, and the step reads `unknown`
    entries = apply(ledger_conn, FIRST_TWO, accept=["table"], on_drift="warn")
    assert [(e.label, e.outcome, e.version) for e in entries] == [("table", "accepted", 9)]
    assert [(row[0], row[1], [d["name"] for d in row[3]]) for row in _query(ledger_conn, DRIFT_ROWS)] == [
        (9, 8, ["nozdormu_test.t", "nozdormu_test.t.id"])
    ]


def test_drift_goes_with_the_next_row_when_another_runner_applied_the_first_step(
    ledger_conn: Any, second_conn: Any
) -> None:
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA]))
    _execute(ledger_conn, "CREATE TABLE nozdormu_test.oob ()")
    stale = preflight(ledger_conn, APP)
    _record(second_conn, TABLE)  # the other runner got there first
    entries = nozdormu._apply_statuses(
        ledger_conn, APP, stale, frozenset(), frozenset(), consumer=True, on_drift="warn"
    )
    assert [e.label for e in entries] == ["index"]
    assert [row[:2] for row in _query(ledger_conn, DRIFT_ROWS)] == [(entries[0].version, 8)]


def test_a_run_whose_steps_are_all_applied_elsewhere_records_no_drift(ledger_conn: Any, second_conn: Any) -> None:
    install(ledger_conn)
    apply(ledger_conn, Changeset("app", [SCHEMA]))
    _execute(ledger_conn, "CREATE TABLE nozdormu_test.oob ()")
    stale = preflight(ledger_conn, FIRST_TWO)
    _record(second_conn, TABLE)
    entries = nozdormu._apply_statuses(
        ledger_conn, FIRST_TWO, stale, frozenset(), frozenset(), consumer=True, on_drift="warn"
    )
    assert entries == []
    assert _query(ledger_conn, DRIFT_COUNT) == [(0,)]


def test_without_a_recorded_fingerprint_nothing_is_checked_or_recorded(
    ledger_conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    install(ledger_conn)
    _execute(ledger_conn, "CREATE SCHEMA nozdormu_test; CREATE TABLE nozdormu_test.oob ()")
    with caplog.at_level(logging.WARNING):
        assert [e.label for e in apply(ledger_conn, Changeset("app", [LATER]))] == ["later"]
    assert "drift before" not in caplog.text
    assert _query(ledger_conn, DRIFT_COUNT) == [(0,)]


def test_without_a_difference_warn_records_nothing(ledger_conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    _applied_prefix(ledger_conn)
    with caplog.at_level(logging.WARNING):
        assert [e.label for e in apply(ledger_conn, APP, on_drift="warn")] == ["index"]
    assert "drift before" not in caplog.text
    assert _query(ledger_conn, DRIFT_COUNT) == [(0,)]


@pytest.mark.parametrize("on_drift", ["ignore", "WARN", "", None, 1, True, ["warn"]])
def test_apply_refuses_an_invalid_on_drift(ledger_conn: Any, on_drift: Any) -> None:
    _drifted(ledger_conn)
    before = _written(ledger_conn)
    with pytest.raises(ChangesetError, match=r"on_drift must be 'refuse' or 'warn', not "):
        apply(ledger_conn, APP, on_drift=on_drift)
    _assert_idle(ledger_conn)
    assert _written(ledger_conn) == before


def test_the_refusals_come_in_order_status_acknowledge_guard_drift(ledger_conn: Any) -> None:
    _drifted(ledger_conn)
    with pytest.raises(PreflightRefused) as status:  # an edited step: the status refusal first
        apply(ledger_conn, Changeset("app", [SCHEMA, Step("table", ADD_NOTE), DROP_T]))
    assert not isinstance(status.value, (GuardRefused, DriftRefused))
    with pytest.raises(ChangesetError, match="no step 'nobody' to acknowledge"):
        apply(ledger_conn, GUARDED, acknowledge=["nobody"])
    with pytest.raises(GuardRefused):  # drift exists, yet the guard refuses first
        apply(ledger_conn, GUARDED)
    with pytest.raises(DriftRefused):  # acknowledged, the drift refuses
        apply(ledger_conn, GUARDED, acknowledge=["drop"])
    assert _query(ledger_conn, DRIFT_COUNT) == [(0,)]
    assert [e.label for e in apply(ledger_conn, GUARDED, acknowledge=["drop"], on_drift="warn")] == ["drop"]
    assert [row[0] for row in _query(ledger_conn, DRIFT_ROWS)] == [10]


def test_the_own_changeset_is_never_checked_for_drift(ledger_conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _drifted(ledger_conn)
    later = Changeset("caverns", [*nozdormu._OWN_CHANGESET.steps, Step("later", "SELECT 1")])
    monkeypatch.setattr(nozdormu, "_OWN_CHANGESET", later)  # a release with one more own step
    assert [(e.label, e.outcome) for e in install(ledger_conn)] == [("later", "applied")]  # drift exists: no refusal
    assert _query(ledger_conn, DRIFT_COUNT) == [(0,)]


def test_a_run_that_refuses_drift_in_a_read_only_transaction_leaves_the_session_idle(ledger_conn: Any) -> None:
    _drifted(ledger_conn)
    with pytest.raises(DriftRefused):
        apply(ledger_conn, APP)
    _assert_idle(ledger_conn)


# --- the drift table, Caverns' seventh own step ---------------------------------------------


def _recorded_drift(conn: Any) -> None:
    """A drift record at ledger version 10, with the fingerprint it refers to at 9."""
    _drifted(conn)
    apply(conn, APP, on_drift="warn")


def test_an_installation_of_the_previous_release_gets_the_drift_step_with_install(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_own_steps(ledger_conn, monkeypatch, 6)
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS[:6]
    assert not _exists(ledger_conn, "caverns.drift")
    assert _query(ledger_conn, nozdormu._DRIFT_EXISTS_SQL) == [(False,)]
    with pytest.raises(LedgerNotInstalled, match="call install"):
        preflight(ledger_conn, APP)
    with pytest.raises(LedgerNotInstalled, match="call install"):
        apply(ledger_conn, APP)
    _assert_idle(ledger_conn)
    assert [e.label for e in history(ledger_conn)][0] == "acknowledgement"  # the old rows still read
    entries = install(ledger_conn)
    assert [(e.version, e.changeset, e.label, e.outcome) for e in entries] == OWN_ROWS[6:]
    assert entries[0].checksum == nozdormu._OWN_CHANGESET.steps[6].checksum
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _query(ledger_conn, nozdormu._DRIFT_EXISTS_SQL) == [(True,)]
    assert install(ledger_conn) == []  # applying twice writes nothing the second time
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


def test_install_accepts_the_drift_step_when_the_table_is_already_there(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_own_steps(ledger_conn, monkeypatch, 6)
    _execute(ledger_conn, nozdormu._DRIFT_DDL_SQL)  # a restore that kept the table but not the row
    assert [(e.label, e.outcome) for e in install(ledger_conn)] == [("drift", "accepted")]


@pytest.mark.parametrize(
    "statement",
    ["UPDATE caverns.drift SET schemas = schemas", "DELETE FROM caverns.drift", "TRUNCATE caverns.drift"],
)
def test_the_drift_table_is_append_only(ledger_conn: Any, statement: str) -> None:
    _recorded_drift(ledger_conn)
    before = _query(ledger_conn, DRIFT_ROWS)
    assert len(before) == 1
    _refused(ledger_conn, statement, "caverns.drift is append-only")
    assert _query(ledger_conn, DRIFT_ROWS) == before


@pytest.mark.parametrize(
    ("version", "fingerprint_version", "differences", "message"),
    [
        (999, 9, '[{"kind": "table"}]', "violates foreign key constraint"),
        (1, 999, '[{"kind": "table"}]', "violates foreign key constraint"),
        (1, 9, "[]", "violates check constraint"),
        (1, 9, '{"kind": "table"}', "violates check constraint"),
        (10, 9, '[{"kind": "table"}]', "duplicate key value"),
    ],
)
def test_the_drift_table_refuses_an_inconsistent_row(
    ledger_conn: Any, version: int, fingerprint_version: int, differences: str, message: str
) -> None:
    _recorded_drift(ledger_conn)
    before = _query(ledger_conn, DRIFT_ROWS)
    _refused(
        ledger_conn,
        "INSERT INTO caverns.drift (version, fingerprint_version, schemas, differences)"
        " VALUES (%s, %s, '{}', %s::jsonb)",
        message,
        (version, fingerprint_version, differences),
    )
    assert _query(ledger_conn, DRIFT_ROWS) == before


def test_a_runner_role_with_the_documented_grants_records_drift(ledger_conn: Any, roles: tuple[str, str, str]) -> None:
    _, _, runner = roles
    _applied_prefix(ledger_conn)
    _run(
        ledger_conn,
        ADD_NOTE,
        f"GRANT USAGE ON SCHEMA nozdormu_test TO {runner}",
        *(grant.format(role=runner) for grant in RUNNER_GRANTS),
        f"SET ROLE {runner}",
    )
    changeset = Changeset("app", [SCHEMA, TABLE, NOOP])
    with pytest.raises(DriftRefused):
        apply(ledger_conn, changeset)
    assert [e.label for e in apply(ledger_conn, changeset, on_drift="warn")] == ["noop"]
    _run(ledger_conn, "RESET ROLE")
    assert [row[0] for row in _query(ledger_conn, DRIFT_ROWS)] == [10]
