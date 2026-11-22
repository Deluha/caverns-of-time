"""Chromie's pure parts: connection-string resolution, the parser skeleton and the console helpers."""

from __future__ import annotations

import json

import pytest

from caverns_of_time import chromie, console

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
