"""Epochs against PostgreSQL 18, on both supported drivers: the epoch tables and the registry of tracked tables."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import psycopg
import psycopg2
import pytest

from caverns_of_time import __version__, _connection, epochs, nozdormu
from caverns_of_time.epochs import (
    Column,
    Epoch,
    EpochTable,
    TrackedTable,
    catalog,
    register,
    snapshot,
    tracked,
    unregister,
)
from caverns_of_time.errors import CavernsError, ChangesetError, EpochRefused, LedgerNotInstalled
from caverns_of_time.nozdormu import Changeset, install, preflight
from tests.test_nozdormu_ledger import WAIT_SECONDS, _wait_for_a_waiting_lock

DRIVER_ERRORS = (psycopg.Error, psycopg2.Error)
SCHEMA = "nozdormu_test"
EPOCH_TABLES = ["epoch", "epoch_deletion", "epoch_row", "epoch_source", "epoch_table"]
LEDGER_ROWS = "SELECT version, label, outcome FROM caverns.ledger WHERE changeset = 'caverns' ORDER BY version"
OWN_ROWS = [(1, "ledger", "applied"), (2, "fingerprint", "applied"), (3, "epochs", "applied")]
APP = Changeset("app", [nozdormu.Step("one", "SELECT 1")])


def _query(conn: Any, sql: str, params: tuple[Any, ...] | None = None) -> list[tuple[Any, ...]]:
    """Rows of a read, with the transaction it opened ended: Caverns wants a connection with no open work."""
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = [tuple(row) for row in cursor.fetchall()]
    conn.commit()
    return rows


def _execute(conn: Any, *statements: str) -> None:
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


def _refused(conn: Any, sql: str, message: str) -> None:
    """The statement fails with *message* and its transaction is rolled back."""
    with pytest.raises(DRIVER_ERRORS, match=message), conn.cursor() as cursor:
        cursor.execute(sql)
    conn.rollback()


def _assert_idle(conn: Any) -> None:
    """The connection has no open transaction: both drivers refuse to switch autocommit inside one."""
    conn.autocommit = True
    conn.autocommit = False


def _installed_with_tables(conn: Any, *statements: str) -> None:
    """Caverns installed and the test schema holding the tables the statements create."""
    install(conn)
    _execute(conn, f"CREATE SCHEMA {SCHEMA}", *statements)


def _tables_exist(conn: Any) -> list[str]:
    rows = _query(
        conn,
        "SELECT c.relname::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'caverns' AND c.relname LIKE 'epoch%' AND c.relkind = 'r' ORDER BY c.relname",
    )
    return [row[0] for row in rows]


# --- the own step `epochs` ------------------------------------------------------------------


def test_install_creates_the_five_epoch_tables_and_records_the_step(ledger_conn: Any) -> None:
    entries = install(ledger_conn)
    assert [(e.version, e.label, e.outcome) for e in entries] == OWN_ROWS
    assert entries[2].checksum == nozdormu._OWN_CHANGESET.steps[2].checksum
    assert _tables_exist(ledger_conn) == EPOCH_TABLES
    assert _query(ledger_conn, nozdormu.EPOCHS_EXISTS_SQL) == [(True,)]
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    for table in EPOCH_TABLES:  # installing records no epoch and registers no table
        assert _query(ledger_conn, f"SELECT count(*) FROM caverns.{table}") == [(0,)]


def test_installing_twice_writes_nothing(ledger_conn: Any) -> None:
    install(ledger_conn)
    assert install(ledger_conn) == []
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS


def test_a_database_installed_before_epochs_gets_the_step_with_install(
    ledger_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:  # the previous release's own changeset: `ledger` and `fingerprint`
        patch.setattr(nozdormu, "_OWN_CHANGESET", Changeset("caverns", nozdormu._OWN_CHANGESET.steps[:2]))
        install(ledger_conn)
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS[:2]
    assert _tables_exist(ledger_conn) == []
    with pytest.raises(LedgerNotInstalled, match="call install"):
        preflight(ledger_conn, APP)
    with pytest.raises(LedgerNotInstalled, match="call install"):
        register(ledger_conn, "public", "anything")
    _assert_idle(ledger_conn)
    assert [e.label for e in nozdormu.history(ledger_conn)] == ["fingerprint", "ledger"]  # the old rows still read
    entries = install(ledger_conn)
    assert [(e.version, e.label, e.outcome) for e in entries] == [(3, "epochs", "applied")]
    assert _query(ledger_conn, LEDGER_ROWS) == OWN_ROWS
    assert _tables_exist(ledger_conn) == EPOCH_TABLES
    assert len(preflight(ledger_conn, APP)) == len(APP.steps)


# --- the epoch tables' own rules ------------------------------------------------------------

INSERT_EPOCH = "INSERT INTO caverns.epoch (name, ledger_version, caverns_version) VALUES ('e1', 1, 'test')"
EPOCH_ID = "(SELECT epoch FROM caverns.epoch WHERE name = 'e1')"
INSERT_SOURCE = (
    "INSERT INTO caverns.epoch_source (epoch, schema_name, table_name, key, columns, row_count)"
    f" VALUES ({EPOCH_ID}, 's', 't', '{{id}}', '[]', 1)"
)
INSERT_ROW = (
    "INSERT INTO caverns.epoch_row (epoch, schema_name, table_name, key, row)"
    f" VALUES ({EPOCH_ID}, 's', 't', '[1]', '{{\"id\": 1}}')"
)
INSERT_TOMBSTONE = f"INSERT INTO caverns.epoch_deletion (epoch, caverns_version) VALUES ({EPOCH_ID}, 'test')"


def _with_epoch(conn: Any, *statements: str) -> None:
    """Caverns installed with one epoch, one source and one row of it, then *statements*."""
    install(conn)
    _execute(conn, INSERT_EPOCH, INSERT_SOURCE, INSERT_ROW, *statements)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE caverns.epoch SET name = 'other'",
        "DELETE FROM caverns.epoch",
        "TRUNCATE caverns.epoch CASCADE",  # without CASCADE the foreign keys refuse it first
        "UPDATE caverns.epoch_source SET row_count = 2",
        "DELETE FROM caverns.epoch_source",
        "TRUNCATE caverns.epoch_source CASCADE",
        "UPDATE caverns.epoch_deletion SET caverns_version = 'other'",
        "DELETE FROM caverns.epoch_deletion",
        "TRUNCATE caverns.epoch_deletion",
    ],
)
def test_the_catalog_source_and_deletion_tables_are_append_only(ledger_conn: Any, statement: str) -> None:
    _with_epoch(ledger_conn, INSERT_TOMBSTONE)
    _refused(ledger_conn, statement, "append-only")
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch") == [(1,)]
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_source") == [(1,)]
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_deletion") == [(1,)]


@pytest.mark.parametrize(
    "statement",
    ["UPDATE caverns.epoch_row SET row = '{}'", "DELETE FROM caverns.epoch_row"],
)
def test_the_rows_of_an_epoch_without_a_tombstone_are_kept(ledger_conn: Any, statement: str) -> None:
    _with_epoch(ledger_conn)
    _refused(ledger_conn, statement, "keeps an epoch's rows until the epoch is deleted")
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_row") == [(1,)]


def test_the_rows_of_an_epoch_cannot_be_updated_or_truncated_even_with_a_tombstone(ledger_conn: Any) -> None:
    _with_epoch(ledger_conn, INSERT_TOMBSTONE)
    _refused(ledger_conn, "UPDATE caverns.epoch_row SET row = '{}'", "keeps an epoch's rows")
    _refused(ledger_conn, "TRUNCATE caverns.epoch_row", "append-only")
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_row") == [(1,)]


def test_the_rows_of_an_epoch_can_be_deleted_once_it_has_a_tombstone(ledger_conn: Any) -> None:
    _with_epoch(ledger_conn, INSERT_TOMBSTONE)
    _execute(ledger_conn, "DELETE FROM caverns.epoch_row")
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_row") == [(0,)]
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_source") == [(1,)]  # the catalog stays


def test_a_tombstone_frees_only_its_own_epoch(ledger_conn: Any) -> None:
    _with_epoch(ledger_conn, INSERT_TOMBSTONE)
    _execute(
        ledger_conn,
        "INSERT INTO caverns.epoch (name, ledger_version, caverns_version) VALUES ('e2', 1, 'test')",
        "INSERT INTO caverns.epoch_source (epoch, schema_name, table_name, key, columns, row_count)"
        " SELECT epoch, 's', 't', '{id}', '[]', 1 FROM caverns.epoch WHERE name = 'e2'",
        "INSERT INTO caverns.epoch_row (epoch, schema_name, table_name, key, row)"
        " SELECT epoch, 's', 't', '[1]', '{}' FROM caverns.epoch WHERE name = 'e2'",
    )
    _refused(
        ledger_conn,
        "DELETE FROM caverns.epoch_row WHERE epoch = (SELECT epoch FROM caverns.epoch WHERE name = 'e2')",
        "keeps an epoch's rows",
    )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO caverns.epoch (name, ledger_version, caverns_version) VALUES ('x', 999, 'test')",
        "INSERT INTO caverns.epoch_table (schema_name, table_name, key) VALUES ('s', 't', '{}')",
        "INSERT INTO caverns.epoch_source (epoch, schema_name, table_name, key, columns, row_count)"
        f" VALUES ({EPOCH_ID}, 's', 'u', '{{}}', '[]', 0)",
        "INSERT INTO caverns.epoch_source (epoch, schema_name, table_name, key, columns, row_count)"
        f" VALUES ({EPOCH_ID}, 's', 'u', '{{id}}', '[]', -1)",
    ],
)
def test_the_epoch_tables_refuse_an_inconsistent_row(ledger_conn: Any, statement: str) -> None:
    _with_epoch(ledger_conn)
    _refused(ledger_conn, statement, "violates (check|foreign key) constraint")


def test_the_rows_of_an_epoch_need_their_source_by_the_end_of_the_transaction(ledger_conn: Any) -> None:
    _with_epoch(ledger_conn)
    orphan = (
        "INSERT INTO caverns.epoch_row (epoch, schema_name, table_name, key, row)"
        f" VALUES ({EPOCH_ID}, 's', 'other', '[1]', '{{}}')"
    )
    with pytest.raises(DRIVER_ERRORS, match="foreign key"), ledger_conn.cursor() as cursor:
        cursor.execute(orphan)  # accepted for now: the foreign key is deferred ...
        ledger_conn.commit()  # ... and refused here
    ledger_conn.rollback()
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_row") == [(1,)]


def test_the_registry_has_no_trigger_and_is_mutable(ledger_conn: Any) -> None:
    install(ledger_conn)
    triggers = _query(
        ledger_conn,
        "SELECT c.relname::text, count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'caverns' AND NOT t.tgisinternal"
        " AND c.relname LIKE 'epoch%' GROUP BY c.relname ORDER BY c.relname",
    )
    assert triggers == [("epoch", 2), ("epoch_deletion", 2), ("epoch_row", 2), ("epoch_source", 2)]
    _execute(ledger_conn, "INSERT INTO caverns.epoch_table (schema_name, table_name, key) VALUES ('s', 't', '{id}')")
    _execute(ledger_conn, "UPDATE caverns.epoch_table SET key = '{a,b}'")
    _execute(ledger_conn, "DELETE FROM caverns.epoch_table")


# --- the registry: register -----------------------------------------------------------------

ONE = f"CREATE TABLE {SCHEMA}.one (id int PRIMARY KEY, name text)"


def test_register_takes_the_primary_key_by_default(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    row = register(ledger_conn, SCHEMA, "one")
    assert (row.schema, row.table, row.key) == (SCHEMA, "one", ("id",))
    assert isinstance(row, TrackedTable) and row.registered_at is not None and row.registered_by
    assert tracked(ledger_conn) == [row]
    assert _query(ledger_conn, "SELECT schema_name, table_name, key FROM caverns.epoch_table") == [
        (SCHEMA, "one", ["id"])
    ]
    _assert_idle(ledger_conn)


def test_register_takes_a_composite_primary_key_in_index_order(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, f"CREATE TABLE {SCHEMA}.pair (a int, b int, c int, PRIMARY KEY (c, a))")
    assert register(ledger_conn, SCHEMA, "pair").key == ("c", "a")


def test_register_takes_a_partitioned_table(ledger_conn: Any) -> None:
    _installed_with_tables(
        ledger_conn, f"CREATE TABLE {SCHEMA}.parts (id int PRIMARY KEY, n int) PARTITION BY RANGE (id)"
    )
    assert register(ledger_conn, SCHEMA, "parts").key == ("id",)


def test_register_keeps_the_order_of_an_explicit_key(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, f"CREATE TABLE {SCHEMA}.pair (a int, b int, c int)")
    row = register(ledger_conn, SCHEMA, "pair", key=("c", "a", "b"))
    assert row.key == ("c", "a", "b")
    assert tracked(ledger_conn)[0].key == ("c", "a", "b")


def test_an_explicit_key_may_differ_from_the_primary_key(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    assert register(ledger_conn, SCHEMA, "one", key=["name"]).key == ("name",)


def test_register_with_the_same_key_again_writes_nothing(ledger_conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    _installed_with_tables(ledger_conn, ONE)
    first = register(ledger_conn, SCHEMA, "one")
    with caplog.at_level(logging.INFO, logger=epochs.__name__):
        again = register(ledger_conn, SCHEMA, "one", key=["id"])
    assert again == first  # the stored row, registered_at unchanged
    assert caplog.records == []
    assert tracked(ledger_conn) == [first]


def test_register_with_another_key_replaces_it_and_warns(ledger_conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    _installed_with_tables(ledger_conn, ONE)
    first = register(ledger_conn, SCHEMA, "one")
    with caplog.at_level(logging.WARNING, logger=epochs.__name__):
        second = register(ledger_conn, SCHEMA, "one", key=["id", "name"])
    assert second.key == ("id", "name")
    assert second.registered_at >= first.registered_at
    assert [r.getMessage() for r in caplog.records] == [f"the key of {SCHEMA}.one changes from (id) to (id, name)"]
    assert tracked(ledger_conn) == [second]


def test_register_refuses_a_table_without_a_primary_key_and_no_key(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, f"CREATE TABLE {SCHEMA}.bare (id int)")
    with pytest.raises(EpochRefused, match=rf"{SCHEMA}\.bare has no primary key: pass key="):
        register(ledger_conn, SCHEMA, "bare")
    _assert_idle(ledger_conn)
    assert register(ledger_conn, SCHEMA, "bare", key=["id"]).key == ("id",)


def test_register_refuses_a_missing_table_and_a_view(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE, f"CREATE VIEW {SCHEMA}.v AS SELECT id FROM {SCHEMA}.one")
    for table in ("missing", "v"):
        with pytest.raises(EpochRefused, match=rf"{SCHEMA}\.{table} does not exist or is not a table"):
            register(ledger_conn, SCHEMA, table, key=["id"])
        _assert_idle(ledger_conn)
    with pytest.raises(EpochRefused, match=r"nowhere\.one does not exist"):
        register(ledger_conn, "nowhere", "one")
    assert tracked(ledger_conn) == []


def test_register_refuses_a_key_column_that_is_not_a_live_column(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE, f"ALTER TABLE {SCHEMA}.one ADD COLUMN gone int")
    _execute(ledger_conn, f"ALTER TABLE {SCHEMA}.one DROP COLUMN gone")
    for column in ("nope", "gone", "ID", "tableoid"):
        with pytest.raises(EpochRefused, match=rf"no column '{column}'"):
            register(ledger_conn, SCHEMA, "one", key=["id", column])
        _assert_idle(ledger_conn)
    assert tracked(ledger_conn) == []


def test_register_takes_the_exact_names_of_a_mixed_case_table_with_a_space(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, f'CREATE TABLE {SCHEMA}."Odd Table" ("Odd Id" int PRIMARY KEY, "x y" text)')
    row = register(ledger_conn, SCHEMA, "Odd Table")
    assert (row.table, row.key) == ("Odd Table", ("Odd Id",))
    assert register(ledger_conn, SCHEMA, "Odd Table", key=["x y", "Odd Id"]).key == ("x y", "Odd Id")
    with pytest.raises(EpochRefused, match="odd table does not exist"):  # names are not folded
        register(ledger_conn, SCHEMA, "odd table", key=["x"])
    with pytest.raises(EpochRefused, match="no column 'odd id'"):
        register(ledger_conn, SCHEMA, "Odd Table", key=["odd id"])


def test_a_hostile_name_is_a_name_not_sql(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    with pytest.raises(EpochRefused, match="does not exist"):
        register(ledger_conn, SCHEMA, "one'; DROP SCHEMA caverns CASCADE; --")
    with pytest.raises(EpochRefused, match="no column"):
        register(ledger_conn, SCHEMA, "one", key=['id"; DROP SCHEMA caverns CASCADE; --'])
    assert _tables_exist(ledger_conn) == EPOCH_TABLES


# --- the registry: unregister and tracked ---------------------------------------------------


def test_unregister_removes_the_row(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE, f"CREATE TABLE {SCHEMA}.two (id int PRIMARY KEY)")
    register(ledger_conn, SCHEMA, "one")
    kept = register(ledger_conn, SCHEMA, "two")
    assert unregister(ledger_conn, SCHEMA, "one") is None
    assert tracked(ledger_conn) == [kept]
    _assert_idle(ledger_conn)


def test_unregister_works_for_a_table_that_no_longer_exists(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    register(ledger_conn, SCHEMA, "one")
    _execute(ledger_conn, f"DROP TABLE {SCHEMA}.one")
    unregister(ledger_conn, SCHEMA, "one")
    assert tracked(ledger_conn) == []


def test_unregister_refuses_a_table_that_is_not_registered(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    with pytest.raises(EpochRefused, match=rf"{SCHEMA}\.one is not registered"):
        unregister(ledger_conn, SCHEMA, "one")
    _assert_idle(ledger_conn)


def test_tracked_is_empty_before_anything_is_registered(ledger_conn: Any) -> None:
    install(ledger_conn)
    assert tracked(ledger_conn) == []
    _assert_idle(ledger_conn)


def test_tracked_is_ordered_by_schema_and_table(ledger_conn: Any) -> None:
    _installed_with_tables(
        ledger_conn,
        f"CREATE TABLE {SCHEMA}.b (id int PRIMARY KEY)",
        f"CREATE TABLE {SCHEMA}.a (id int PRIMARY KEY)",
        f"CREATE TABLE {SCHEMA}.c (id int PRIMARY KEY)",
    )
    for table in ("c", "a", "b"):
        register(ledger_conn, SCHEMA, table)
    _execute(
        ledger_conn,
        "INSERT INTO caverns.epoch_table (schema_name, table_name, key) VALUES ('aaa', 'z', '{id}')",
        "INSERT INTO caverns.epoch_table (schema_name, table_name, key) VALUES ('zzz', 'a', '{id}')",
    )
    assert [(t.schema, t.table) for t in tracked(ledger_conn)] == [
        ("aaa", "z"),
        (SCHEMA, "a"),
        (SCHEMA, "b"),
        (SCHEMA, "c"),
        ("zzz", "a"),
    ]
    _assert_idle(ledger_conn)


def test_the_registry_survives_a_second_install(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    row = register(ledger_conn, SCHEMA, "one")
    assert install(ledger_conn) == []
    assert tracked(ledger_conn) == [row]


# --- refusals every function shares ---------------------------------------------------------

CALLS: dict[str, Callable[[Any], object]] = {
    "register": lambda conn: register(conn, SCHEMA, "one"),
    "unregister": lambda conn: unregister(conn, SCHEMA, "one"),
    "tracked": tracked,
    "snapshot": lambda conn: snapshot(conn, "e"),
    "catalog": catalog,
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_function_refuses_a_database_without_the_epoch_tables(ledger_conn: Any, name: str) -> None:
    with pytest.raises(LedgerNotInstalled, match="epoch tables are not installed.*call install"):
        CALLS[name](ledger_conn)
    _assert_idle(ledger_conn)


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_function_refuses_a_connection_with_autocommit_on(ledger_conn: Any, name: str) -> None:
    install(ledger_conn)
    ledger_conn.autocommit = True
    try:
        with pytest.raises(CavernsError, match="autocommit"):
            CALLS[name](ledger_conn)
    finally:
        ledger_conn.autocommit = False


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_function_refuses_a_connection_with_uncommitted_work(ledger_conn: Any, name: str) -> None:
    install(ledger_conn)
    with ledger_conn.cursor() as cursor:
        cursor.execute("CREATE TABLE public.epochs_caller_work (id int)")
    try:
        with pytest.raises(CavernsError, match="uncommitted work"):
            CALLS[name](ledger_conn)
    finally:
        ledger_conn.rollback()


@pytest.mark.parametrize(
    ("schema", "table"),
    [("", "t"), ("s", ""), (None, "t"), ("s", None), (3, "t"), ("s", b"t")],
)
@pytest.mark.parametrize("function", [register, unregister])
def test_a_schema_or_table_that_is_not_a_name_is_refused(
    ledger_conn: Any, function: Callable[..., object], schema: Any, table: Any
) -> None:
    with pytest.raises(ChangesetError, match="schema|table"):  # refused before the database is asked
        function(ledger_conn, schema, table)
    _assert_idle(ledger_conn)


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("id", "not the string 'id'"),
        ([], "key is empty"),
        ((), "key is empty"),
        (["id", "id"], "repeats the column 'id'"),
        (["id", None], "key column None is not a string"),
        ([1], "key column 1 is not a string"),
        (5, "not 5"),
    ],
)
def test_a_key_that_is_not_a_list_of_distinct_names_is_refused(ledger_conn: Any, key: Any, message: str) -> None:
    with pytest.raises(ChangesetError, match=message):  # refused before the database is asked
        register(ledger_conn, SCHEMA, "one", key=key)
    _assert_idle(ledger_conn)
    _installed_with_tables(ledger_conn, ONE)
    with pytest.raises(ChangesetError, match=message):
        register(ledger_conn, SCHEMA, "one", key=key)
    assert tracked(ledger_conn) == []


# --- snapshot -------------------------------------------------------------------------------

TWO = f"CREATE TABLE {SCHEMA}.two (a int, b text, c numeric(8, 2), PRIMARY KEY (b, a))"
STORED_ROWS = "SELECT key, row FROM caverns.epoch_row WHERE epoch = %s AND schema_name = %s AND table_name = %s"
ADVISORY_LOCKS = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"


def _by_row(rows: list[Any]) -> list[Any]:
    return sorted(rows, key=str)


def _stored(conn: Any, epoch: int, table: str, schema: str = SCHEMA) -> list[tuple[Any, Any]]:
    """The (key, row) pairs stored for the table in the epoch, in a fixed order."""
    return _by_row(_query(conn, STORED_ROWS, (epoch, schema, table)))


def _nothing_written(conn: Any) -> None:
    for table in ("epoch", "epoch_source", "epoch_row", "epoch_deletion"):
        assert _query(conn, f"SELECT count(*) FROM caverns.{table}") == [(0,)], table
    assert _query(conn, ADVISORY_LOCKS) == [(0,)]
    _assert_idle(conn)


def test_snapshot_copies_every_registered_table_under_one_epoch(ledger_conn: Any) -> None:
    _installed_with_tables(
        ledger_conn,
        ONE,
        TWO,
        f"CREATE TABLE {SCHEMA}.untracked (id int PRIMARY KEY)",
        f"INSERT INTO {SCHEMA}.one VALUES (1, 'x'), (2, NULL)",
        f"INSERT INTO {SCHEMA}.two VALUES (1, 'p', 1.50), (2, 'p', NULL), (1, 'q', 3)",
        f"INSERT INTO {SCHEMA}.untracked VALUES (9)",
    )
    register(ledger_conn, SCHEMA, "two")
    register(ledger_conn, SCHEMA, "one")
    top = max(e.version for e in nozdormu.history(ledger_conn))
    taken = snapshot(ledger_conn, "first")
    _assert_idle(ledger_conn)
    assert isinstance(taken, Epoch)
    assert (taken.name, taken.ledger_version, taken.caverns_version, taken.deleted_at) == (
        "first",
        top,
        __version__,
        None,
    )
    assert taken.taken_at is not None and taken.taken_by
    assert taken.tables == (
        EpochTable(SCHEMA, "one", ("id",), (Column("id", "integer"), Column("name", "text")), 2),
        EpochTable(
            SCHEMA,
            "two",
            ("b", "a"),
            (Column("a", "integer"), Column("b", "text"), Column("c", "numeric(8,2)")),
            3,
        ),
    )
    assert catalog(ledger_conn) == [taken]
    one = _by_row(_query(ledger_conn, f"SELECT to_jsonb(t) FROM {SCHEMA}.one t"))
    two = _by_row(_query(ledger_conn, f"SELECT to_jsonb(t) FROM {SCHEMA}.two t"))
    assert [row for _, row in _stored(ledger_conn, taken.epoch, "one")] == [r[0] for r in one]
    assert [row for _, row in _stored(ledger_conn, taken.epoch, "two")] == [r[0] for r in two]
    assert [key for key, _ in _stored(ledger_conn, taken.epoch, "one")] == [{"id": 1}, {"id": 2}]
    assert [key for key, _ in _stored(ledger_conn, taken.epoch, "two")] == [
        {"a": 1, "b": "p"},
        {"a": 1, "b": "q"},
        {"a": 2, "b": "p"},
    ]
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch_row WHERE table_name = 'untracked'") == [(0,)]
    sources = _query(ledger_conn, "SELECT table_name, key, row_count FROM caverns.epoch_source ORDER BY table_name")
    assert sources == [("one", ["id"], 2), ("two", ["b", "a"], 3)]
    assert _query(ledger_conn, ADVISORY_LOCKS) == [(0,)]


def test_snapshot_logs_what_it_took(ledger_conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    _installed_with_tables(ledger_conn, ONE, f"INSERT INTO {SCHEMA}.one VALUES (1, 'x')")
    register(ledger_conn, SCHEMA, "one")
    top = max(e.version for e in nozdormu.history(ledger_conn))
    with caplog.at_level(logging.INFO, logger=epochs.__name__):
        snapshot(ledger_conn, "logged")
    assert [r.getMessage() for r in caplog.records] == [
        f"took epoch logged at ledger version {top}: 1 rows in 1 tables"
    ]


def test_snapshot_of_an_empty_table_records_it_with_no_rows(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    register(ledger_conn, SCHEMA, "one")
    taken = snapshot(ledger_conn, "empty")
    assert [(t.table, t.row_count) for t in taken.tables] == [("one", 0)]
    assert _stored(ledger_conn, taken.epoch, "one") == []


def test_a_second_snapshot_is_a_second_epoch_of_the_changed_table(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE, f"INSERT INTO {SCHEMA}.one VALUES (1, 'x')")
    register(ledger_conn, SCHEMA, "one")
    first = snapshot(ledger_conn, "first")
    _execute(ledger_conn, f"UPDATE {SCHEMA}.one SET name = 'y'", f"INSERT INTO {SCHEMA}.one VALUES (2, 'z')")
    second = snapshot(ledger_conn, "second")
    assert second.epoch > first.epoch
    assert [t.row_count for t in first.tables] == [1] and [t.row_count for t in second.tables] == [2]
    assert [row["name"] for _, row in _stored(ledger_conn, first.epoch, "one")] == ["x"]
    assert [row["name"] for _, row in _stored(ledger_conn, second.epoch, "one")] == ["y", "z"]


def test_snapshot_prints_a_user_type_qualified_whatever_the_search_path(ledger_conn: Any) -> None:
    _installed_with_tables(
        ledger_conn,
        f"CREATE TYPE {SCHEMA}.mood AS ENUM ('ok', 'sad')",
        f"CREATE TABLE {SCHEMA}.felt (id int PRIMARY KEY, mood {SCHEMA}.mood, moods {SCHEMA}.mood[])",
        f"INSERT INTO {SCHEMA}.felt VALUES (1, 'sad', '{{ok,sad}}')",
        f"SET search_path TO {SCHEMA}, public",
    )
    register(ledger_conn, SCHEMA, "felt")
    taken = snapshot(ledger_conn, "mood")
    assert [(c.name, c.type) for c in taken.tables[0].columns] == [
        ("id", "integer"),
        ("mood", f"{SCHEMA}.mood"),
        ("moods", f"{SCHEMA}.mood[]"),
    ]
    assert _stored(ledger_conn, taken.epoch, "felt")[0][1] == {"id": 1, "mood": "sad", "moods": ["ok", "sad"]}


def test_snapshot_takes_hostile_names_as_names(ledger_conn: Any) -> None:
    table = 'x"; DROP TABLE nozdormu_test.y; --'
    quoted = '"My Schema"."x""; DROP TABLE nozdormu_test.y; --"'
    _installed_with_tables(
        ledger_conn,
        f"CREATE TABLE {SCHEMA}.y (id int)",
        'CREATE SCHEMA "My Schema"',
        f'CREATE TABLE {quoted} ("50%" int PRIMARY KEY, "a b" text)',
        f"INSERT INTO {quoted} VALUES (1, 'it''s'), (2, 'back\\slash')",
    )
    try:
        register(ledger_conn, "My Schema", table)
        taken = snapshot(ledger_conn, "hostile; DROP SCHEMA caverns CASCADE")
        assert [(t.schema, t.table, t.key, t.row_count) for t in taken.tables] == [("My Schema", table, ("50%",), 2)]
        assert [c.name for c in taken.tables[0].columns] == ["50%", "a b"]
        stored = _stored(ledger_conn, taken.epoch, table, "My Schema")
        assert stored == [({"50%": 1}, {"50%": 1, "a b": "it's"}), ({"50%": 2}, {"50%": 2, "a b": "back\\slash"})]
        assert _tables_exist(ledger_conn) == EPOCH_TABLES
        assert _query(ledger_conn, f"SELECT count(*) FROM {SCHEMA}.y") == [(0,)]
    finally:
        ledger_conn.rollback()
        _execute(ledger_conn, 'DROP SCHEMA IF EXISTS "My Schema" CASCADE')


def test_snapshot_refuses_without_a_registered_table(ledger_conn: Any) -> None:
    install(ledger_conn)
    with pytest.raises(EpochRefused, match=r"no table is registered: call register\(\) first"):
        snapshot(ledger_conn, "none")
    _nothing_written(ledger_conn)


def test_snapshot_refuses_a_name_that_is_taken(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    register(ledger_conn, SCHEMA, "one")
    taken = snapshot(ledger_conn, "once")
    with pytest.raises(EpochRefused, match=rf"the epoch name 'once' is taken \(epoch {taken.epoch}\)"):
        snapshot(ledger_conn, "once")
    _assert_idle(ledger_conn)
    assert _query(ledger_conn, "SELECT count(*) FROM caverns.epoch") == [(1,)]
    assert _query(ledger_conn, ADVISORY_LOCKS) == [(0,)]
    _execute(ledger_conn, INSERT_TOMBSTONE.replace(EPOCH_ID, str(taken.epoch)))  # a deleted epoch keeps its name
    with pytest.raises(EpochRefused, match="is taken"):
        snapshot(ledger_conn, "once")


@pytest.mark.parametrize("name", ["", " lead", "trail ", "two\nlines", None, 3])
def test_snapshot_refuses_a_bad_name(ledger_conn: Any, name: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    register(ledger_conn, SCHEMA, "one")
    with pytest.raises(ChangesetError, match="epoch name"):
        snapshot(ledger_conn, name)
    _nothing_written(ledger_conn)


def test_snapshot_refuses_before_install_and_releases_the_lock(ledger_conn: Any) -> None:
    with pytest.raises(LedgerNotInstalled, match="call install"):
        snapshot(ledger_conn, "early")
    assert _query(ledger_conn, ADVISORY_LOCKS) == [(0,)]


def test_snapshot_refuses_a_registered_table_that_is_gone(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE, f"CREATE TABLE {SCHEMA}.zz (id int PRIMARY KEY)")
    register(ledger_conn, SCHEMA, "one")
    register(ledger_conn, SCHEMA, "zz")
    _execute(ledger_conn, f"DROP TABLE {SCHEMA}.zz")
    with pytest.raises(EpochRefused, match=rf"{SCHEMA}\.zz is registered but does not exist"):
        snapshot(ledger_conn, "gone")  # nothing of `one`, which came first, is kept either
    _nothing_written(ledger_conn)


def test_snapshot_refuses_a_key_column_that_is_gone(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    register(ledger_conn, SCHEMA, "one", key=["name"])
    _execute(ledger_conn, f"ALTER TABLE {SCHEMA}.one DROP COLUMN name")
    with pytest.raises(EpochRefused, match=rf"{SCHEMA}\.one has no column 'name' for the key"):
        snapshot(ledger_conn, "nokey")
    _nothing_written(ledger_conn)


def test_snapshot_refuses_a_key_with_duplicates_and_keeps_nothing(ledger_conn: Any) -> None:
    _installed_with_tables(
        ledger_conn,
        ONE,
        f"CREATE TABLE {SCHEMA}.dup (a int, b int)",
        f"INSERT INTO {SCHEMA}.one VALUES (1, 'x')",
        f"INSERT INTO {SCHEMA}.dup VALUES (1, 1), (1, 2), (2, 3)",
    )
    register(ledger_conn, SCHEMA, "dup", key=["a"])
    register(ledger_conn, SCHEMA, "one")
    with pytest.raises(EpochRefused, match=rf"the key \(a\) of {SCHEMA}\.dup is not unique"):
        snapshot(ledger_conn, "dup")  # `dup` sorts before `one`: the rows copied before the refusal go too
    _nothing_written(ledger_conn)
    _execute(ledger_conn, f"UPDATE {SCHEMA}.dup SET a = b")
    assert [t.row_count for t in snapshot(ledger_conn, "dup").tables] == [3, 1]


def test_snapshot_refuses_a_key_with_nulls(ledger_conn: Any) -> None:
    _installed_with_tables(
        ledger_conn,
        f"CREATE TABLE {SCHEMA}.nul (a int, b int)",
        f"INSERT INTO {SCHEMA}.nul VALUES (1, 1), (NULL, 2)",
    )
    register(ledger_conn, SCHEMA, "nul", key=["b", "a"])
    with pytest.raises(EpochRefused, match=rf"the key \(b, a\) of {SCHEMA}\.nul has nulls"):
        snapshot(ledger_conn, "nul")
    _nothing_written(ledger_conn)


def test_snapshot_releases_the_session_lock(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    register(ledger_conn, SCHEMA, "one")
    snapshot(ledger_conn, "ok")
    assert _query(ledger_conn, ADVISORY_LOCKS) == [(0,)]


def test_snapshot_waits_for_the_lock_and_sees_what_the_holder_committed(ledger_conn: Any, second_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE, f"INSERT INTO {SCHEMA}.one VALUES (1, 'x')")
    register(ledger_conn, SCHEMA, "one")
    with second_conn.cursor() as cursor:
        _connection.lock(cursor)
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["epoch"] = snapshot(ledger_conn, "waited")
        except BaseException as error:  # reported by the assertions below
            result["error"] = error

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    try:
        _wait_for_a_waiting_lock(second_conn)
        with second_conn.cursor() as cursor:  # the other runner's work, under the lock it holds
            cursor.execute(f"INSERT INTO {SCHEMA}.one VALUES (2, 'y')")
        second_conn.commit()
    finally:
        second_conn.rollback()  # releases the lock when the test failed before the commit
        runner.join(WAIT_SECONDS)
    assert not runner.is_alive()
    assert "error" not in result, result
    assert [t.row_count for t in result["epoch"].tables] == [2]


# --- catalog --------------------------------------------------------------------------------


def test_catalog_is_empty_before_any_snapshot(ledger_conn: Any) -> None:
    install(ledger_conn)
    assert catalog(ledger_conn) == []
    _assert_idle(ledger_conn)


def test_catalog_lists_the_epochs_newest_first_with_their_tables(ledger_conn: Any) -> None:
    _installed_with_tables(
        ledger_conn,
        ONE,
        TWO,
        f"INSERT INTO {SCHEMA}.one VALUES (1, 'x')",
        f"INSERT INTO {SCHEMA}.two VALUES (1, 'p', 2)",
    )
    register(ledger_conn, SCHEMA, "one")
    first = snapshot(ledger_conn, "first")
    register(ledger_conn, SCHEMA, "two")
    second = snapshot(ledger_conn, "second")
    listed = catalog(ledger_conn)
    assert listed == [second, first]
    assert [e.name for e in listed] == ["second", "first"]
    assert [[(t.table, t.row_count) for t in e.tables] for e in listed] == [[("one", 1), ("two", 1)], [("one", 1)]]
    assert [e.deleted_at for e in listed] == [None, None]
    assert [e.name for e in catalog(ledger_conn, limit=1)] == ["second"]
    assert [e.name for e in catalog(ledger_conn, limit=5)] == ["second", "first"]
    assert [e.name for e in catalog(ledger_conn, limit=2**63 - 1)] == ["second", "first"]
    _assert_idle(ledger_conn)


def test_catalog_shows_the_deletion_time_and_keeps_the_tables(ledger_conn: Any) -> None:
    _installed_with_tables(ledger_conn, ONE)
    register(ledger_conn, SCHEMA, "one")
    taken = snapshot(ledger_conn, "gone")
    _execute(ledger_conn, INSERT_TOMBSTONE.replace(EPOCH_ID, str(taken.epoch)))
    (listed,) = catalog(ledger_conn)
    assert listed.deleted_at is not None
    assert listed.tables == taken.tables


@pytest.mark.parametrize("limit", [0, -1, True, False, "x", 1.5, 2**63])
def test_catalog_refuses_an_invalid_limit(ledger_conn: Any, limit: Any) -> None:
    install(ledger_conn)
    with pytest.raises(ChangesetError, match="limit must be a positive integer up to 9223372036854775807"):
        catalog(ledger_conn, limit=limit)
    _assert_idle(ledger_conn)
