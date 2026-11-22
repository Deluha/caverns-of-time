"""Chromie: connection-string resolution, the parser, the `log` and `status` commands and the console helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from caverns_of_time import chromie, console, nozdormu
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


def test_log_refuses_a_limit_that_is_not_a_number() -> None:
    with pytest.raises(SystemExit) as raised:
        chromie.main(["log", "--limit", "many"])
    assert raised.value.code == chromie.EXIT_USAGE


def test_log_without_an_installed_ledger_is_a_usage_error(
    ledger_conn: Any, db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "log", "--dsn", db_dsn)
    assert (code, out) == (chromie.EXIT_USAGE, "")
    assert "call install()" in err


def _installed_and_applied(conn: Any, directory: Path) -> Changeset:
    """Install the ledger and apply the directory's changeset, so the ledger holds three rows."""
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
        ["3", "app", "2_table", "applied"],
        ["2", "app", "1_schema", "applied"],
        ["1", "caverns", "ledger", "applied"],
    ]
    assert re.fullmatch(r"\s*3\s+app\s+2_table\s+applied\s+\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\s+\S+", lines[1])


def test_log_limit_and_changeset(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    _, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--limit", "1")
    assert [line.split()[0] for line in out.splitlines()[1:]] == ["3"]
    _, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--limit", "0")
    assert len(out.splitlines()) == 4
    _, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--changeset", "caverns")
    assert [line.split()[0] for line in out.splitlines()[1:]] == ["1"]
    code, out, _ = _run(capsys, "log", "--dsn", db_dsn, "--changeset", "nobody")
    assert (code, out) == (0, "ℹ️ Nothing to show — changeset nobody has no ledger rows.\n")


def test_log_json_has_every_ledger_field(
    ledger_conn: Any, db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed_and_applied(ledger_conn, _changeset_directory(tmp_path))
    code, out, err = _run(capsys, "log", "--dsn", db_dsn, "--format", "json", "--limit", "2")
    rows = json.loads(out)
    assert (code, err) == (0, "")
    assert [row["version"] for row in rows] == [3, 2]
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
        ["1_schema", "applied", "2"],
        ["2_table", "applied", "3"],
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
            {"label": "1_schema", "status": "applied", "version": 2},
            {"label": "2_table", "status": "applied", "version": 3},
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
