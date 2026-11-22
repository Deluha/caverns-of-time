"""Tests for dev_tools/verify.py: the step list and the database requirement it exports."""

from __future__ import annotations

from dev_tools import verify
from dev_tools.database import DEFAULT_DSN


def test_steps_are_fixed_and_ordered() -> None:
    names = [step.name for step in verify.steps()]
    assert names == ["conventions", "guidance-integrity", "ruff", "mypy", "pytest", "docs-sync"]


def test_docs_sync_step_gets_the_base() -> None:
    assert verify.steps("origin/dev")[-1].command[-2:] == ["--base", "origin/dev"]


def test_environment_requires_the_database_on_the_local_instance() -> None:
    env = verify.step_environment({})
    assert env[verify.REQUIRE_DB_VARIABLE] == "1" and env[verify.DSN_VARIABLE] == DEFAULT_DSN


def test_environment_keeps_a_named_dsn_and_overrides_a_relaxed_requirement() -> None:
    env = verify.step_environment({verify.DSN_VARIABLE: "named", verify.REQUIRE_DB_VARIABLE: "0"})
    assert env[verify.DSN_VARIABLE] == "named" and env[verify.REQUIRE_DB_VARIABLE] == "1"


def test_variable_names_match_the_fixtures() -> None:
    from tests import conftest

    assert (verify.DSN_VARIABLE, verify.REQUIRE_DB_VARIABLE) == (conftest.DSN_VARIABLE, conftest.REQUIRE_DB_VARIABLE)


def test_environment_drops_pytest_options_from_the_caller() -> None:
    assert "PYTEST_ADDOPTS" not in verify.step_environment({"PYTEST_ADDOPTS": '-m "not db"'})
