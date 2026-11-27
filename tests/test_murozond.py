"""Murozond's classifier and the SQL lexer it reads with: every rule, every exemption, the lexer's edge cases."""

from __future__ import annotations

import dataclasses

import pytest

from caverns_of_time import _sql, murozond
from caverns_of_time.murozond import Classification, Finding, Risk, classify

D, L, U = Risk.DESTRUCTIVE, Risk.LOCK_HAZARD, Risk.UNKNOWN
Expected = list[tuple[Risk, int, str]]


def found(sql: str) -> Expected:
    """The findings of the SQL as (risk, statement, reason) triples."""
    return [(f.risk, f.statement, f.reason) for f in classify(sql).findings]


# --- the data model ----------------------------------------------------------------------


def test_the_public_names() -> None:
    assert sorted(murozond.__all__) == ["Classification", "Finding", "Risk", "classify"]
    assert {risk.value for risk in Risk} == {"safe", "destructive", "lock_hazard", "unknown"}


def test_classification_and_finding_are_frozen() -> None:
    result = classify("DROP TABLE t")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.risk = Risk.SAFE  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.findings[0].reason = "x"  # type: ignore[misc]
    assert isinstance(result, Classification) and isinstance(result.findings[0], Finding)


def test_safe_sql_has_no_findings() -> None:
    assert classify("INSERT INTO t VALUES (1)") == Classification(Risk.SAFE, ())


@pytest.mark.parametrize("sql", ["", "   \n", "-- only a comment", "/* c */ ;; ;", ";"])
def test_sql_with_no_statement_is_unknown(sql: str) -> None:
    assert classify(sql) == Classification(U, (Finding(U, 1, "no statement"),))


def test_the_risk_is_the_worst_finding() -> None:
    sql = "SELECT 1; CREATE INDEX i ON t (a); DROP TABLE t"
    result = classify(sql)
    assert result.risk is D
    assert [f.risk for f in result.findings] == [U, L, D]
    assert classify("SELECT 1; CREATE INDEX i ON t (a)").risk is L
    assert classify("SELECT 1; INSERT INTO t VALUES (1)").risk is U


# --- destructive -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DROP TABLE t", "DROP TABLE"),
        ("DROP TABLE IF EXISTS t CASCADE", "DROP TABLE"),
        ("DROP MATERIALIZED VIEW v", "DROP MATERIALIZED VIEW"),
        ("DROP TABLE data", "DROP TABLE"),  # a name that is an object-type word is not read as the type
        ("DROP TABLE user, event", "DROP TABLE"),
        ("DROP FOREIGN DATA WRAPPER w", "DROP FOREIGN DATA WRAPPER"),
        ("DROP TEXT SEARCH CONFIGURATION c", "DROP TEXT SEARCH CONFIGURATION"),
        ("DROP INDEX CONCURRENTLY IF EXISTS i", "DROP INDEX"),
        ("DROP OWNED BY someone", "DROP OWNED"),
        ("DROP FOREIGN TABLE f", "DROP FOREIGN TABLE"),
        ("DROP FOREIGN DATA WRAPPER w", "DROP FOREIGN DATA WRAPPER"),
        ("DROP TEXT SEARCH CONFIGURATION c", "DROP TEXT SEARCH CONFIGURATION"),
        ("DROP SCHEMA s CASCADE", "DROP SCHEMA"),
        ("DROP EXTENSION IF EXISTS e", "DROP EXTENSION"),
        ("DROP", "DROP"),
        ("TRUNCATE t", "TRUNCATE"),
        ("DELETE FROM t WHERE a = 1", "DELETE"),
        ("MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE", "MERGE with DELETE"),
        ("ALTER TABLE t DROP COLUMN a", "ALTER TABLE DROP COLUMN"),
        ("ALTER TABLE t DROP a", "ALTER TABLE DROP COLUMN"),
        ("ALTER TABLE IF EXISTS ONLY t DROP COLUMN IF EXISTS a CASCADE", "ALTER TABLE DROP COLUMN"),
        ("ALTER TABLE t DROP CONSTRAINT c", "ALTER TABLE DROP CONSTRAINT"),
        ("ALTER FOREIGN TABLE f DROP COLUMN a", "ALTER TABLE DROP COLUMN"),
        ("ALTER TYPE t DROP ATTRIBUTE a", "ALTER TYPE DROP"),
        ("ALTER DOMAIN d DROP CONSTRAINT c", "ALTER DOMAIN DROP"),
        ("ALTER PUBLICATION p DROP TABLE t", "ALTER PUBLICATION DROP"),
        ("ALTER EXTENSION e DROP TABLE t", "ALTER EXTENSION DROP"),
    ],
)
def test_destructive_statements(sql: str, reason: str) -> None:
    assert found(sql) == [(D, 1, reason)]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE t SET a = 1",
        "ALTER TABLE t RENAME TO u",
        "ALTER TABLE t RENAME COLUMN a TO b",
        "ALTER TABLE t ALTER COLUMN a DROP DEFAULT",
        "ALTER TABLE t ALTER a DROP NOT NULL",
        "ALTER TABLE t ALTER COLUMN a DROP EXPRESSION",
        "ALTER TABLE t ALTER COLUMN a DROP IDENTITY IF EXISTS",
        "ALTER DOMAIN d DROP NOT NULL",
        "ALTER VIEW v ALTER COLUMN a DROP DEFAULT",
        "ALTER SERVER s OPTIONS (DROP host)",
        "MERGE INTO t USING s ON t.a = s.a WHEN NOT MATCHED THEN INSERT VALUES (1)",
    ],
)
def test_statements_that_are_not_destructive(sql: str) -> None:
    assert D not in {risk for risk, _, _ in found(sql)}


def test_merge_without_delete_is_unknown() -> None:
    assert found("MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET a = 1") == [(U, 1, "MERGE")]


# --- lock hazards ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("CREATE INDEX i ON t (a)", "CREATE INDEX without CONCURRENTLY"),
        ("CREATE UNIQUE INDEX IF NOT EXISTS i ON ONLY t (a)", "CREATE INDEX without CONCURRENTLY"),
        ("CREATE INDEX ON t (a)", "CREATE INDEX without CONCURRENTLY"),
        ("ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0)", "ADD CONSTRAINT without NOT VALID"),
        ("ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (id)", "ADD CONSTRAINT without NOT VALID"),
        ("ALTER TABLE t ADD PRIMARY KEY (a)", "ADD CONSTRAINT without NOT VALID"),
        ("ALTER TABLE t ADD UNIQUE (a)", "ADD CONSTRAINT without NOT VALID"),
        ("ALTER TABLE t ADD CONSTRAINT c EXCLUDE USING gist (a WITH =)", "ADD CONSTRAINT without NOT VALID"),
        ("ALTER TABLE t ADD CONSTRAINT c UNIQUE (a) USING INDEX TABLESPACE ts", "ADD CONSTRAINT without NOT VALID"),
        ("ALTER TABLE t ADD CONSTRAINT c CHECK (NOT valid)", "ADD CONSTRAINT without NOT VALID"),
        ("ALTER TABLE t ALTER COLUMN a TYPE bigint", "ALTER COLUMN TYPE"),
        ("ALTER TABLE t ALTER a SET DATA TYPE text USING a::text", "ALTER COLUMN TYPE"),
        ('ALTER TABLE t ALTER COLUMN "type" TYPE text', "ALTER COLUMN TYPE"),
        ("ALTER TABLE t ALTER COLUMN a SET NOT NULL", "SET NOT NULL"),
        ("ALTER TABLE t ADD COLUMN a int PRIMARY KEY", "ADD COLUMN with PRIMARY KEY"),
        ("ALTER TABLE t ADD COLUMN a int UNIQUE", "ADD COLUMN with UNIQUE"),
        ("ALTER TABLE t ADD a int GENERATED ALWAYS AS (b + 1) STORED", "ADD COLUMN GENERATED STORED"),
        ("ALTER TABLE t SET TABLESPACE ts", "SET TABLESPACE"),
        ("ALTER TABLE ALL IN TABLESPACE a SET TABLESPACE b", "SET TABLESPACE"),
        ("ALTER INDEX i SET TABLESPACE ts", "SET TABLESPACE"),
        ("ALTER TABLE t SET LOGGED", "SET LOGGED"),
        ("ALTER TABLE t SET UNLOGGED", "SET UNLOGGED"),
        ("ALTER TABLE t ATTACH PARTITION p FOR VALUES IN (1, 2)", "ATTACH PARTITION"),
        ("ALTER TABLE t DETACH PARTITION p", "DETACH PARTITION without CONCURRENTLY"),
        ("REINDEX TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (VERBOSE) INDEX i", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY false) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (VERBOSE, CONCURRENTLY off) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY 0) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY -0) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY +0) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY E'off') TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY $$off$$) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY, CONCURRENTLY off) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (CONCURRENTLY true, CONCURRENTLY false) TABLE t", "REINDEX without CONCURRENTLY"),
        ("REINDEX (TABLESPACE concurrently) TABLE t", "REINDEX without CONCURRENTLY"),
        ("CREATE INDEX ON s.concurrently (a)", "CREATE INDEX without CONCURRENTLY"),
        ("REINDEX TABLE s.concurrently", "REINDEX without CONCURRENTLY"),
        ("REFRESH MATERIALIZED VIEW s.concurrently", "REFRESH MATERIALIZED VIEW without CONCURRENTLY"),
        ("ALTER TABLE t ADD NOT NULL a", "ADD CONSTRAINT without NOT VALID"),
        ("REFRESH MATERIALIZED VIEW v", "REFRESH MATERIALIZED VIEW without CONCURRENTLY"),
        ("VACUUM FULL t", "VACUUM FULL"),
        ("VACUUM (FULL, ANALYZE) t", "VACUUM FULL"),
        ("CLUSTER t USING i", "CLUSTER"),
        ("LOCK TABLE t IN ACCESS EXCLUSIVE MODE", "LOCK"),
    ],
)
def test_lock_hazards(sql: str, reason: str) -> None:
    assert found(sql) == [(L, 1, reason)]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX CONCURRENTLY i ON t (a)",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS i ON t (a)",
        "ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0) NOT VALID",
        "ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES u (id) NOT VALID",
        "ALTER TABLE t ADD CONSTRAINT c PRIMARY KEY USING INDEX i",
        "ALTER TABLE t ADD CONSTRAINT c UNIQUE USING INDEX i",
        "ALTER TABLE t ADD UNIQUE USING INDEX i",
        "ALTER TABLE t VALIDATE CONSTRAINT c",
        "ALTER TABLE t ALTER COLUMN a SET DEFAULT 1",
        "ALTER TABLE t ALTER COLUMN a SET STATISTICS 100",
        "ALTER TABLE t ALTER CONSTRAINT type DEFERRABLE",
        "ALTER TABLE t ADD COLUMN a int",
        "ALTER TABLE t ADD COLUMN a int DEFAULT 0 NOT NULL REFERENCES u (id)",
        "ALTER TABLE t OWNER TO someone",
        "ALTER TABLE t ENABLE TRIGGER ALL",
        "ALTER TABLE t ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE t SET (fillfactor = 70, autovacuum_enabled = false)",
        "ALTER TABLE t SET SCHEMA s",
        "ALTER TABLE t SET ACCESS METHOD heap",
        "ALTER TABLE t CLUSTER ON i",
        "ALTER TABLE t DETACH PARTITION p CONCURRENTLY",
        "ALTER TABLE t DETACH PARTITION p FINALIZE",
        "REINDEX TABLE CONCURRENTLY t",
        "REINDEX (CONCURRENTLY) TABLE t",
        "REINDEX (CONCURRENTLY true) TABLE t",
        "REINDEX (CONCURRENTLY on) TABLE t",
        "REINDEX (CONCURRENTLY 'on') TABLE t",
        "REINDEX (CONCURRENTLY 1) TABLE t",
        "REINDEX (VERBOSE, CONCURRENTLY) TABLE t",
        "REINDEX (CONCURRENTLY false, CONCURRENTLY) TABLE t",
        "REINDEX (CONCURRENTLY off) TABLE CONCURRENTLY t",
        "REINDEX TABLE CONCURRENTLY off",
        "ALTER TABLE t ADD NOT NULL a NOT VALID",
        "ALTER TABLE t ADD COLUMN c int NOT NULL DEFAULT 0",
        "REFRESH MATERIALIZED VIEW CONCURRENTLY v",
    ],
)
def test_the_safe_counterparts(sql: str) -> None:
    assert found(sql) == []


def test_a_table_created_earlier_in_the_sql_is_new() -> None:
    sql = "CREATE TABLE s.t (a int); CREATE INDEX i ON s.t (a); ALTER TABLE s.t ADD b int PRIMARY KEY, DROP COLUMN a"
    assert found(sql) == []
    assert found("CREATE TABLE t (a int); CREATE INDEX i ON ONLY t (a); ALTER TABLE ONLY t ADD CHECK (a > 0)") == []
    assert found('CREATE TEMP TABLE "T" (a int); ALTER TABLE "T" ALTER a SET NOT NULL') == []
    assert found("CREATE FOREIGN TABLE f (a int) SERVER s; ALTER FOREIGN TABLE f DROP COLUMN a") == []


def test_create_table_if_not_exists_does_not_make_a_table_new() -> None:
    # the table may already exist with data
    assert found('CREATE TEMP TABLE IF NOT EXISTS "T" (a int); ALTER TABLE "T" ALTER a SET NOT NULL') == [
        (L, 2, "SET NOT NULL")
    ]
    drop = "CREATE TABLE IF NOT EXISTS public.orders (id int); ALTER TABLE public.orders DROP COLUMN legacy"
    assert found(drop) == [(D, 2, "ALTER TABLE DROP COLUMN")]
    index = "CREATE TABLE IF NOT EXISTS public.orders (id int); CREATE INDEX ON public.orders (id)"
    assert found(index) == [(L, 2, "CREATE INDEX without CONCURRENTLY")]


def test_a_rename_or_set_schema_ends_the_created_table_exemption() -> None:
    rename = "CREATE TABLE t2 (a int); ALTER TABLE t2 RENAME TO t_old; ALTER TABLE big RENAME TO t2; "
    assert found(rename + "ALTER TABLE t2 DROP COLUMN a") == [(D, 4, "ALTER TABLE DROP COLUMN")]
    move = "CREATE TABLE t2 (a int); ALTER TABLE big SET SCHEMA s; ALTER TABLE t2 DROP COLUMN a"
    assert found(move) == [(D, 3, "ALTER TABLE DROP COLUMN")]
    other = "CREATE TABLE t2 (a int); ALTER SCHEMA s RENAME TO public; CREATE INDEX ON t2 (a)"
    assert found(other) == [(L, 3, "CREATE INDEX without CONCURRENTLY")]
    # renaming a column is not moving a table
    column = "CREATE TABLE t2 (a int); ALTER TABLE t2 RENAME COLUMN a TO b; ALTER TABLE t2 DROP COLUMN b"
    assert found(column) == []


def test_a_table_created_later_does_not_exempt_an_earlier_statement() -> None:
    assert found("CREATE INDEX i ON t (a); CREATE TABLE t (a int)") == [(L, 1, "CREATE INDEX without CONCURRENTLY")]


def test_table_names_are_compared_as_written() -> None:
    assert found("CREATE TABLE public.t (a int); CREATE INDEX i ON t (a)") == [
        (L, 2, "CREATE INDEX without CONCURRENTLY")
    ]
    assert found("CREATE TABLE t (a int); ALTER TABLE public.t DROP COLUMN a") == [(D, 2, "ALTER TABLE DROP COLUMN")]
    assert found("CREATE TABLE other (a int); CREATE INDEX i ON t (a)") == [(L, 2, "CREATE INDEX without CONCURRENTLY")]
    # unquoted names fold to lower case, a quoted one is kept verbatim
    assert found("CREATE TABLE Public.T (a int); CREATE INDEX i ON public.t (a)") == []
    assert found('CREATE TABLE "T" (a int); CREATE INDEX i ON t (a)') == [(L, 2, "CREATE INDEX without CONCURRENTLY")]


def test_a_multi_action_alter_gives_one_finding_per_risky_action() -> None:
    sql = (
        "ALTER TABLE t ADD COLUMN a int, DROP COLUMN b, ALTER COLUMN c TYPE text, "
        "ADD CONSTRAINT k CHECK (c <> ''), SET (fillfactor = 70, autovacuum_enabled = false), "
        "ALTER COLUMN d SET NOT NULL, DROP CONSTRAINT old"
    )
    assert found(sql) == [
        (D, 1, "ALTER TABLE DROP COLUMN"),
        (L, 1, "ALTER COLUMN TYPE"),
        (L, 1, "ADD CONSTRAINT without NOT VALID"),
        (L, 1, "SET NOT NULL"),
        (D, 1, "ALTER TABLE DROP CONSTRAINT"),
    ]


def test_commas_inside_parentheses_do_not_split_actions() -> None:
    assert found("ALTER TABLE t ADD CONSTRAINT c FOREIGN KEY (a, b) REFERENCES u (a, b) NOT VALID") == []
    assert found("ALTER TABLE t ADD COLUMN a numeric(10, 2), ADD COLUMN b int") == []


def test_an_other_object_with_several_actions() -> None:
    assert found("ALTER TYPE t DROP ATTRIBUTE a, ADD ATTRIBUTE b int, DROP ATTRIBUTE c") == [
        (D, 1, "ALTER TYPE DROP"),
        (D, 1, "ALTER TYPE DROP"),
    ]


# --- unknown and safe leading keywords ---------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DO $$ BEGIN DROP TABLE t; END $$", "DO block"),
        ("CALL p()", "CALL"),
        ("EXECUTE stmt (1)", "EXECUTE"),
        ("SELECT 1", "SELECT"),
        ("WITH x AS (SELECT 1) DELETE FROM t", "WITH"),
        ("BEGIN", "transaction control"),
        ("START TRANSACTION", "transaction control"),
        ("COMMIT", "transaction control"),
        ("END", "transaction control"),
        ("ROLLBACK", "transaction control"),
        ("ABORT", "transaction control"),
        ("SAVEPOINT s", "transaction control"),
        ("RELEASE s", "transaction control"),
        ("PREPARE TRANSACTION 'x'", "transaction control"),
        ("VACUUM t", "VACUUM"),
        ("VACUUM (ANALYZE) t", "VACUUM"),
        ("DISCARD ALL", "unrecognised statement"),
        ("(SELECT 1)", "unrecognised statement"),
        ("1 + 1", "unrecognised statement"),
        ('"drop" t', "unrecognised statement"),
        ("REFRESH PUBLICATION", "unrecognised statement"),
    ],
)
def test_unknown_statements(sql: str, reason: str) -> None:
    assert found(sql) == [(U, 1, reason)]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("COPY (DELETE FROM t RETURNING a) TO STDOUT", [(D, 1, "COPY with DELETE")]),
        (
            "copy (merge into t using s on true when matched then delete returning 1) to stdout",
            [(D, 1, "COPY with DELETE"), (D, 1, "COPY with MERGE")],
        ),
        (
            "COPY (MERGE INTO t USING s ON true WHEN MATCHED THEN UPDATE SET a = 1 RETURNING 1) TO STDOUT",
            [(D, 1, "COPY with MERGE")],
        ),
        ("COPY t FROM PROGRAM 'cmd'", [(U, 1, "COPY PROGRAM")]),
        ("COPY t TO PROGRAM 'cmd'", [(U, 1, "COPY PROGRAM")]),
        ("COPY (DELETE FROM t RETURNING a) TO PROGRAM 'cmd'", [(D, 1, "COPY with DELETE"), (U, 1, "COPY PROGRAM")]),
        ("COPY t FROM STDIN", []),
        ("COPY t TO STDOUT", []),
        ("COPY (SELECT 1) TO STDOUT", []),
        ("COPY (SELECT a FROM t) TO STDOUT WITH (FORMAT csv)", []),
        ("COPY t (a, b) FROM '/file'", []),
        ("COPY t FROM '/file' WITH (FORMAT csv, DEFAULT 'delete')", []),
        ('COPY (SELECT 1 AS "delete") TO STDOUT', []),
        ("COPY (SELECT t.delete FROM t) TO STDOUT", []),
        ("COPY (SELECT 1) TO STDOUT WITH (FORCE_QUOTE (program))", []),
    ],
)
def test_copy(sql: str, expected: list[tuple[Risk, int, str]]) -> None:
    assert found(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "GRANT SELECT ON t TO r",
        "REVOKE ALL ON t FROM r",
        "COMMENT ON TABLE t IS 'x'",
        "SET search_path TO s",
        "RESET ALL",
        "ANALYZE t",
        "ANALYSE t",
        "CREATE SCHEMA s",
        "CREATE VIEW v AS SELECT 1",
        "CREATE MATERIALIZED VIEW v AS SELECT 1",
        "CREATE SEQUENCE s",
        "CREATE TYPE t AS ENUM ('a')",
        "CREATE OR REPLACE FUNCTION f() RETURNS int LANGUAGE sql AS $$ DROP TABLE t; SELECT 1 $$",
        "CREATE PROCEDURE p() LANGUAGE sql AS 'DELETE FROM t'",
        "CREATE EXTENSION e",
        "CREATE TRIGGER g BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f()",
        "CREATE UNLOGGED TABLE t (a int)",
        "ALTER VIEW v RENAME TO w",
        "ALTER SEQUENCE s RESTART",
        "ALTER ROLE r SET search_path TO s",
        "ALTER FUNCTION f(int, text) OWNER TO r",
    ],
)
def test_safe_statements(sql: str) -> None:
    assert found(sql) == []


@pytest.mark.parametrize(
    "sql",
    ["drop table t", "Drop Table T", "alter TABLE t alter column a type int", "create INDEX i on t (a)"],
)
def test_keywords_are_case_insensitive(sql: str) -> None:
    assert classify(sql).risk is not Risk.SAFE
    assert classify(sql).findings == classify(sql.upper()).findings


# --- statements and numbering ------------------------------------------------------------


def test_statements_are_numbered_from_one_and_empty_ones_do_not_count() -> None:
    sql = ";; SELECT 1;; ; -- c\n ;DROP TABLE t;;"
    assert found(sql) == [(U, 1, "SELECT"), (D, 2, "DROP TABLE")]


def test_findings_are_in_statement_then_action_order() -> None:
    sql = "DROP TABLE a; ALTER TABLE t ADD COLUMN x int UNIQUE, DROP COLUMN y; TRUNCATE b"
    assert [(f.statement, f.reason) for f in classify(sql).findings] == [
        (1, "DROP TABLE"),
        (2, "ADD COLUMN with UNIQUE"),
        (2, "ALTER TABLE DROP COLUMN"),
        (3, "TRUNCATE"),
    ]


# --- the lexer: text that is not code ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES ('a; DROP TABLE t; b')",
        "INSERT INTO t VALUES ('it''s; DROP TABLE t')",
        "INSERT INTO t VALUES (E'it\\'s; DROP TABLE t')",
        "INSERT INTO t VALUES (e'\\\\'); INSERT INTO t VALUES (1)",
        "INSERT INTO t VALUES (U&'a; DROP TABLE t')",
        "INSERT INTO t VALUES (B'1'), (X'ff'), (N'a; DROP TABLE t')",
        "INSERT INTO t VALUES ($$a; DROP TABLE t$$)",
        "INSERT INTO t VALUES ($tag$a; $$ DROP TABLE t; $tag$)",
        "INSERT INTO t VALUES ($_t1$ ; DROP TABLE t $_t1$)",
        "INSERT INTO t VALUES (1) -- DROP TABLE t;\n",
        "INSERT INTO t VALUES (1) /* DROP TABLE t; */",
        "INSERT INTO t VALUES (1) /* a /* nested; DROP TABLE t */ still; a comment DROP TABLE t */",
        'INSERT INTO "drop" VALUES (1)',
        'INSERT INTO "a""; DROP TABLE t" VALUES (1)',
        "SET x = 'DROP'; /* */ SET y = 1",
    ],
)
def test_text_inside_quotes_and_comments_is_not_read(sql: str) -> None:
    assert found(sql) == []


def test_a_standard_string_ends_at_a_backslash_quote() -> None:
    # without E, the backslash is an ordinary character, so the string ends before DROP
    assert found("INSERT INTO t VALUES ('a\\'); DROP TABLE t; INSERT INTO t VALUES ('b')") == [(D, 2, "DROP TABLE")]


def test_an_e_string_does_not_end_at_a_backslash_quote() -> None:
    assert found("INSERT INTO t VALUES (E'a\\'); DROP TABLE t; ')") == []


def test_a_quoted_identifier_is_never_a_keyword() -> None:
    assert found('"drop" table t') == [(U, 1, "unrecognised statement")]
    assert found('ALTER TABLE t "drop" COLUMN a') == []


def test_a_dollar_parameter_is_not_a_dollar_quote() -> None:
    assert found("SELECT $1; DROP TABLE t; SELECT $2") == [(U, 1, "SELECT"), (D, 2, "DROP TABLE"), (U, 3, "SELECT")]
    assert found("PREPARE p AS SELECT $1$2; DROP TABLE t") == [(U, 1, "transaction control"), (D, 2, "DROP TABLE")]


def test_a_word_containing_a_dollar_is_one_word() -> None:
    assert found("CREATE TABLE a$b (a int); CREATE INDEX i ON a$b (a)") == []


def test_a_non_ascii_character_is_part_of_a_dollar_tag() -> None:
    assert found("CREATE FUNCTION f() RETURNS void LANGUAGE sql AS $😀$'$😀$; DROP TABLE x; -- '") == [
        (D, 2, "DROP TABLE")
    ]


def test_a_dollar_after_a_non_ascii_identifier_character_is_part_of_the_identifier() -> None:
    # PostgreSQL reads `x😀$a$` as one identifier, so no dollar quote opens
    assert found("INSERT INTO t SELECT 1 AS x😀$a$; DROP TABLE y; SELECT $a$") == [
        (D, 2, "DROP TABLE"),
        (U, 3, "unterminated quote or comment"),  # the last `$a$` opens a dollar quote of its own
    ]


def test_a_string_continued_after_a_newline_is_one_string() -> None:
    assert found("INSERT INTO t VALUES ('a'\n'b')") == []
    assert len(_sql.split("INSERT INTO t VALUES ('a' -- c\n  'b'); INSERT INTO t VALUES (1)")) == 2
    (statement,) = _sql.split("SELECT 'a'\n'b'")
    assert [t.text for t in statement.tokens if t.kind is _sql.Kind.STRING] == ["'a'\n'b'"]


def test_an_e_string_continuation_keeps_its_backslash_escapes() -> None:
    assert found("INSERT INTO t VALUES (E'a'\n'\\''); DROP TABLE y; --')") == [(D, 2, "DROP TABLE")]


def test_a_non_breaking_space_is_part_of_a_name_not_whitespace() -> None:
    # PostgreSQL reads `\u00a0$a$` as one identifier, so no dollar quote opens
    sql = "INSERT INTO t SELECT 1 AS \u00a0$a$; DROP TABLE y; INSERT INTO t SELECT 1 AS \u00a0$a$"
    assert found(sql) == [(D, 2, "DROP TABLE")]


def test_semicolons_inside_parentheses_do_not_split() -> None:
    assert found("INSERT INTO t VALUES (1; DROP TABLE t)") == []


# --- the lexer: BEGIN ATOMIC -------------------------------------------------------------


def test_a_begin_atomic_body_is_one_statement() -> None:
    sql = (
        "CREATE FUNCTION f(a int) RETURNS int LANGUAGE sql BEGIN ATOMIC "
        "SELECT CASE WHEN a > 0 THEN 1 ELSE 2 END; DROP TABLE t; INSERT INTO u VALUES (a); END"
    )
    assert found(sql) == []
    assert len(_sql.split(sql)) == 1


def test_statements_after_a_begin_atomic_body_are_read() -> None:
    sql = "CREATE PROCEDURE p() BEGIN ATOMIC SELECT 1; END; DROP TABLE t"
    assert found(sql) == [(D, 2, "DROP TABLE")]


def test_begin_atomic_outside_a_routine_definition_does_not_open_a_body() -> None:
    assert found("INSERT INTO t SELECT begin atomic FROM s; DROP TABLE x") == [(D, 2, "DROP TABLE")]
    assert len(_sql.split("INSERT INTO t SELECT begin atomic FROM s; DROP TABLE x")) == 2
    assert found("CREATE FUNCTION f() RETURNS int AS (begin atomic); DROP TABLE x") == [(D, 2, "DROP TABLE")]


def test_begin_atomic_is_read_only_in_a_create_function_or_procedure_statement() -> None:
    sql = "CREATE VIEW function AS WITH begin AS (SELECT 1 AS a) SELECT a FROM begin atomic; DROP TABLE y; SELECT 1"
    assert found(sql) == [(D, 2, "DROP TABLE"), (U, 3, "SELECT")]
    assert len(_sql.split(sql)) == 3
    assert len(_sql.split("CREATE OR REPLACE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END")) == 1


def test_a_case_or_end_after_a_dot_is_a_name_inside_a_begin_atomic_body() -> None:
    sql = (
        'CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT r.case FROM (SELECT 1 AS "case") r; END; '
        "DROP TABLE y; CREATE FUNCTION g() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END"
    )
    assert found(sql) == [(D, 2, "DROP TABLE")]
    assert len(_sql.split(sql)) == 3


def test_a_plain_begin_does_not_open_a_body() -> None:
    assert found("BEGIN; DROP TABLE t; COMMIT") == [
        (U, 1, "transaction control"),
        (D, 2, "DROP TABLE"),
        (U, 3, "transaction control"),
    ]


# --- the lexer: statements it cannot read ------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES ('unterminated",
        "INSERT INTO t VALUES (E'unterminated\\')",
        'INSERT INTO "unterminated VALUES (1)',
        "SELECT $$ unterminated",
        "SELECT $tag$ unterminated $$",
        "SELECT 1 /* unterminated",
        "SELECT 1 /* a /* nested */ unterminated",
        "INSERT INTO t VALUES (1); INSERT INTO t VALUES ('unterminated; DROP TABLE t",
    ],
)
def test_an_unterminated_quote_or_comment_is_unknown(sql: str) -> None:
    result = classify(sql)
    assert result.findings[-1].reason == "unterminated quote or comment"
    assert result.risk is U


def test_an_unterminated_quote_swallows_the_rest_into_its_statement() -> None:
    assert found("INSERT INTO t VALUES ('x); DROP TABLE t; DROP TABLE u") == [(U, 1, "unterminated quote or comment")]


@pytest.mark.parametrize("sql", ["\\copy t from 'f.csv'", "\\d t", "INSERT INTO t VALUES (1) \\gexec"])
def test_a_psql_meta_command_is_unknown(sql: str) -> None:
    assert found(sql) == [(U, 1, "psql meta-command")]


def test_a_meta_command_breaks_only_its_statement() -> None:
    assert found("\\set x 1; DROP TABLE t") == [(U, 1, "psql meta-command"), (D, 2, "DROP TABLE")]


# --- secrets -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER ROLE x PASSWORD 'hunter2'",
        "DELETE FROM t WHERE p = 'hunter2'",
        "DROP TABLE hunter2",
        "SELECT 'hunter2'",
        "DO $$ BEGIN PERFORM 'hunter2'; END $$",
        "HUNTER2 TABLE t",
        "ALTER TABLE hunter2 ALTER COLUMN a TYPE int",
        "INSERT INTO t VALUES ('hunter2') /* hunter2",
        "ALTER TYPE hunter2 DROP ATTRIBUTE hunter2",
    ],
)
def test_a_reason_never_quotes_the_sql(sql: str) -> None:
    for finding in classify(sql).findings:
        assert "hunter2" not in finding.reason.lower()


# --- the lexer itself --------------------------------------------------------------------


def test_split_gives_tokens_with_kinds_and_depths() -> None:
    (statement,) = _sql.split('ALTER TABLE "a""b" ADD CHECK (x IN (\'y\', 1.5));')
    assert statement.broken is None
    assert [(t.kind, t.text, t.depth) for t in statement.tokens][:5] == [
        (_sql.Kind.WORD, "ALTER", 0),
        (_sql.Kind.WORD, "TABLE", 0),
        (_sql.Kind.QUOTED, 'a"b', 0),
        (_sql.Kind.WORD, "ADD", 0),
        (_sql.Kind.WORD, "CHECK", 0),
    ]
    assert [(t.text, t.depth) for t in statement.tokens if t.kind is _sql.Kind.PUNCT] == [
        ("(", 0),
        ("(", 1),
        (",", 2),
        (")", 1),
        (")", 0),
    ]
    assert [t.depth for t in statement.tokens if t.kind is _sql.Kind.STRING] == [2]


def test_split_drops_empty_statements_and_comments() -> None:
    assert _sql.split(" ;; /* c */ ; -- c") == []
    assert [len(s.tokens) for s in _sql.split("a b; ;c;")] == [2, 1]


def test_tokens_expose_keywords_and_identifiers() -> None:
    word, quoted, string = _sql.split("Foo \"Bar\" 'baz'")[0].tokens
    assert (word.keyword, word.identifier) == ("FOO", "foo")
    assert (quoted.keyword, quoted.identifier) == ("", "Bar")
    assert (string.keyword, string.identifier) == ("", None)


# --- the lexer and exemption rules the final review tightened ----------------------------


def test_a_line_comment_ends_at_a_carriage_return() -> None:
    assert found("INSERT INTO t VALUES (1); -- note\rDROP TABLE victim;") == [(D, 2, "DROP TABLE")]


def test_an_attach_partition_on_a_created_table_is_still_a_lock_hazard_and_ends_its_exemption() -> None:
    sql = (
        "CREATE TABLE p (a int) PARTITION BY RANGE (a); "
        "ALTER TABLE p ATTACH PARTITION big FOR VALUES FROM (0) TO (10); CREATE INDEX ON p (a)"
    )
    assert found(sql) == [(L, 2, "ATTACH PARTITION"), (L, 3, "CREATE INDEX without CONCURRENTLY")]


def test_a_name_followed_by_uescape_is_never_a_created_table() -> None:
    index = "CREATE INDEX ON U&\"!0074\" UESCAPE '!' (a)"
    assert found(f'CREATE TABLE U&"!0074" (a int); {index}') == [(L, 2, "CREATE INDEX without CONCURRENTLY")]
    assert found(f"CREATE TABLE U&\"!0074\" UESCAPE '!' (a int); {index}") == [
        (L, 2, "CREATE INDEX without CONCURRENTLY")
    ]


def test_inherit_ends_the_exemption_of_the_created_child() -> None:
    sql = "CREATE TABLE nt (a int); ALTER TABLE old INHERIT nt; ALTER TABLE nt ADD CONSTRAINT c CHECK (a > 0)"
    assert found(sql) == [(L, 3, "ADD CONSTRAINT without NOT VALID")]


def test_a_partition_created_of_an_old_table_changes_nothing() -> None:
    assert found("CREATE TABLE c PARTITION OF old FOR VALUES FROM (0) TO (10); CREATE INDEX ON c (a)") == []


@pytest.mark.parametrize("statement", ["SET search_path = other", "RESET ALL", "SET LOCAL search_path TO other"])
def test_a_set_or_reset_ends_the_created_table_exemption(statement: str) -> None:
    sql = f"CREATE TABLE t (a int); {statement}; ALTER TABLE t DROP COLUMN a"
    assert found(sql) == [(D, 3, "ALTER TABLE DROP COLUMN")]


@pytest.mark.parametrize("partition", ["finalize", "concurrently"])
def test_a_detach_of_a_partition_named_like_an_option_is_a_lock_hazard(partition: str) -> None:
    assert found(f"ALTER TABLE p DETACH PARTITION {partition}") == [(L, 1, "DETACH PARTITION without CONCURRENTLY")]


@pytest.mark.parametrize("option", ["CONCURRENTLY", "FINALIZE"])
def test_a_detach_with_an_option_after_the_partition_name_is_safe(option: str) -> None:
    assert found(f"ALTER TABLE p DETACH PARTITION x {option}") == []
    assert found(f"ALTER TABLE p DETACH PARTITION s.x {option}") == []


def test_a_unicode_escaped_identifier_is_one_token_and_never_a_plain_name() -> None:
    assert found('CREATE TABLE U&"x" (a int); ALTER TABLE u DROP COLUMN a') == [(D, 2, "ALTER TABLE DROP COLUMN")]
    assert found('CREATE TABLE u (a int); CREATE INDEX ON U&"victim" (a)') == [
        (L, 2, "CREATE INDEX without CONCURRENTLY")
    ]
    assert found('CREATE TABLE u&"x" (a int); ALTER TABLE u DROP COLUMN a') == [(D, 2, "ALTER TABLE DROP COLUMN")]
    (statement,) = _sql.split("CREATE TABLE U&\"d\\0061t\" UESCAPE '!'")
    assert [(t.kind, t.text) for t in statement.tokens[2:]] == [
        (_sql.Kind.QUOTED, 'U&"d\\0061t"'),
        (_sql.Kind.WORD, "UESCAPE"),
        (_sql.Kind.STRING, "'!'"),
    ]


def test_identifiers_fold_ascii_letters_only() -> None:
    assert found("CREATE TABLE Ä (a int); ALTER TABLE ä DROP COLUMN a") == [(D, 2, "ALTER TABLE DROP COLUMN")]


@pytest.mark.parametrize("sql", ["ınsert INTO t VALUES (1)", "ſelect 1", "drıp table t"])
def test_keywords_fold_ascii_letters_only(sql: str) -> None:
    assert found(sql) == [(U, 1, "unrecognised statement")]
