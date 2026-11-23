"""Chromie: dsn resolution, the parser, the `log`, `status`, `drift`, `as-of` and `diff` commands, the console."""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from caverns_of_time import chromie, console, epochs, nozdormu
from caverns_of_time.nozdormu import Changeset

# --- resolve_dsn -------------------------------------------------------------------------


def test_resolve_dsn_argument_wins_over_environment() -> None:
    assert chromie.resolve_dsn("arg", {chromie.ENV_DSN: "env"}) == "arg"


def test_resolve_dsn_uses_the_environment() -> None:
    assert chromie.resolve_dsn(None, {chromie.ENV_DSN: "env"}) == "env"


def test_resolve_dsn_ignores_empty_strings() -> None:
    assert chromie.resolve_dsn("", {chromie.ENV_DSN: "env"}) == "env"
    assert chromie.resolve_dsn(None, {chromie.ENV_DSN: ""}) is None


def test_resolve_dsn_is_none_without_argument_or_environment() -> None:
    assert chromie.resolve_dsn(None, {}) is None


# --- main --------------------------------------------------------------------------------


def test_main_without_a_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        chromie.main([])
    assert raised.value.code == chromie.EXIT_USAGE == 2


# --- console -----------------------------------------------------------------------------


def test_table_pads_columns_and_aligns_right(capsys: pytest.CaptureFixture[str]) -> None:
    console.table(["name", "n"], [["alpha", 5], ["b", 120]], right=[1])
    assert capsys.readouterr().out == "name     n\nalpha    5\nb      120\n"


def test_table_prints_none_as_a_dash_and_strips_trailing_space(capsys: pytest.CaptureFixture[str]) -> None:
    console.table(["a", "b"], [["x", None]])
    assert capsys.readouterr().out == "a  b\nx  -\n"


def test_table_without_headers_prints_no_header_and_sizes_columns_from_the_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    console.table(None, [["a", 1], ["bbb", None]])
    assert capsys.readouterr().out == "a    1\nbbb  -\n"
    console.table(None, [])
    assert capsys.readouterr().out == ""


def test_success_and_info_go_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    console.success("done")
    console.info("note")
    captured = capsys.readouterr()
    assert captured.out == "✅ done\nℹ️ note\n"
    assert captured.err == ""


def test_warning_and_error_go_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    console.warning("careful")
    console.error("broken")
    captured = capsys.readouterr()
    assert captured.err == "⚠️ careful\n❌ broken\n"
    assert captured.out == ""


def test_emit_json_round_trips(capsys: pytest.CaptureFixture[str]) -> None:
    value = {"a": [1, 2], "b": None}
    console.emit_json(value)
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert json.loads(out) == value


@pytest.mark.parametrize("answer", ["y\n", "YES\n", " Yes \n", "y"])
def test_confirm_is_true_for_y_and_yes(
    answer: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert console.confirm("Go on?") is True
    assert capsys.readouterr().err == "Go on? [y/N] "


@pytest.mark.parametrize("answer", ["n\n", "no\n", "\n", "", "yep\n", "maybe\n"])
def test_confirm_is_false_for_anything_else_and_for_end_of_input(
    answer: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert console.confirm("Go on?") is False
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "Go on? [y/N] ")


def test_interactive_follows_stdin_being_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert console.interactive() is False


# --- log and status ------------------------------------------------------------------------

STEP_FILES = {
    "1_schema.sql": "CREATE SCHEMA nozdormu_test",
    "2_table.sql": "CREATE TABLE nozdormu_test.t (id int)",
}


def _changeset_directory(tmp_path: Path, name: str = "app") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    for file_name, sql in STEP_FILES.items():
        (directory / file_name).write_text(sql, encoding="utf-8")
    return directory


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = chromie.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_main_without_a_connection_string_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(chromie.ENV_DSN, raising=False)
    code, out, err = _run(capsys, "log")
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "No connection string" in err


def test_log_refuses_a_negative_limit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        chromie.main(["log", "--limit", "-1"])
    assert raised.value.code == chromie.EXIT_USAGE
    assert "--limit" in capsys.readouterr().err


def test_a_refused_connection_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    refused = "host=127.0.0.1 port=1 dbname=x connect_timeout=2"
    code, out, err = _run(capsys, "log", "--dsn", refused)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "cannot connect" in err


def test_a_missing_driver_is_a_usage_error_naming_the_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)  # `import psycopg` now raises ImportError
    code, out, err = _run(capsys, "log", "--dsn", "host=127.0.0.1 dbname=x")
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "caverns-of-time[cli]" in err


def test_an_unexpected_error_is_a_usage_error_never_a_finding(
    monkeypatch: pytest.MonkeyPatch, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(args: object, connection: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(chromie, "_log", broken)
    code, out, err = _run(capsys, "log", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert err == "❌ RuntimeError: boom\n"


def test_log_refuses_a_limit_that_is_not_a_number() -> None:
    with pytest.raises(SystemExit) as raised:
        chromie.main(["log", "--limit", "many"])
    assert raised.value.code == chromie.EXIT_USAGE


def test_log_refuses_a_limit_above_the_bigint_maximum_with_exit_2(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    nozdormu.install(ledger_conn)
    code, out, err = _run(capsys, "log", "--dsn", db_dsn, "--limit", "9223372036854775808")
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "limit" in err


def test_log_without_an_installed_ledger_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "log", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "call install()" in err


def _installed_and_applied(conn: Any, directory: Path) -> Changeset:
    """Install the ledger and apply the directory's changeset, so the ledger holds four rows."""
    changeset = Changeset.from_directory(directory.name, directory)
    nozdormu.install(conn)
    nozdormu.apply(conn, changeset)
    return changeset


def test_log_of_a_ledger_without_rows_says_so(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with ledger_conn.cursor() as cursor:  # a schema-only restore: the table, but no row
        cursor.execute(nozdormu._LEDGER_DDL_SQL)
    ledger_conn.commit()
    assert _run(capsys, "log", "--dsn", db_dsn) == (0, "ℹ️ Nothing to show — the ledger is empty.\n", "")
    code, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--format", "json")
    assert (code, json.loads(out)) == (0, [])


def test_log_shows_the_ledger_newest_first(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    code, out, err = _run(capsys, "log", "--dsn", db_dsn)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].split() == ["VERSION", "CHANGESET", "LABEL", "OUTCOME", "RECORDED_AT", "(UTC)", "BY"]
    assert [line.split()[:4] for line in lines[1:]] == [
        ["5", "app", "2_table", "applied"],
        ["4", "app", "1_schema", "applied"],
        ["3", "caverns", "epochs", "applied"],
        ["2", "caverns", "fingerprint", "applied"],
        ["1", "caverns", "ledger", "applied"],
    ]
    assert re.fullmatch(r"\s*5\s+app\s+2_table\s+applied\s+\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\s+\S+", lines[1])


def test_log_limit_and_changeset(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    _, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--limit", "1")
    assert [line.split()[0] for line in out.splitlines()[1:]] == ["5"]
    _, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--limit", "0")
    assert len(out.splitlines()) == 6
    _, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--changeset", "caverns")
    assert [line.split()[0] for line in out.splitlines()[1:]] == ["3", "2", "1"]
    code, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--changeset", "nobody")
    assert (code, out) == (0, "ℹ️ Nothing to show — changeset nobody has no ledger rows.\n")


def test_log_json_has_every_ledger_field(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    code, out, err = _run(capsys, "log", "--dsn", db_dsn, "--format", "json", "--limit", "2")
    rows = json.loads(out)
    assert (code, err) == (0, "")
    assert [row["version"] for row in rows] == [5, 4]
    assert set(rows[0]) == {
        "version", "changeset", "label", "checksum", "outcome", "fingerprint", "error",
        "duration_ms", "recorded_at", "recorded_by", "caverns_version",
    }  # fmt: skip
    assert rows[0]["label"] == "2_table"
    assert re.search(r"[+-]\d\d:\d\d$", rows[0]["recorded_at"])


def test_status_of_applied_steps_succeeds(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    _installed_and_applied(ledger_conn, directory)
    code, out, err = _run(capsys, "status", str(directory), "--dsn", db_dsn)
    assert (code, err) == (0, "")
    assert [line.split() for line in out.splitlines()] == [
        ["LABEL", "STATUS", "VERSION"],
        ["1_schema", "applied", "4"],
        ["2_table", "applied", "5"],
        ["✅", "All", "2", "steps", "of", "changeset", "app", "applied."],
    ]


def test_status_of_a_pending_step_is_a_finding(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    nozdormu.install(ledger_conn)
    code, out, err = _run(capsys, "status", str(directory), "--dsn", db_dsn)
    assert code == chromie.EXIT_FINDING
    assert [line.split() for line in out.splitlines()] == [
        ["LABEL", "STATUS", "VERSION"],
        ["1_schema", "pending", "-"],
        ["2_table", "pending", "-"],
    ]
    assert err == "❌ 2 of 2 steps of changeset app not applied: 1_schema (pending), 2_table (pending).\n"


def test_status_takes_the_changeset_name_from_the_option(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    nozdormu.install(ledger_conn)
    code, _, err = _run(capsys, "status", str(directory), "--dsn", db_dsn, "--changeset", "other")
    assert code == chromie.EXIT_FINDING
    assert "of changeset other not applied" in err


def test_status_json_shape(ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _changeset_directory(tmp_path)
    _installed_and_applied(ledger_conn, directory)
    (directory / "3_later.sql").write_text("SELECT 1", encoding="utf-8")
    code, out, err = _run(capsys, "status", str(directory), "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (chromie.EXIT_FINDING, "")
    assert json.loads(out) == {
        "changeset": "app",
        "steps": [
            {"label": "1_schema", "status": "applied", "version": 4},
            {"label": "2_table", "status": "applied", "version": 5},
            {"label": "3_later", "status": "pending", "version": None},
        ],
    }


def test_status_json_of_applied_steps_succeeds(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    _installed_and_applied(ledger_conn, directory)
    code, out, err = _run(capsys, "status", str(directory), "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (0, "")
    assert [step["status"] for step in json.loads(out)["steps"]] == ["applied", "applied"]


def test_status_of_a_missing_directory_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nozdormu.install(ledger_conn)
    code, out, err = _run(capsys, "status", str(tmp_path / "missing"), "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "missing" in err


def test_status_without_an_installed_ledger_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "status", str(_changeset_directory(tmp_path)), "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "call install()" in err


# --- drift ---------------------------------------------------------------------------------

ADD_COLUMN = "ALTER TABLE nozdormu_test.t ADD COLUMN extra text"
CHECK_FILE = "2_table.check.sql"
TABLE_CHECK = "SELECT to_regclass('nozdormu_test.t') IS NOT NULL"


def _out_of_band(conn: Any, *statements: str) -> None:
    conn.rollback()
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


def test_drift_of_an_unchanged_database_is_clean(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    _installed_and_applied(ledger_conn, directory)
    code, out, err = _run(capsys, "drift", str(directory), "--dsn", db_dsn)
    assert (code, err) == (0, "")
    first, last = out.splitlines()
    assert re.fullmatch(r"ℹ️ Comparing with the fingerprint at ledger version 5 \(schemas: .*nozdormu_test.*\)\.", first)
    assert last == "✅ No drift — the live schema matches the fingerprint at ledger version 5."


def test_drift_lists_each_difference_and_exits_1(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    _out_of_band(ledger_conn, "DROP TABLE nozdormu_test.t", "CREATE TABLE nozdormu_test.u (id int)")
    code, out, err = _run(capsys, "drift", "--dsn", db_dsn, "--schema", "nozdormu_test")
    assert code == chromie.EXIT_FINDING
    assert out.splitlines() == [
        "ℹ️ Comparing with the fingerprint at ledger version 5 (schemas: nozdormu_test).",
        "-  table   nozdormu_test.t",
        "+  table   nozdormu_test.u",
        "-  column  nozdormu_test.t.id",
        "+  column  nozdormu_test.u.id",
    ]
    assert err == "❌ Drift: 4 differences.\n"


def test_drift_says_difference_in_the_singular(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    _out_of_band(ledger_conn, ADD_COLUMN)
    code, out, err = _run(capsys, "drift", "--dsn", db_dsn, "--schema", "nozdormu_test")
    assert (code, err) == (chromie.EXIT_FINDING, "❌ Drift: 1 difference.\n")
    assert out.splitlines()[1:] == ["+  column  nozdormu_test.t.extra"]


def test_drift_narrows_to_the_schemas_named(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    _out_of_band(ledger_conn, ADD_COLUMN)
    code, out, _ = _run(capsys, "drift", "--dsn", db_dsn, "--schema", "public")
    assert code == 0 and "(schemas: public)" in out
    code, out, _ = _run(capsys, "drift", "--dsn", db_dsn, "--schema", "public", "--schema", "nozdormu_test")
    assert code == chromie.EXIT_FINDING and "(schemas: nozdormu_test, public)" in out


def test_drift_reports_a_step_whose_check_is_now_false(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    (directory / CHECK_FILE).write_text(TABLE_CHECK, encoding="utf-8")
    _installed_and_applied(ledger_conn, directory)
    _out_of_band(ledger_conn, "DROP TABLE nozdormu_test.t")
    code, out, err = _run(capsys, "drift", str(directory), "--dsn", db_dsn, "--schema", "nozdormu_test")
    assert code == chromie.EXIT_FINDING
    assert out.splitlines()[1:] == [
        "-  table   nozdormu_test.t",
        "-  column  nozdormu_test.t.id",
        "~  step    app/2_table: recorded applied, check now false",
    ]
    assert err == "❌ Drift: 3 differences.\n"
    code, out, _ = _run(capsys, "drift", str(directory), "--dsn", db_dsn, "--changeset", "other", "--schema", "public")
    assert code == 0  # another changeset has no recorded steps: nothing disagrees, and public is unchanged


def test_drift_json_of_a_clean_database(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    code, out, err = _run(capsys, "drift", "--dsn", db_dsn, "--format", "json", "--schema", "nozdormu_test")
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "fingerprint_version": 5,
        "schemas": ["nozdormu_test"],
        "differences": [],
        "disagreements": [],
    }


def test_drift_json_of_a_drifted_database(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    (directory / CHECK_FILE).write_text(TABLE_CHECK, encoding="utf-8")
    _installed_and_applied(ledger_conn, directory)
    _out_of_band(ledger_conn, "DROP TABLE nozdormu_test.t")
    code, out, err = _run(
        capsys, "drift", str(directory), "--dsn", db_dsn, "--format", "json", "--schema", "nozdormu_test"
    )
    assert (code, err) == (chromie.EXIT_FINDING, "")
    assert json.loads(out) == {
        "fingerprint_version": 5,
        "schemas": ["nozdormu_test"],
        "differences": [
            {"kind": "table", "name": "nozdormu_test.t", "change": "removed"},
            {"kind": "column", "name": "nozdormu_test.t.id", "change": "removed"},
        ],
        "disagreements": [{"changeset": "app", "label": "2_table", "version": 5}],
    }


def test_drift_takes_the_changeset_name_from_the_option(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _changeset_directory(tmp_path)
    (directory / CHECK_FILE).write_text(TABLE_CHECK, encoding="utf-8")
    _installed_and_applied(ledger_conn, directory)
    _out_of_band(ledger_conn, "DROP TABLE nozdormu_test.t")
    _, out, _ = _run(capsys, "drift", str(directory), "--dsn", db_dsn, "--format", "json", "--changeset", "app")
    assert [d["label"] for d in json.loads(out)["disagreements"]] == ["2_table"]


def test_drift_changeset_without_a_directory_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    nozdormu.install(ledger_conn)
    code, out, err = _run(capsys, "drift", "--dsn", db_dsn, "--changeset", "app")
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "--changeset needs a DIRECTORY" in err


def test_drift_without_a_fingerprint_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    nozdormu.install(ledger_conn)
    code, out, err = _run(capsys, "drift", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "no fingerprint is recorded yet" in err


def test_drift_without_an_installed_ledger_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "drift", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "call install()" in err


def test_drift_of_an_unwatched_schema_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    code, _, err = _run(capsys, "drift", "--dsn", db_dsn, "--schema", "caverns")
    assert code == chromie.EXIT_USAGE and "'caverns' is not watched" in err


# --- as-of -----------------------------------------------------------------------------------

USERS = "CREATE TABLE nozdormu_test.users (id int PRIMARY KEY, email text, nickname text, active boolean, tags jsonb)"
TAKEN = r"taken \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC"


def _epoch_of(conn: Any, *statements: str, name: str = "run-42") -> epochs.Epoch:
    """Caverns installed, nozdormu_test.users created, filled by *statements* and snapshotted as epoch *name*."""
    nozdormu.install(conn)
    with conn.cursor() as cursor:
        for statement in ("CREATE SCHEMA nozdormu_test", USERS, *statements):
            cursor.execute(statement)
    conn.commit()
    epochs.register(conn, "nozdormu_test", "users")
    return epochs.snapshot(conn, name)


def test_as_of_prints_the_header_and_the_table(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    taken = _epoch_of(
        ledger_conn,
        "INSERT INTO nozdormu_test.users VALUES (10, 'bob@example.org', NULL, false, '{\"a\": 1}'),"
        " (2, 'ann@example.org', 'annie', true, '[1, \"x\"]')",
    )
    code, out, err = _run(capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn)
    assert (code, err) == (0, "")
    header, *lines = out.splitlines()
    assert re.fullmatch(
        rf"ℹ️ nozdormu_test\.users at epoch run-42 \(ledger version {taken.ledger_version}, {TAKEN}\):"
        r" 2 rows, key \(id\)\.",
        header,
    )
    assert [line.split() for line in lines] == [
        ["id", "email", "nickname", "active", "tags"],
        ["2", "ann@example.org", "annie", "true", '[1,"x"]'],
        ["10", "bob@example.org", "-", "false", '{"a":1}'],
    ]


def test_as_of_says_one_row_in_the_singular_and_prints_no_table_for_none(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _epoch_of(ledger_conn, "INSERT INTO nozdormu_test.users (id) VALUES (1)")
    _, out, _ = _run(capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn)
    assert out.splitlines()[0].endswith("): 1 row, key (id).")
    assert out.splitlines()[2].split() == ["1", "-", "-", "-", "-"]
    with ledger_conn.cursor() as cursor:
        cursor.execute("DELETE FROM nozdormu_test.users")
    ledger_conn.commit()
    epochs.snapshot(ledger_conn, "empty")
    code, out, _ = _run(capsys, "as-of", "empty", "nozdormu_test.users", "--dsn", db_dsn)
    assert code == 0 and len(out.splitlines()) == 1 and out.endswith("): 0 rows, key (id).\n")


def test_as_of_limits_to_twenty_rows_by_default_and_says_so(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _epoch_of(ledger_conn, "INSERT INTO nozdormu_test.users (id) SELECT generate_series(1, 25)")
    code, out, _ = _run(capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn)
    lines = out.splitlines()
    assert code == 0 and lines[0].endswith("): 25 rows, key (id).")
    assert [line.split()[0] for line in lines[2:-1]] == [str(i) for i in range(1, 21)]
    assert lines[-1] == "ℹ️ Showing 20 of 25 rows — --limit 0 shows all."
    _, out, _ = _run(capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn, "--limit", "0")
    assert len(out.splitlines()) == 2 + 25 and "Showing" not in out
    _, out, _ = _run(capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn, "--limit", "3")
    assert out.splitlines()[-1] == "ℹ️ Showing 3 of 25 rows — --limit 0 shows all."


def test_as_of_json_shape(ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    taken = _epoch_of(
        ledger_conn,
        "INSERT INTO nozdormu_test.users VALUES (2, 'ann@example.org', NULL, true, '{\"a\": [1]}'),"
        " (1, 'x', 'y', NULL, NULL)",
    )
    code, out, err = _run(capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "epoch": "run-42",
        "ledger_version": taken.ledger_version,
        "taken_at": taken.taken_at.isoformat(),
        "schema": "nozdormu_test",
        "table": "users",
        "key": ["id"],
        "columns": [
            {"name": "id", "type": "integer"},
            {"name": "email", "type": "text"},
            {"name": "nickname", "type": "text"},
            {"name": "active", "type": "boolean"},
            {"name": "tags", "type": "jsonb"},
        ],
        "row_count": 2,
        "rows": [
            {"id": 1, "email": "x", "nickname": "y", "active": None, "tags": None},
            {"id": 2, "email": "ann@example.org", "nickname": None, "active": True, "tags": {"a": [1]}},
        ],
    }
    _, out, _ = _run(
        capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn, "--format", "json", "--limit", "1"
    )
    parsed = json.loads(out)
    assert (parsed["row_count"], len(parsed["rows"])) == (2, 1)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("nope", "nozdormu_test.users"), "no epoch is named 'nope'"),
        (("run-42", "nozdormu_test.other"), "epoch 'run-42' has no table nozdormu_test.other"),
        (("run-42", "a.b.c"), "epoch 'run-42' has no table a.b.c"),
    ],
)
def test_as_of_of_an_unknown_epoch_or_a_missing_table_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], message: str
) -> None:
    _epoch_of(ledger_conn)
    code, out, err = _run(capsys, "as-of", *argv, "--dsn", db_dsn)
    assert (code, out, err) == (chromie.EXIT_USAGE, "", f"❌ {message}\n")


def test_as_of_of_a_deleted_epoch_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    taken = _epoch_of(ledger_conn)
    with ledger_conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO caverns.epoch_deletion (epoch, caverns_version) VALUES (%s, 'test')", (taken.epoch,)
        )
    ledger_conn.commit()
    code, out, err = _run(capsys, "as-of", "run-42", "nozdormu_test.users", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert re.fullmatch(r"❌ epoch 'run-42' was deleted at \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC\n", err)


def test_as_of_without_an_installed_ledger_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "as-of", "run-42", "public.users", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "call install()" in err


@pytest.mark.parametrize("table", ["public", ".users", "public.", "."])
def test_as_of_refuses_a_table_that_is_not_schema_dot_table(table: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        chromie.main(["as-of", "run-42", table])
    assert raised.value.code == chromie.EXIT_USAGE
    assert f"expected SCHEMA.TABLE, got {table!r}" in capsys.readouterr().err


def test_as_of_splits_the_table_at_the_first_dot() -> None:
    args = chromie.build_parser().parse_args(["as-of", "e", "s.a.b"])
    assert args.table == ("s", "a.b")
    assert (args.epoch, args.limit) == ("e", chromie.DEFAULT_LOG_LIMIT)


def test_as_of_refuses_a_bad_limit(capsys: pytest.CaptureFixture[str]) -> None:
    for limit in ("-1", "many"):
        with pytest.raises(SystemExit) as raised:
            chromie.main(["as-of", "e", "s.t", "--limit", limit])
        assert raised.value.code == chromie.EXIT_USAGE
    assert "--limit" in capsys.readouterr().err


# --- diff ------------------------------------------------------------------------------------

CODES = "CREATE TABLE nozdormu_test.codes (code text PRIMARY KEY, label text)"
MIXED_LINES = [
    "~  column  nozdormu_test.codes.label: text → character varying(5)",
    '~  row     nozdormu_test.codes (code="x"): label',
    "-  column  nozdormu_test.users.tags",
    "+  column  nozdormu_test.users.age",
    "+  row     nozdormu_test.users (id=3)",
    "-  row     nozdormu_test.users (id=1)",
    "~  row     nozdormu_test.users (id=2): nickname",
]


def _two_epochs(conn: Any, *between: str) -> tuple[epochs.Epoch, epochs.Epoch]:
    """Caverns installed, `users` and `codes` filled and snapshotted as run-1; *between* run, then run-2."""
    nozdormu.install(conn)
    with conn.cursor() as cursor:
        for statement in (
            "CREATE SCHEMA nozdormu_test",
            USERS,
            CODES,
            "INSERT INTO nozdormu_test.users VALUES (1, 'ann@example.org', NULL, true, NULL),"
            " (2, 'bob@example.org', 'bobby', true, '[1]')",
            "INSERT INTO nozdormu_test.codes VALUES ('x', 'old'), ('y', 'same')",
        ):
            cursor.execute(statement)
    conn.commit()
    epochs.register(conn, "nozdormu_test", "users")
    epochs.register(conn, "nozdormu_test", "codes")
    first = epochs.snapshot(conn, "run-1")
    with conn.cursor() as cursor:
        for statement in between:
            cursor.execute(statement)
    conn.commit()
    return first, epochs.snapshot(conn, "run-2")


MIXED_CHANGES = (
    "DELETE FROM nozdormu_test.users WHERE id = 1",
    "INSERT INTO nozdormu_test.users VALUES (3, 'cy@example.org', NULL, false, NULL)",
    "UPDATE nozdormu_test.users SET nickname = 'bob' WHERE id = 2",
    "ALTER TABLE nozdormu_test.users DROP COLUMN tags, ADD COLUMN age int",
    "UPDATE nozdormu_test.codes SET label = 'new' WHERE code = 'x'",
    "ALTER TABLE nozdormu_test.codes ALTER COLUMN label TYPE varchar(5)",
)


def test_diff_prints_each_difference_and_exits_1(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = _two_epochs(ledger_conn, *MIXED_CHANGES)
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn)
    assert code == chromie.EXIT_FINDING
    assert out.splitlines() == [
        f"ℹ️ Comparing epoch run-1 (ledger version {first.ledger_version})"
        f" with run-2 (ledger version {second.ledger_version}).",
        *MIXED_LINES,
    ]
    assert err == "❌ 7 differences in 2 tables.\n"


def test_diff_says_difference_and_table_in_the_singular(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_epochs(ledger_conn, "UPDATE nozdormu_test.codes SET label = 'new' WHERE code = 'y'")
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn)
    assert (code, err) == (chromie.EXIT_FINDING, "❌ 1 difference in 1 table.\n")
    assert out.splitlines()[1:] == ['~  row  nozdormu_test.codes (code="y"): label']


def test_diff_quotes_a_string_key_as_json(ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    _two_epochs(ledger_conn, "INSERT INTO nozdormu_test.codes VALUES ('é\"q', 'n')")
    _, out, _ = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn)
    assert out.splitlines()[1:] == ['+  row  nozdormu_test.codes (code="é\\"q")']


def test_diff_prints_a_table_in_one_epoch_only_as_added_or_removed(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_epochs(ledger_conn)
    with ledger_conn.cursor() as cursor:
        cursor.execute("CREATE TABLE nozdormu_test.extra (id int PRIMARY KEY)")
    ledger_conn.commit()
    epochs.register(ledger_conn, "nozdormu_test", "extra")
    epochs.snapshot(ledger_conn, "run-3")
    code, out, err = _run(capsys, "diff", "run-2", "run-3", "--dsn", db_dsn)
    assert (code, err) == (chromie.EXIT_FINDING, "❌ 1 difference in 1 table.\n")
    assert out.splitlines()[1:] == ["+  table  nozdormu_test.extra"]
    _, out, _ = _run(capsys, "diff", "run-3", "run-2", "--dsn", db_dsn)
    assert out.splitlines()[1:] == ["-  table  nozdormu_test.extra"]


def test_diff_of_identical_epochs_succeeds(ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    first, second = _two_epochs(ledger_conn)
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn)
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        f"ℹ️ Comparing epoch run-1 (ledger version {first.ledger_version})"
        f" with run-2 (ledger version {second.ledger_version}).",
        "✅ No differences between epochs run-1 and run-2.",
    ]


def test_diff_narrows_to_the_tables_named(ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    _two_epochs(ledger_conn, *MIXED_CHANGES)
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn, "--table", "nozdormu_test.codes")
    assert (code, err) == (chromie.EXIT_FINDING, "❌ 2 differences in 1 table.\n")
    assert out.splitlines()[1:] == MIXED_LINES[:2]
    both = ("--table", "nozdormu_test.users", "--table", "nozdormu_test.codes")
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn, *both)
    assert (code, out.splitlines()[1:], err) == (chromie.EXIT_FINDING, MIXED_LINES, "❌ 7 differences in 2 tables.\n")


def test_diff_of_a_table_neither_epoch_has_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_epochs(ledger_conn)
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn, "--table", "nozdormu_test.nope")
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert err == "❌ neither epoch 'run-1' nor 'run-2' has table nozdormu_test.nope\n"


@pytest.mark.parametrize("table", ["public", ".users", "public.", "."])
def test_diff_refuses_a_table_that_is_not_schema_dot_table(table: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        chromie.main(["diff", "a", "b", "--table", table])
    assert raised.value.code == chromie.EXIT_USAGE
    assert f"expected SCHEMA.TABLE, got {table!r}" in capsys.readouterr().err


def test_diff_parses_the_epochs_and_the_repeatable_table() -> None:
    parser = chromie.build_parser()
    assert parser.parse_args(["diff", "a", "b"]).table is None
    args = parser.parse_args(["diff", "a", "b", "--table", "s.t", "--table", "u.v.w"])
    assert (args.old, args.new, args.table) == ("a", "b", [("s", "t"), ("u", "v.w")])


def test_diff_json_shape(ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    first, second = _two_epochs(ledger_conn, *MIXED_CHANGES)
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (chromie.EXIT_FINDING, "")
    assert json.loads(out) == {
        "old": {"epoch": "run-1", "ledger_version": first.ledger_version},
        "new": {"epoch": "run-2", "ledger_version": second.ledger_version},
        "tables": [
            {
                "schema": "nozdormu_test",
                "table": "codes",
                "change": "changed",
                "key": ["code"],
                "columns": [
                    {"name": "label", "change": "changed", "old_type": "text", "new_type": "character varying(5)"}
                ],
                "added": [],
                "removed": [],
                "changed": [{"key": {"code": "x"}, "old": {"label": "old"}, "new": {"label": "new"}}],
            },
            {
                "schema": "nozdormu_test",
                "table": "users",
                "change": "changed",
                "key": ["id"],
                "columns": [
                    {"name": "tags", "change": "removed", "old_type": "jsonb", "new_type": None},
                    {"name": "age", "change": "added", "old_type": None, "new_type": "integer"},
                ],
                "added": [{"id": 3}],
                "removed": [{"id": 1}],
                "changed": [{"key": {"id": 2}, "old": {"nickname": "bobby"}, "new": {"nickname": "bob"}}],
            },
        ],
    }


def test_diff_json_of_identical_epochs_succeeds(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = _two_epochs(ledger_conn)
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "old": {"epoch": "run-1", "ledger_version": first.ledger_version},
        "new": {"epoch": "run-2", "ledger_version": second.ledger_version},
        "tables": [],
    }


@pytest.mark.parametrize("argv", [("nope", "run-2"), ("run-1", "nope")])
def test_diff_of_an_unknown_epoch_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    _two_epochs(ledger_conn)
    code, out, err = _run(capsys, "diff", *argv, "--dsn", db_dsn)
    assert (code, out, err) == (chromie.EXIT_USAGE, "", "❌ no epoch is named 'nope'\n")


def test_diff_of_a_key_that_changed_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_epochs(ledger_conn)
    epochs.register(ledger_conn, "nozdormu_test", "users", key=["email"])
    epochs.snapshot(ledger_conn, "run-3")
    code, out, err = _run(capsys, "diff", "run-2", "run-3", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert err.startswith("❌ the key of nozdormu_test.users differs between epochs 'run-2' (id) and 'run-3' (email)")
    code, _, _ = _run(capsys, "diff", "run-2", "run-3", "--dsn", db_dsn, "--table", "nozdormu_test.codes")
    assert code == 0


def test_diff_without_an_installed_ledger_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "diff", "run-1", "run-2", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "call install()" in err


# --- epochs and delete-epoch -----------------------------------------------------------------

EPOCH_HEADERS = ["EPOCH", "NAME", "LEDGER", "TAKEN_AT", "(UTC)", "BY", "TABLES", "ROWS", "DELETED", "(UTC)"]
USER_ROWS = (
    "INSERT INTO nozdormu_test.users VALUES (1, 'ann@example.org', NULL, true, NULL),"
    " (2, 'bob@example.org', 'bobby', true, '[1]')"
)


def _three_epochs_of_users(conn: Any) -> list[epochs.Epoch]:
    """`users` with two rows snapshotted as run-1, run-2 and run-3."""
    first = _epoch_of(conn, USER_ROWS, name="run-1")
    return [first, *(epochs.snapshot(conn, name) for name in ("run-2", "run-3"))]


def _kept_rows(conn: Any) -> list[tuple[str, int]]:
    """(epoch name, rows kept) for every epoch."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT e.name, count(r.epoch) FROM caverns.epoch e LEFT JOIN caverns.epoch_row r ON r.epoch = e.epoch"
            " GROUP BY e.name ORDER BY e.name"
        )
        rows = [(name, count) for name, count in cursor.fetchall()]
    conn.commit()
    return rows


def _token_ends(line: str) -> list[int]:
    return [match.end() for match in re.finditer(r"\S+", line)]


def test_epochs_prints_the_epochs_newest_first_with_a_dash_for_a_live_one(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    run1, run2, run3 = _three_epochs_of_users(ledger_conn)
    epochs.delete(ledger_conn, ["run-2"])
    code, out, err = _run(capsys, "epochs", "--dsn", db_dsn)
    assert (code, err) == (0, "")
    header, *lines = out.splitlines()
    assert header.split() == EPOCH_HEADERS
    rows = [line.split() for line in lines]
    assert [r[:3] for r in rows] == [[str(e.epoch), e.name, str(e.ledger_version)] for e in (run3, run2, run1)]
    assert [r[6:8] for r in rows] == [["1", "2"]] * 3  # tables, rows
    assert [r[8:] for r in (rows[0], rows[2])] == [["-"], ["-"]]
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", " ".join(rows[1][8:]))
    ends = _token_ends(header)
    for line in lines:  # EPOCH, LEDGER, TABLES and ROWS are right-aligned under their headers
        assert [_token_ends(line)[i] for i in (0, 2, 6, 7)] == [ends[i] for i in (0, 2, 6, 7)]


def test_epochs_json_shape(ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    _epoch_of(ledger_conn, USER_ROWS, name="run-1")
    run2 = epochs.snapshot(ledger_conn, "run-2")
    (deleted,) = epochs.delete(ledger_conn, ["run-1"])
    code, out, err = _run(capsys, "epochs", "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (0, "")
    newest, oldest = json.loads(out)
    assert newest == {
        "epoch": run2.epoch,
        "name": "run-2",
        "ledger_version": run2.ledger_version,
        "taken_at": run2.taken_at.isoformat(),
        "taken_by": run2.taken_by,
        "caverns_version": run2.caverns_version,
        "deleted_at": None,
        "tables": [
            {
                "schema": "nozdormu_test",
                "table": "users",
                "key": ["id"],
                "columns": [
                    {"name": "id", "type": "integer"},
                    {"name": "email", "type": "text"},
                    {"name": "nickname", "type": "text"},
                    {"name": "active", "type": "boolean"},
                    {"name": "tags", "type": "jsonb"},
                ],
                "row_count": 2,
            }
        ],
    }
    assert deleted.deleted_at is not None
    assert (oldest["name"], oldest["deleted_at"]) == ("run-1", deleted.deleted_at.isoformat())


def test_epochs_limit(ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    _three_epochs_of_users(ledger_conn)
    code, out, _ = _run(capsys, "epochs", "--dsn", db_dsn, "--limit", "2", "--format", "json")
    assert (code, [e["name"] for e in json.loads(out)]) == (0, ["run-3", "run-2"])
    code, out, _ = _run(capsys, "epochs", "--dsn", db_dsn, "--limit", "0", "--format", "json")
    assert [e["name"] for e in json.loads(out)] == ["run-3", "run-2", "run-1"]


def test_epochs_with_none_says_so_and_json_is_an_empty_list(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    nozdormu.install(ledger_conn)
    assert _run(capsys, "epochs", "--dsn", db_dsn) == (0, "ℹ️ Nothing to show — no epoch is recorded.\n", "")
    assert _run(capsys, "epochs", "--dsn", db_dsn, "--format", "json") == (0, "[]\n", "")


def test_epochs_without_an_installed_ledger_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "epochs", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "call install()" in err


def test_delete_epoch_with_yes_deletes_and_says_so(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _three_epochs_of_users(ledger_conn)
    code, out, err = _run(capsys, "delete-epoch", "run-1", "run-3", "--yes", "--dsn", db_dsn)
    assert (code, err) == (0, "")
    assert out == "✅ Deleted epoch run-3 (2 rows in 1 table).\n✅ Deleted epoch run-1 (2 rows in 1 table).\n"
    assert _kept_rows(ledger_conn) == [("run-1", 0), ("run-2", 2), ("run-3", 0)]


def test_delete_epoch_json_lists_the_deleted_epochs(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _three_epochs_of_users(ledger_conn)
    code, out, err = _run(capsys, "delete-epoch", "run-2", "run-2", "--yes", "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (0, "")
    (deleted,) = json.loads(out)
    assert deleted["name"] == "run-2"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT.*[+-]\d\d:\d\d", deleted["deleted_at"])
    assert deleted["tables"][0]["row_count"] == 2
    assert _kept_rows(ledger_conn) == [("run-1", 2), ("run-2", 0), ("run-3", 2)]


def test_delete_epoch_dry_run_deletes_nothing(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _three_epochs_of_users(ledger_conn)
    code, out, err = _run(capsys, "delete-epoch", "run-1", "run-2", "--dry-run", "--dsn", db_dsn)
    assert (code, err) == (0, "")
    first, second, last = out.splitlines()
    assert re.fullmatch(rf"ℹ️ Would delete epoch run-1 \(2 rows in 1 table, {TAKEN}\)\.", first)
    assert re.fullmatch(rf"ℹ️ Would delete epoch run-2 \(2 rows in 1 table, {TAKEN}\)\.", second)
    assert last == "ℹ️ Dry run — nothing deleted."
    code, out, err = _run(capsys, "delete-epoch", "run-1", "--dry-run", "--yes", "--dsn", db_dsn, "--format", "json")
    assert (code, err) == (0, "")
    (planned,) = json.loads(out)
    assert (planned["name"], planned["deleted_at"]) == ("run-1", None)
    assert _kept_rows(ledger_conn) == [("run-1", 2), ("run-2", 2), ("run-3", 2)]
    assert _query_count(ledger_conn) == 0


def _query_count(conn: Any) -> int:
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM caverns.epoch_deletion")
        (count,) = cursor.fetchone()
    conn.commit()
    return int(count)


def test_delete_epoch_without_yes_needs_a_terminal(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _three_epochs_of_users(ledger_conn)
    monkeypatch.setattr(console, "interactive", lambda: False)
    code, out, err = _run(capsys, "delete-epoch", "run-1", "--dsn", db_dsn)
    assert (code, out, err) == (chromie.EXIT_USAGE, "", "❌ delete-epoch needs --yes when not run interactively\n")
    assert _query_count(ledger_conn) == 0


def test_delete_epoch_interactive_yes_deletes(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _three_epochs_of_users(ledger_conn)
    monkeypatch.setattr(console, "interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    code, out, err = _run(capsys, "delete-epoch", "run-1", "run-2", "--dsn", db_dsn)
    assert code == 0
    assert err == "Delete 2 epochs (run-1, run-2) and their 4 rows? [y/N] "
    assert out.count("✅ Deleted epoch") == 2
    assert _kept_rows(ledger_conn) == [("run-1", 0), ("run-2", 0), ("run-3", 2)]


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_delete_epoch_interactive_no_or_end_of_input_deletes_nothing(
    answer: str,
    ledger_conn: Any,
    db_dsn: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _three_epochs_of_users(ledger_conn)
    monkeypatch.setattr(console, "interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    code, out, err = _run(capsys, "delete-epoch", "run-1", "--dsn", db_dsn)
    assert (code, out) == (0, "ℹ️ Aborted — no changes were made.\n")
    assert err == "Delete 1 epoch (run-1) and their 2 rows? [y/N] "
    assert _kept_rows(ledger_conn) == [("run-1", 2), ("run-2", 2), ("run-3", 2)]
    assert _query_count(ledger_conn) == 0


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (("run-1", "nope"), "no epoch is named 'nope'"),
        (("run-1", "run-2"), "epoch 'run-2' is already deleted"),
    ],
)
def test_delete_epoch_of_an_unknown_or_deleted_epoch_is_a_usage_error_and_deletes_nothing(
    names: tuple[str, ...], message: str, ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _three_epochs_of_users(ledger_conn)
    epochs.delete(ledger_conn, ["run-2"])
    code, out, err = _run(capsys, "delete-epoch", *names, "--yes", "--dsn", db_dsn)
    assert (code, out, err) == (chromie.EXIT_USAGE, "", f"❌ {message}\n")
    assert _kept_rows(ledger_conn) == [("run-1", 2), ("run-2", 0), ("run-3", 2)]
    code, _, err = _run(capsys, "delete-epoch", *names, "--dry-run", "--dsn", db_dsn)
    assert (code, err) == (chromie.EXIT_USAGE, f"❌ {message}\n")


def test_delete_epoch_needs_an_epoch_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        chromie.main(["delete-epoch", "--yes", "--dsn", "x"])
    assert raised.value.code == chromie.EXIT_USAGE
    assert "EPOCH" in capsys.readouterr().err
