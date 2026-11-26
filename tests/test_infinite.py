"""Infinite's fingerprint against PostgreSQL 18, on both supported drivers, and its hash."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from typing import Any

import pytest

from caverns_of_time import _fingerprint, infinite
from caverns_of_time.errors import CavernsError, ChangesetError, LedgerNotInstalled, NoFingerprint
from caverns_of_time.infinite import Difference, Drift, Fingerprint, drift, fingerprint
from caverns_of_time.nozdormu import Changeset, Status, Step, apply, baseline, history, install

SCHEMA = "infinite_test"
EXT_SCHEMA = "infinite_ext"
OLD_KINDS = {"table", "column", "index", "constraint", "view", "function"}
NEW_KINDS = {"trigger", "row_security", "policy", "enum"}  # added after the first release
KINDS = OLD_KINDS | NEW_KINDS
# Everything these tests create, dropped before and after each: two schemas (the extension goes with its
# schema) and the foreign data wrapper with its server, which no schema holds.
TEST_DROPS = (
    f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
    f"DROP SCHEMA IF EXISTS {EXT_SCHEMA} CASCADE",
    "DROP SERVER IF EXISTS infinite_server CASCADE",
    "DROP FOREIGN DATA WRAPPER IF EXISTS infinite_fdw CASCADE",
)
TABLE = f"CREATE TABLE {SCHEMA}.t (id integer NOT NULL, note text DEFAULT 'a')"
NOOP = "SET LOCAL application_name = 'noop'"  # a step Murozond calls safe that changes nothing


def _run(conn: Any, *statements: str) -> None:
    conn.rollback()
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


def _query(conn: Any, sql: str) -> list[tuple[Any, ...]]:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        rows = [tuple(row) for row in cursor.fetchall()]
    conn.commit()
    return rows


def _assert_idle(conn: Any) -> None:
    """The connection has no open transaction: both drivers refuse to switch autocommit inside one."""
    conn.autocommit = True
    conn.autocommit = False


@pytest.fixture
def conn(ledger_conn: Any) -> Iterator[Any]:
    """`ledger_conn` with the schemas, extension and foreign data wrapper of these tests dropped around each test."""
    _run(ledger_conn, *TEST_DROPS)
    yield ledger_conn
    _run(ledger_conn, *TEST_DROPS)


def _created(conn: Any, *statements: str) -> None:
    _run(conn, f"CREATE SCHEMA {SCHEMA}", *statements)


def _fp(conn: Any, *schemas: str) -> Fingerprint:
    return fingerprint(conn, schemas=schemas or [SCHEMA])


def _doc(conn: Any) -> dict[str, Any]:
    """The document of the test schema."""
    document: dict[str, Any] = _fp(conn).document[SCHEMA]
    return document


def _changed(conn: Any, *statements: str) -> Fingerprint:
    """The fingerprint after *statements*, which must differ from the one before them."""
    before = _fp(conn)
    _run(conn, *statements)
    after = _fp(conn)
    assert after.hash != before.hash
    return after


# --- the hash ------------------------------------------------------------------------------


def test_the_hash_ignores_key_order_and_sees_content() -> None:
    assert _fingerprint.hash_document({"b": 1, "a": [2]}) == _fingerprint.hash_document({"a": [2], "b": 1})
    assert _fingerprint.hash_document({"a": 1}) != _fingerprint.hash_document({"a": 2})
    assert re.fullmatch(r"[0-9a-f]{64}", _fingerprint.hash_document({}))


def test_infinite_re_exports_the_fingerprint_type() -> None:
    assert infinite.Fingerprint is _fingerprint.Fingerprint
    assert sorted(infinite.__all__) == ["Difference", "Drift", "Fingerprint", "drift", "fingerprint"]
    assert [field.name for field in dataclasses.fields(Fingerprint)] == [
        "schemas",
        "document",
        "hash",
        "server_version",
    ]


# --- the document's shape ------------------------------------------------------------------


def test_an_empty_schema_has_all_ten_kinds_empty(conn: Any) -> None:
    _created(conn)
    result = _fp(conn)
    assert result.schemas == (SCHEMA,)
    assert result.document == {SCHEMA: {kind: {} for kind in KINDS}}
    assert re.fullmatch(r"[0-9a-f]{64}", result.hash)


def test_a_dropped_table_changes_the_hash(conn: Any) -> None:
    _created(conn, TABLE)
    assert _changed(conn, f"DROP TABLE {SCHEMA}.t").document[SCHEMA]["table"] == {}


def test_the_fingerprint_is_stable_and_names_its_schemas_sorted_and_unique(conn: Any) -> None:
    _created(conn, TABLE)
    first = fingerprint(conn, schemas=["public", SCHEMA, "public"])
    assert first.schemas == (SCHEMA, "public")
    assert fingerprint(conn, schemas=("public", SCHEMA)) == first
    assert set(first.document) == {"public", SCHEMA}


def test_the_baseline_records_the_hash_of_the_fingerprint(conn: Any) -> None:
    install(conn)
    _created(conn, TABLE)
    entry = baseline(conn, Changeset("app", [Step("schema", f"CREATE SCHEMA {SCHEMA}")]), schemas=[SCHEMA])
    assert entry.fingerprint == _fp(conn).hash


# --- the kinds -------------------------------------------------------------------------


def test_tables_have_their_kind_and_a_new_table_changes_the_hash(conn: Any) -> None:
    _run(
        conn,
        "CREATE FOREIGN DATA WRAPPER infinite_fdw",
        "CREATE SERVER infinite_server FOREIGN DATA WRAPPER infinite_fdw",
    )
    _created(
        conn,
        TABLE,
        f"CREATE TABLE {SCHEMA}.pt (id integer) PARTITION BY RANGE (id)",
        f"CREATE TABLE {SCHEMA}.pt_1 PARTITION OF {SCHEMA}.pt FOR VALUES FROM (0) TO (10)",
        f"CREATE FOREIGN TABLE {SCHEMA}.ft (id integer) SERVER infinite_server",
    )
    assert _doc(conn)["table"] == {"t": {"kind": "r"}, "pt": {"kind": "p"}, "pt_1": {"kind": "r"}, "ft": {"kind": "f"}}
    _changed(conn, f"CREATE TABLE {SCHEMA}.bare ()")  # a table without columns still counts


def test_columns_have_type_not_null_default_and_nothing_else_when_plain(conn: Any) -> None:
    _created(conn, TABLE)
    plain = {"generated": None, "identity": None, "collation": None}
    assert _doc(conn)["column"] == {
        "t.id": {"type": "integer", "not_null": True, "default": None, **plain},
        "t.note": {"type": "text", "not_null": False, "default": "'a'::text", **plain},
    }


def test_a_new_column_changes_the_hash(conn: Any) -> None:
    _created(conn, TABLE)
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t ADD COLUMN extra numeric(10, 2)")
    assert after.document[SCHEMA]["column"]["t.extra"]["type"] == "numeric(10,2)"


def test_a_changed_default_changes_the_hash(conn: Any) -> None:
    _created(conn, TABLE)
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t ALTER COLUMN note SET DEFAULT 'b'")
    assert after.document[SCHEMA]["column"]["t.note"]["default"] == "'b'::text"


def test_a_changed_not_null_changes_the_hash(conn: Any) -> None:
    _created(conn, TABLE)
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t ALTER COLUMN note SET NOT NULL")
    assert after.document[SCHEMA]["column"]["t.note"]["not_null"] is True
    assert after.document[SCHEMA]["constraint"] == {}  # PostgreSQL 18's NOT NULL constraint is not listed twice


def test_a_generated_column_has_its_expression_and_no_default(conn: Any) -> None:
    _created(conn, f"CREATE TABLE {SCHEMA}.g (a integer, b integer GENERATED ALWAYS AS (a * 2) STORED)")
    column = _doc(conn)["column"]["g.b"]
    assert (column["default"], column["identity"]) == (None, None)
    assert "a * 2" in column["generated"]
    assert _doc(conn)["column"]["g.a"]["generated"] is None
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.g ALTER COLUMN b SET EXPRESSION AS (a * 3)")
    assert "a * 3" in after.document[SCHEMA]["column"]["g.b"]["generated"]


def test_identity_columns_say_always_or_by_default(conn: Any) -> None:
    _created(
        conn,
        f"CREATE TABLE {SCHEMA}.i (a integer GENERATED ALWAYS AS IDENTITY,"
        " b integer GENERATED BY DEFAULT AS IDENTITY, c integer)",
    )
    columns = _doc(conn)["column"]
    assert [columns[f"i.{name}"]["identity"] for name in "abc"] == ["always", "by default", None]
    assert columns["i.a"]["default"] is None
    assert columns["i.a"]["not_null"] is True


def test_a_collation_is_recorded_only_when_it_differs_from_the_types_default(conn: Any) -> None:
    _created(conn, f'CREATE TABLE {SCHEMA}.c (plain text, special text COLLATE "C", number integer)')
    columns = _doc(conn)["column"]
    assert [columns[f"c.{name}"]["collation"] for name in ("plain", "special", "number")] == [None, "C", None]
    after = _changed(conn, f'ALTER TABLE {SCHEMA}.c ALTER COLUMN plain TYPE text COLLATE "POSIX"')
    assert after.document[SCHEMA]["column"]["c.plain"]["collation"] == "POSIX"


def test_an_index_is_its_definition_and_a_new_index_changes_the_hash(conn: Any) -> None:
    _created(conn, TABLE)
    assert _doc(conn)["index"] == {}
    after = _changed(conn, f"CREATE INDEX t_note_idx ON {SCHEMA}.t (note)")
    assert after.document[SCHEMA]["index"] == {
        "t_note_idx": f"CREATE INDEX t_note_idx ON {SCHEMA}.t USING btree (note)"
    }
    assert _changed(conn, f"DROP INDEX {SCHEMA}.t_note_idx").document[SCHEMA]["index"] == {}


def test_a_constraint_is_its_definition_and_a_new_constraint_changes_the_hash(conn: Any) -> None:
    _created(conn, TABLE)
    assert _doc(conn)["constraint"] == {}
    after = _changed(
        conn,
        f"ALTER TABLE {SCHEMA}.t ADD CONSTRAINT t_pk PRIMARY KEY (id)",
        f"ALTER TABLE {SCHEMA}.t ADD CONSTRAINT t_short CHECK (length(note) < 5)",
        f"CREATE TABLE {SCHEMA}.u (t_id integer CONSTRAINT u_t REFERENCES {SCHEMA}.t (id))",
    )
    assert after.document[SCHEMA]["constraint"] == {
        "t.t_pk": "PRIMARY KEY (id)",
        "t.t_short": "CHECK ((length(note) < 5))",
        "u.u_t": f"FOREIGN KEY (t_id) REFERENCES {SCHEMA}.t(id)",
    }
    assert "t_pk" in after.document[SCHEMA]["index"]  # the key's index is an index too


def test_a_view_is_its_kind_and_definition_and_a_changed_definition_changes_the_hash(conn: Any) -> None:
    _created(conn, TABLE, f"CREATE VIEW {SCHEMA}.v AS SELECT id FROM {SCHEMA}.t")
    view = _doc(conn)["view"]["v"]
    assert view["kind"] == "v"
    assert f"FROM {SCHEMA}.t" in view["definition"]
    after = _changed(conn, f"CREATE OR REPLACE VIEW {SCHEMA}.v AS SELECT id, note FROM {SCHEMA}.t")
    assert "note" in after.document[SCHEMA]["view"]["v"]["definition"]
    assert not [key for key in after.document[SCHEMA]["column"] if key.startswith("v.")]  # a view is its definition


def test_a_materialized_view_has_kind_m(conn: Any) -> None:
    _created(conn, TABLE, f"CREATE MATERIALIZED VIEW {SCHEMA}.mv AS SELECT id FROM {SCHEMA}.t")
    assert _doc(conn)["view"]["mv"]["kind"] == "m"
    assert _doc(conn)["table"] == {"t": {"kind": "r"}}


def test_functions_and_procedures_are_keyed_by_identity_arguments(conn: Any) -> None:
    _created(
        conn,
        f"CREATE FUNCTION {SCHEMA}.f(a integer, b text) RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        f"CREATE FUNCTION {SCHEMA}.f(a integer) RETURNS integer LANGUAGE sql AS 'SELECT 2'",
        f"CREATE PROCEDURE {SCHEMA}.p() LANGUAGE sql AS 'SELECT 1'",
        f"CREATE AGGREGATE {SCHEMA}.agg(integer) (SFUNC = int4pl, STYPE = integer)",
    )
    functions = _doc(conn)["function"]
    assert set(functions) == {"f(a integer, b text)", "f(a integer)", "p()"}  # an aggregate is not covered
    assert f"{SCHEMA}.f(a integer)" in functions["f(a integer)"]
    after = _changed(
        conn, f"CREATE OR REPLACE FUNCTION {SCHEMA}.f(a integer) RETURNS integer LANGUAGE sql AS 'SELECT 3'"
    )
    assert "SELECT 3" in after.document[SCHEMA]["function"]["f(a integer)"]


TRIGGER_FUNCTION = f"CREATE FUNCTION {SCHEMA}.trg() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'"
TRIGGER = f"CREATE TRIGGER t_ins BEFORE INSERT ON {SCHEMA}.t FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.trg()"
POLICY = f"CREATE POLICY p_all ON {SCHEMA}.t USING (id > 0)"
ENUM = f"CREATE TYPE {SCHEMA}.mood AS ENUM ('sad', 'happy')"
PK = f"ALTER TABLE {SCHEMA}.t ADD CONSTRAINT t_pk PRIMARY KEY (id)"


def test_triggers_have_their_definition_and_enabled_state(conn: Any) -> None:
    _created(
        conn,
        TABLE,
        PK,
        f"CREATE TABLE {SCHEMA}.u (t_id integer REFERENCES {SCHEMA}.t (id))",  # a foreign key's internal triggers
        f"CREATE VIEW {SCHEMA}.v AS SELECT id FROM {SCHEMA}.t",
        TRIGGER_FUNCTION,
        TRIGGER,
        f"CREATE TRIGGER t_off AFTER UPDATE ON {SCHEMA}.t FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.trg()",
        f"ALTER TABLE {SCHEMA}.t DISABLE TRIGGER t_off",
        f"CREATE CONSTRAINT TRIGGER t_ct AFTER INSERT ON {SCHEMA}.t DEFERRABLE INITIALLY DEFERRED"
        f" FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.trg()",
        f"CREATE TRIGGER v_ins INSTEAD OF INSERT ON {SCHEMA}.v FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.trg()",
        f"ALTER TABLE {SCHEMA}.t ENABLE REPLICA TRIGGER t_ins",
    )
    triggers = _doc(conn)["trigger"]
    assert set(triggers) == {"t.t_ins", "t.t_off", "t.t_ct", "v.v_ins"}  # no key for the internal ones
    assert triggers["t.t_ins"] == {
        "definition": f"CREATE TRIGGER t_ins BEFORE INSERT ON {SCHEMA}.t FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.trg()",
        "enabled": "replica",
    }
    assert [triggers[key]["enabled"] for key in ("t.t_off", "t.t_ct", "v.v_ins")] == ["disabled", "origin", "origin"]
    assert "CONSTRAINT TRIGGER" in triggers["t.t_ct"]["definition"]
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t ENABLE ALWAYS TRIGGER t_off")
    assert after.document[SCHEMA]["trigger"]["t.t_off"]["enabled"] == "always"


def test_row_security_is_recorded_only_for_a_table_with_a_flag(conn: Any) -> None:
    _created(
        conn, TABLE, f"CREATE TABLE {SCHEMA}.plain ()", f"CREATE TABLE {SCHEMA}.pt (id integer) PARTITION BY LIST (id)"
    )
    assert _doc(conn)["row_security"] == {}
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t ENABLE ROW LEVEL SECURITY")
    assert after.document[SCHEMA]["row_security"] == {"t": {"enabled": True, "forced": False}}
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t FORCE ROW LEVEL SECURITY")
    assert after.document[SCHEMA]["row_security"] == {"t": {"enabled": True, "forced": True}}
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.pt ENABLE ROW LEVEL SECURITY")
    assert set(after.document[SCHEMA]["row_security"]) == {"t", "pt"}  # a partitioned table counts
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t DISABLE ROW LEVEL SECURITY")
    assert after.document[SCHEMA]["row_security"]["t"] == {"enabled": False, "forced": True}  # forced alone counts
    after = _changed(conn, f"ALTER TABLE {SCHEMA}.t NO FORCE ROW LEVEL SECURITY")
    assert set(after.document[SCHEMA]["row_security"]) == {"pt"}


def test_policies_have_command_permissiveness_roles_and_expressions(conn: Any) -> None:
    me = _query(conn, "SELECT current_user::text")[0][0]
    _created(
        conn,
        TABLE,
        f"CREATE POLICY p_all ON {SCHEMA}.t USING (id > 0)",
        f'CREATE POLICY p_ins ON {SCHEMA}.t AS RESTRICTIVE FOR INSERT TO pg_monitor, "{me}"'
        " WITH CHECK (note IS NOT NULL)",
        f"CREATE POLICY p_upd ON {SCHEMA}.t FOR UPDATE USING (id < 9) WITH CHECK (id < 10)",
        f"CREATE POLICY p_sel ON {SCHEMA}.t FOR SELECT TO CURRENT_USER USING (true)",
        f"CREATE POLICY p_del ON {SCHEMA}.t FOR DELETE USING (false)",
    )
    policies = _doc(conn)["policy"]
    assert set(policies) == {"t.p_all", "t.p_ins", "t.p_upd", "t.p_sel", "t.p_del"}
    assert policies["t.p_all"] == {
        "command": "all",
        "permissive": True,
        "roles": ["public"],
        "using": "(id > 0)",
        "with_check": None,
    }
    assert policies["t.p_ins"] == {
        "command": "insert",
        "permissive": False,
        "roles": sorted(["pg_monitor", me]),
        "using": None,
        "with_check": "(note IS NOT NULL)",
    }
    assert (policies["t.p_upd"]["using"], policies["t.p_upd"]["with_check"]) == ("(id < 9)", "(id < 10)")
    assert [policies[key]["command"] for key in ("t.p_sel", "t.p_upd", "t.p_del")] == ["select", "update", "delete"]
    assert policies["t.p_sel"]["roles"] == [me]
    after = _changed(conn, f"ALTER POLICY p_all ON {SCHEMA}.t USING (id > 1)")
    assert after.document[SCHEMA]["policy"]["t.p_all"]["using"] == "(id > 1)"
    assert _doc(conn)["row_security"] == {}  # a policy is not the table's flag


def test_an_enum_is_its_labels_in_sort_order(conn: Any) -> None:
    _created(conn, ENUM, f"CREATE DOMAIN {SCHEMA}.d AS integer", f"CREATE TYPE {SCHEMA}.pair AS (a integer)")
    assert _doc(conn)["enum"] == {"mood": ["sad", "happy"]}  # a domain and a composite type are not covered
    after = _changed(conn, f"ALTER TYPE {SCHEMA}.mood ADD VALUE 'ecstatic' BEFORE 'sad'")
    assert after.document[SCHEMA]["enum"]["mood"] == ["ecstatic", "sad", "happy"]
    after = _changed(conn, f"ALTER TYPE {SCHEMA}.mood ADD VALUE 'meh' AFTER 'sad'")
    assert after.document[SCHEMA]["enum"]["mood"] == ["ecstatic", "sad", "meh", "happy"]
    _changed(conn, f"ALTER TYPE {SCHEMA}.mood RENAME VALUE 'meh' TO 'ok'")


def test_what_an_extension_owns_is_excluded(conn: Any) -> None:
    _run(conn, f"CREATE SCHEMA {EXT_SCHEMA}", f"CREATE EXTENSION citext SCHEMA {EXT_SCHEMA}")
    assert fingerprint(conn, schemas=[EXT_SCHEMA]).document == {EXT_SCHEMA: {kind: {} for kind in KINDS}}
    _run(conn, f"CREATE TABLE {EXT_SCHEMA}.mine (name {EXT_SCHEMA}.citext UNIQUE)")
    document = fingerprint(conn, schemas=[EXT_SCHEMA]).document[EXT_SCHEMA]
    assert set(document["table"]) == {"mine"}
    assert document["column"]["mine.name"]["type"] == f"{EXT_SCHEMA}.citext"
    assert set(document["index"]) == {"mine_name_key"}
    assert set(document["constraint"]) == {"mine.mine_name_key"}
    assert document["function"] == {} and document["view"] == {}


def test_the_extension_goes_with_its_schema(conn: Any) -> None:
    _run(conn, f"CREATE SCHEMA {EXT_SCHEMA}", f"CREATE EXTENSION citext SCHEMA {EXT_SCHEMA}")
    _run(conn, *TEST_DROPS)
    assert _query(conn, "SELECT count(*) FROM pg_extension WHERE extname = 'citext'") == [(0,)]


# Session settings that change how the catalogs print a definition (a constant, an identifier, a string literal).
PRINTING_SETTINGS = {
    "DateStyle": "'SQL, DMY'",
    "TimeZone": "'Asia/Tokyo'",
    "IntervalStyle": "'iso_8601'",
    "extra_float_digits": "0",
    "bytea_output": "'escape'",
    "quote_all_identifiers": "on",
    "standard_conforming_strings": "off",
}
DEFINITIONS = (
    f"SELECT pg_get_viewdef('{SCHEMA}.v'::regclass), (SELECT string_agg(pg_get_expr(adbin, adrelid), ';'"
    f" ORDER BY adnum) FROM pg_attrdef WHERE adrelid = '{SCHEMA}.t'::regclass)"
)


def test_the_hash_does_not_depend_on_the_sessions_printing_settings(conn: Any) -> None:
    _created(
        conn,
        f"CREATE TABLE {SCHEMA}.t (plain integer, d date DEFAULT '2024-03-04',"
        " ts timestamptz DEFAULT '2024-03-04 05:06:07+00', i interval DEFAULT '1 day 2 hours',"
        " f float8 DEFAULT '0.30000000000000004', b bytea DEFAULT '\\xdeadbeef', s text DEFAULT 'a\\b')",
        f"CREATE VIEW {SCHEMA}.v AS SELECT DATE '2024-03-04' AS d, TIMESTAMPTZ '2024-03-04 05:06:07+00' AS ts,"
        " INTERVAL '1 day 2 hours' AS i, '0.30000000000000004'::float8 AS f, '\\xdeadbeef'::bytea AS b,"
        " 'a\\b'::text AS s",
    )
    first = _fp(conn)
    plain = _query(conn, DEFINITIONS)
    shown = {name: _query(conn, f"SHOW {name}") for name in PRINTING_SETTINGS}
    _run(conn, *(f"SET {name} = {value}" for name, value in PRINTING_SETTINGS.items()))
    try:
        changed = _query(conn, "SELECT " + ", ".join(f"current_setting('{name}')" for name in PRINTING_SETTINGS))
        assert changed[0] != tuple(row[0][0] for row in shown.values())  # the settings took effect
        assert _query(conn, DEFINITIONS) != plain  # and they do change how the catalogs print these objects
        second = _fp(conn)
        assert (
            _query(conn, "SELECT " + ", ".join(f"current_setting('{name}')" for name in PRINTING_SETTINGS)) == changed
        )
    finally:
        _run(conn, *(f"RESET {name}" for name in PRINTING_SETTINGS))
    assert (second.document, second.hash) == (first.document, first.hash)
    assert first.document[SCHEMA]["column"]["t.plain"]["type"] == "integer"  # printed unquoted
    assert {name: _query(conn, f"SHOW {name}") for name in PRINTING_SETTINGS} == shown


def test_the_document_does_not_depend_on_the_search_path(conn: Any) -> None:
    _created(
        conn,
        f"CREATE DOMAIN {SCHEMA}.d AS integer",
        f"CREATE TABLE {SCHEMA}.t (id serial, d {SCHEMA}.d DEFAULT 1)",
        f"CREATE VIEW {SCHEMA}.v AS SELECT id FROM {SCHEMA}.t",
        f"CREATE INDEX t_d ON {SCHEMA}.t (d)",
        f"CREATE FUNCTION {SCHEMA}.f() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
    )
    first = _fp(conn)
    _run(conn, f"SET search_path = {SCHEMA}, public")
    try:
        second = _fp(conn)
    finally:
        _run(conn, "RESET search_path")
    assert second == first
    assert first.document[SCHEMA]["column"]["t.d"]["type"] == f"{SCHEMA}.d"


# --- the default schemas and the refusals --------------------------------------------------


def test_the_default_schemas_exclude_the_system_ones_and_caverns(conn: Any) -> None:
    install(conn)
    _created(conn, TABLE)
    result = fingerprint(conn)
    assert SCHEMA in result.schemas and "public" in result.schemas
    assert "caverns" not in result.schemas and "information_schema" not in result.schemas
    assert not [name for name in result.schemas if name.startswith("pg_")]
    assert list(result.schemas) == sorted(result.schemas)
    assert result == fingerprint(conn, schemas=result.schemas)


@pytest.mark.parametrize(
    ("schemas", "message"),
    [
        ("public", "not the string"),
        ([], "empty"),
        ((), "empty"),
        ([None], "must hold names"),
        (["public", 3], "must hold names"),
    ],
)
def test_a_bad_collection_of_schemas_is_refused(conn: Any, schemas: Any, message: str) -> None:
    with pytest.raises(ChangesetError, match=message):
        fingerprint(conn, schemas=schemas)
    _assert_idle(conn)


def test_a_schema_that_does_not_exist_is_refused_by_name(conn: Any) -> None:
    with pytest.raises(ChangesetError, match="the schema 'infinite_missing' does not exist"):
        fingerprint(conn, schemas=["public", "infinite_missing"])
    _assert_idle(conn)


def test_a_connection_with_autocommit_on_or_uncommitted_writes_is_refused(conn: Any) -> None:
    conn.autocommit = True
    try:
        with pytest.raises(CavernsError, match="autocommit"):
            fingerprint(conn)
    finally:
        conn.autocommit = False
    with conn.cursor() as cursor:
        cursor.execute(f"CREATE SCHEMA {SCHEMA}")
    with pytest.raises(CavernsError, match="uncommitted work"):
        fingerprint(conn)
    conn.rollback()


def test_reading_writes_nothing_and_leaves_no_open_transaction(conn: Any) -> None:
    install(conn)
    _created(conn, TABLE)
    ledger = history(conn)
    catalog = "SELECT count(*) FROM pg_class WHERE relnamespace = to_regnamespace('infinite_test')"
    before = _query(conn, catalog)
    fingerprint(conn)
    _assert_idle(conn)
    assert history(conn) == ledger
    assert _query(conn, catalog) == before
    assert _query(conn, "SELECT pg_current_xact_id_if_assigned() IS NULL") == [(True,)]


# --- the watched schemas ---------------------------------------------------------------------


def _latest_schemas(conn: Any) -> list[str]:
    """The schemas of the latest recorded fingerprint."""
    schemas: list[str] = _query(conn, "SELECT schemas FROM caverns.fingerprint ORDER BY version DESC LIMIT 1")[0][0]
    return schemas


def test_a_baseline_with_schemas_sets_the_default_of_the_fingerprint(conn: Any) -> None:
    _created(conn, TABLE)
    install(conn)
    baseline(conn, Changeset("app", [Step("base", "SELECT 1")]), schemas=[SCHEMA])
    assert fingerprint(conn).schemas == (SCHEMA,)
    assert fingerprint(conn, schemas=["public"]).schemas == ("public",)


def test_without_a_baseline_a_schema_created_later_is_watched(conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    install(conn)
    apply(conn, APP)
    assert EXT_SCHEMA not in _latest_schemas(conn)
    _run(conn, f"CREATE SCHEMA {EXT_SCHEMA}", f"CREATE TABLE {EXT_SCHEMA}.u ()")
    result = drift(conn)
    assert EXT_SCHEMA in result.schemas  # read live: the new schema is watched at once
    assert [(d.change, d.kind, d.name) for d in result.differences] == [("added", "table", f"{EXT_SCHEMA}.u")]
    with caplog.at_level("WARNING"):
        apply(conn, Changeset("app", [*STEPS, Step("later", NOOP)]), on_drift="warn")
    assert f"added table {EXT_SCHEMA}.u" in caplog.text  # the warning compares the watched schemas too
    assert EXT_SCHEMA in _latest_schemas(conn)
    assert drift(conn).differences == ()


def test_without_a_baseline_a_dropped_schema_is_drift_and_is_warned_of(
    conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _applied(conn)
    _run(conn, f"DROP SCHEMA {SCHEMA} CASCADE")
    expected = [
        ("removed", "table", f"{SCHEMA}.t"),
        ("removed", "column", f"{SCHEMA}.t.id"),
        ("removed", "column", f"{SCHEMA}.t.note"),
    ]
    result = drift(conn)
    assert SCHEMA in result.schemas  # no longer watched, still covered by the latest fingerprint
    assert _changes(conn) == expected
    narrowed = drift(conn, schemas=[SCHEMA])
    assert narrowed.schemas == (SCHEMA,)
    assert [(d.change, d.kind, d.name) for d in narrowed.differences] == expected
    with caplog.at_level("WARNING"):
        apply(conn, Changeset("app", [*STEPS, Step("later", NOOP)]), on_drift="warn")
    assert f"removed table {SCHEMA}.t" in caplog.text  # the pre-apply warning compares the same schemas
    assert SCHEMA not in _latest_schemas(conn)  # the recorded fingerprint describes the database as it now is
    assert drift(conn).differences == ()


def test_a_baseline_with_schemas_makes_later_applies_and_drift_cover_only_those(conn: Any) -> None:
    _baselined(conn, TABLE, f"CREATE SCHEMA {EXT_SCHEMA}", f"CREATE TABLE {EXT_SCHEMA}.u ()")
    changeset = Changeset("app", [Step("base", "SELECT 1"), Step("more", f"CREATE TABLE {SCHEMA}.more ()")])
    apply(conn, changeset)
    assert _latest_schemas(conn) == [SCHEMA]
    _run(conn, f"CREATE TABLE {EXT_SCHEMA}.v ()", f"CREATE TABLE {SCHEMA}.later ()")
    result = drift(conn)
    assert result.schemas == (SCHEMA,)
    assert [d.name for d in result.differences] == [f"{SCHEMA}.later"]
    with pytest.raises(ChangesetError, match=f"the schema '{EXT_SCHEMA}' is not watched"):
        drift(conn, schemas=[EXT_SCHEMA])
    _assert_idle(conn)


def test_a_baseline_without_schemas_takes_the_default_not_the_inherited_set(conn: Any) -> None:
    _baselined(conn, TABLE)
    assert _latest_schemas(conn) == [SCHEMA]
    baseline(conn, Changeset("other", [Step("base", "SELECT 1")]))
    assert _latest_schemas(conn) == list(fingerprint(conn).schemas)
    assert SCHEMA in _latest_schemas(conn) and "public" in _latest_schemas(conn)
    assert "caverns" not in _latest_schemas(conn)


def test_a_later_baseline_of_another_changeset_changes_the_watched_set(conn: Any) -> None:
    _baselined(conn, TABLE)
    assert drift(conn).schemas == (SCHEMA,)
    baseline(conn, Changeset("other", [Step("base", "SELECT 1")]), schemas=["public"])  # database-wide
    assert fingerprint(conn).schemas == ("public",)
    assert drift(conn).schemas == ("public",)


# --- compare ---------------------------------------------------------------------------------


def _document(**schemas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {name: {kind: kinds.get(kind, {}) for kind in KINDS} for name, kinds in schemas.items()}


def test_compare_of_equal_documents_finds_nothing() -> None:
    document = _document(a={"table": {"t": {"kind": "r"}}, "index": {"i": "CREATE INDEX i"}})
    assert _fingerprint.compare(document, document) == []
    assert _fingerprint.compare({}, {}) == []


def test_compare_names_what_was_added_removed_and_changed_in_schema_kind_key_order() -> None:
    recorded = _document(
        b={"function": {"f()": "old"}},
        a={"column": {"t.x": {"type": "integer"}, "t.y": 1}, "index": {"gone": "CREATE INDEX gone"}},
    )
    live = _document(
        b={"function": {"f()": "new"}, "view": {"v": {"kind": "v"}}},
        a={"column": {"t.x": {"type": "bigint"}, "t.z": 1}, "table": {"t": {"kind": "r"}}},
    )
    assert _fingerprint.compare(recorded, live) == [
        Difference("table", "a.t", "added"),
        Difference("column", "a.t.x", "changed"),
        Difference("column", "a.t.y", "removed"),
        Difference("column", "a.t.z", "added"),
        Difference("index", "a.gone", "removed"),
        Difference("view", "b.v", "added"),
        Difference("function", "b.f()", "changed"),
    ]


def test_compare_reads_a_schema_in_one_document_only_as_all_added_or_all_removed() -> None:
    kept = _document(a={"table": {"t": {"kind": "r"}}})
    both = _document(a={"table": {"t": {"kind": "r"}}}, b={"table": {"u": {"kind": "r"}}})
    assert _fingerprint.compare(kept, both) == [Difference("table", "b.u", "added")]
    assert _fingerprint.compare(both, kept) == [Difference("table", "b.u", "removed")]


def _old(document: dict[str, Any]) -> dict[str, Any]:
    """*document* as an earlier release recorded it: without the kinds added since."""
    return {
        name: {kind: value for kind, value in kinds.items() if kind in OLD_KINDS} for name, kinds in document.items()
    }


def test_compare_skips_the_kinds_a_recorded_schema_lacks_and_reads_a_lacking_schema_in_full() -> None:
    live = _document(
        a={
            "table": {"t": {"kind": "r"}},
            "trigger": {"t.g": {"definition": "d", "enabled": "origin"}},
            "enum": {"e": ["x"]},
        },
        b={"row_security": {"t": {"enabled": True, "forced": False}}},
    )
    recorded = _old(_document(a={"table": {"t": {"kind": "r"}}}))
    assert _fingerprint.compare(recorded, live) == [Difference("row_security", "b.t", "added")]
    assert _fingerprint.compare(_old(live), live) == []
    assert _fingerprint.compare({"a": {"table": {}, "trigger": {}}}, live) == [
        Difference("table", "a.t", "added"),
        Difference("trigger", "a.t.g", "added"),  # the kinds the recorded entry holds are compared
        Difference("row_security", "b.t", "added"),  # a schema it lacks is compared on every kind
    ]


def test_a_fingerprint_recorded_before_the_new_kinds_reports_no_drift_for_them(conn: Any) -> None:
    _created(conn, TABLE, TRIGGER_FUNCTION, TRIGGER, f"ALTER TABLE {SCHEMA}.t ENABLE ROW LEVEL SECURITY", POLICY, ENUM)
    live = fingerprint(conn, schemas=[SCHEMA]).document
    assert all(live[SCHEMA][kind] for kind in NEW_KINDS)
    assert _fingerprint.compare(_old(live), live) == []
    assert _fingerprint.compare({}, live) == [
        *(Difference(kind, f"{SCHEMA}.{key}", "added") for kind in _fingerprint._KINDS for key in live[SCHEMA][kind])
    ]


# --- drift -----------------------------------------------------------------------------------

STEPS = [
    Step("schema", f"CREATE SCHEMA {SCHEMA}", check=f"SELECT to_regnamespace('{SCHEMA}') IS NOT NULL"),
    Step("table", TABLE, check=f"SELECT to_regclass('{SCHEMA}.t') IS NOT NULL"),
    Step("note", NOOP),  # no check: it can never disagree
]
APP = Changeset("app", STEPS)
FUNCTION = f"CREATE FUNCTION {SCHEMA}.f(a integer) RETURNS integer LANGUAGE sql AS 'SELECT 1'"
LEDGER_AND_FINGERPRINTS = (
    "SELECT version, hash FROM caverns.fingerprint UNION ALL SELECT version, checksum FROM caverns.ledger"
)


def _applied(conn: Any) -> None:
    """Install, then apply `APP`: the ledger records it and a fingerprint of every schema follows."""
    install(conn)
    apply(conn, APP)


def _baselined(conn: Any, *statements: str, schemas: tuple[str, ...] = (SCHEMA,)) -> None:
    """Install, build the test schema by hand and baseline it: a fingerprint of *schemas*, nothing applied."""
    install(conn)
    _created(conn, *statements)
    baseline(conn, Changeset("app", [Step("base", "SELECT 1")]), schemas=schemas)


def _changes(conn: Any, **kwargs: Any) -> list[tuple[str, str, str]]:
    return [(d.change, d.kind, d.name) for d in drift(conn, **kwargs).differences]


def test_drift_without_a_fingerprint_is_refused(conn: Any) -> None:
    install(conn)
    with pytest.raises(NoFingerprint, match="apply a changeset or take a baseline first"):
        drift(conn)
    _assert_idle(conn)


def test_drift_before_install_is_refused(conn: Any) -> None:
    with pytest.raises(LedgerNotInstalled, match="call install"):
        drift(conn)
    _assert_idle(conn)


def test_drift_of_an_unchanged_database_finds_nothing(conn: Any) -> None:
    _applied(conn)
    result = drift(conn, APP)
    assert isinstance(result, Drift)
    assert (result.differences, result.disagreements) == ((), ())
    assert result.fingerprint_version == history(conn, changeset="app", limit=1)[0].version
    assert SCHEMA in result.schemas and "public" in result.schemas
    assert drift(conn).differences == ()


def test_drift_finds_what_was_added(conn: Any) -> None:
    _baselined(conn, TABLE)
    _run(conn, f"ALTER TABLE {SCHEMA}.t ADD COLUMN extra text", f"CREATE INDEX t_id_idx ON {SCHEMA}.t (id)", FUNCTION)
    assert _changes(conn) == [
        ("added", "column", f"{SCHEMA}.t.extra"),
        ("added", "index", f"{SCHEMA}.t_id_idx"),
        ("added", "function", f"{SCHEMA}.f(a integer)"),
    ]
    assert len(drift(conn).differences) == 3


def test_drift_finds_what_was_removed(conn: Any) -> None:
    _baselined(conn, TABLE, f"CREATE INDEX t_id_idx ON {SCHEMA}.t (id)", FUNCTION)
    _run(conn, f"DROP INDEX {SCHEMA}.t_id_idx", f"DROP FUNCTION {SCHEMA}.f(integer)")
    assert _changes(conn) == [
        ("removed", "index", f"{SCHEMA}.t_id_idx"),
        ("removed", "function", f"{SCHEMA}.f(a integer)"),
    ]


def test_drift_finds_a_dropped_schema_as_everything_removed(conn: Any) -> None:
    _baselined(conn, TABLE)
    _run(conn, f"DROP SCHEMA {SCHEMA} CASCADE")
    assert _changes(conn) == [
        ("removed", "table", f"{SCHEMA}.t"),
        ("removed", "column", f"{SCHEMA}.t.id"),
        ("removed", "column", f"{SCHEMA}.t.note"),
    ]


def test_drift_finds_a_changed_column_and_a_changed_function_body(conn: Any) -> None:
    _baselined(conn, TABLE, FUNCTION)
    _run(
        conn,
        f"ALTER TABLE {SCHEMA}.t ALTER COLUMN note SET DEFAULT 'b'",
        f"CREATE OR REPLACE FUNCTION {SCHEMA}.f(a integer) RETURNS integer LANGUAGE sql AS 'SELECT 2'",
    )
    assert _changes(conn) == [
        ("changed", "column", f"{SCHEMA}.t.note"),
        ("changed", "function", f"{SCHEMA}.f(a integer)"),
    ]


def test_drift_finds_a_changed_index(conn: Any) -> None:
    _baselined(conn, TABLE, f"CREATE INDEX t_idx ON {SCHEMA}.t (id)")
    _run(conn, f"DROP INDEX {SCHEMA}.t_idx", f"CREATE INDEX t_idx ON {SCHEMA}.t (note)")
    assert _changes(conn) == [("changed", "index", f"{SCHEMA}.t_idx")]


def test_drift_finds_a_changed_trigger_a_new_policy_a_new_flag_and_a_new_enum_label(conn: Any) -> None:
    _baselined(conn, TABLE, TRIGGER_FUNCTION, TRIGGER, ENUM)
    assert _changes(conn) == []
    _run(
        conn,
        f"ALTER TABLE {SCHEMA}.t DISABLE TRIGGER t_ins",
        POLICY,
        f"ALTER TABLE {SCHEMA}.t ENABLE ROW LEVEL SECURITY",
        f"ALTER TYPE {SCHEMA}.mood ADD VALUE 'fine'",
    )
    assert _changes(conn) == [
        ("changed", "trigger", f"{SCHEMA}.t.t_ins"),
        ("added", "row_security", f"{SCHEMA}.t"),
        ("added", "policy", f"{SCHEMA}.t.p_all"),
        ("changed", "enum", f"{SCHEMA}.mood"),
    ]


def test_drift_compares_with_the_latest_fingerprint_only(conn: Any) -> None:
    _applied(conn)
    _run(conn, f"ALTER TABLE {SCHEMA}.t ADD COLUMN extra text")
    assert drift(conn).differences
    apply(conn, Changeset("app", [*STEPS, Step("later", f"CREATE TABLE {SCHEMA}.later ()")]), on_drift="warn")
    result = drift(conn)  # the new run's fingerprint holds the column
    assert not result.differences and result.fingerprint_version == history(conn, limit=1)[0].version


def test_drift_schemas_narrow_the_report(conn: Any) -> None:
    _baselined(conn, TABLE, f"CREATE SCHEMA {EXT_SCHEMA}", schemas=(SCHEMA, EXT_SCHEMA))
    _run(conn, f"ALTER TABLE {SCHEMA}.t ADD COLUMN extra text", f"CREATE TABLE {EXT_SCHEMA}.u ()")
    everything = drift(conn)
    assert everything.schemas == (EXT_SCHEMA, SCHEMA)
    assert [d.name for d in everything.differences] == [f"{EXT_SCHEMA}.u", f"{SCHEMA}.t.extra"]
    narrowed = drift(conn, schemas=[SCHEMA])
    assert narrowed.schemas == (SCHEMA,)
    assert [d.name for d in narrowed.differences] == [f"{SCHEMA}.t.extra"]
    assert drift(conn, schemas=[SCHEMA, SCHEMA, EXT_SCHEMA]).schemas == (EXT_SCHEMA, SCHEMA)


def test_drift_refuses_a_schema_that_is_not_watched(conn: Any) -> None:
    _baselined(conn, TABLE)
    with pytest.raises(ChangesetError, match=rf"the schema 'public' is not watched \(watched: {SCHEMA}\)"):
        drift(conn, schemas=["public"])
    _assert_idle(conn)


@pytest.mark.parametrize(
    ("schemas", "message"), [([], "empty"), ("public", "not the string"), ([3], "must hold names")]
)
def test_drift_refuses_bad_schemas(conn: Any, schemas: Any, message: str) -> None:
    _applied(conn)
    with pytest.raises(ChangesetError, match=message):
        drift(conn, schemas=schemas)
    _assert_idle(conn)


def test_drift_reports_a_step_recorded_as_applied_whose_check_is_now_false(conn: Any) -> None:
    _applied(conn)
    _run(conn, f"DROP TABLE {SCHEMA}.t")
    result = drift(conn, APP)
    assert [(s.step.label, s.status) for s in result.disagreements] == [("table", Status.APPLIED)]
    assert result.disagreements[0].entry is not None and result.disagreements[0].entry.changeset == "app"
    assert [(d.change, d.kind) for d in result.differences] == [
        ("removed", "table"),
        ("removed", "column"),
        ("removed", "column"),
    ]
    assert drift(conn).disagreements == ()  # without a changeset there is nothing to run


def test_drift_does_not_call_an_unknown_step_a_disagreement(conn: Any) -> None:
    _applied(conn)
    _run(conn, f"CREATE TABLE {SCHEMA}.extra ()")
    extra = Step("extra", f"CREATE TABLE {SCHEMA}.extra ()", check=f"SELECT to_regclass('{SCHEMA}.extra') IS NOT NULL")
    result = drift(conn, Changeset("app", [*STEPS, extra]))
    assert result.disagreements == ()
    assert [d.name for d in result.differences] == [f"{SCHEMA}.extra"]


def test_drift_runs_the_checks_with_the_sessions_search_path(conn: Any) -> None:
    pinned = Step(
        "pinned",
        NOOP,
        check=f"SELECT current_setting('search_path') <> 'pg_catalog' AND to_regclass('{SCHEMA}.t') IS NOT NULL",
    )
    changeset = Changeset("app", [*STEPS, pinned])
    install(conn)
    apply(conn, changeset)
    assert drift(conn, changeset).disagreements == ()  # reading the catalogs pins the path only after the checks


def test_drift_writes_nothing_and_leaves_no_open_transaction(conn: Any) -> None:
    _applied(conn)
    _run(conn, f"ALTER TABLE {SCHEMA}.t ADD COLUMN extra text")
    before = _query(conn, LEDGER_AND_FINGERPRINTS)
    assert drift(conn, APP).differences
    _assert_idle(conn)
    assert _query(conn, LEDGER_AND_FINGERPRINTS) == before
    assert _query(conn, "SELECT pg_current_xact_id_if_assigned() IS NULL") == [(True,)]
