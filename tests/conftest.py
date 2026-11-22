"""Shared fixtures: the PostgreSQL test connection and its guards (tests/README.md).

The connection string comes from CAVERNS_TEST_DSN and must point at this machine.
Without it a database test is skipped with a notice, unless CAVERNS_REQUIRE_DB=1
(set by dev_tools/verify.py and CI), which turns the skip into a failure.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

DSN_VARIABLE = "CAVERNS_TEST_DSN"
REQUIRE_DB_VARIABLE = "CAVERNS_REQUIRE_DB"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DB_FIXTURES = {"db_dsn", "db_conn"}
DB_MARKER = "db"
_db_tests_passed = 0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that takes a database fixture with `db`."""
    for item in items:
        if DB_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(getattr(pytest.mark, DB_MARKER))


def resolve_dsn(environ: dict[str, str]) -> tuple[str | None, str | None]:
    """(dsn, problem): the test connection string, or why there is none.

    A problem with a dsn of None is a failure when the database is required and a
    skip otherwise; a non-local host is always a failure (the dsn is returned too).
    """
    dsn = environ.get(DSN_VARIABLE)
    if not dsn:
        return None, f"{DSN_VARIABLE} is not set — database tests did not run (tests/README.md)"
    target = non_local_target(dsn)
    if target:
        return dsn, f"{DSN_VARIABLE} points at {target} — tests connect to this machine only"
    return dsn, None


def non_local_target(dsn: str) -> str | None:
    """What makes *dsn* reach beyond this machine, as libpq reads it, or None when it is local.

    Every entry of `host` and `hostaddr` must be local (a host list or a query parameter can name
    a second server), a host must be named (else libpq falls back to the environment) and a
    service file entry is refused because its target is not visible here.
    """
    try:
        info = conninfo_to_dict(dsn)
    except psycopg.Error:
        return "an unreadable connection string"
    if info.get("service"):
        return f"service `{info['service']}`"
    hosts = [h for key in ("host", "hostaddr") for h in str(info.get(key) or "").split(",") if h]
    if not info.get("host"):
        return "no named host"
    remote = [h for h in hosts if h not in LOCAL_HOSTS]
    return f"`{remote[0]}`" if remote else None


def missing_db_run_problem(environ: dict[str, str], db_tests_passed: int) -> str | None:
    """Why a run that requires the database is not acceptable, or None.

    With CAVERNS_REQUIRE_DB=1 at least one database test must have passed: a run that deselected
    them (`-m "not db"`) or lost the files that hold them is not a verified run (hard rule 8).
    """
    if environ.get(REQUIRE_DB_VARIABLE) == "1" and db_tests_passed == 0:
        return f"{REQUIRE_DB_VARIABLE}=1 but no database test passed — the database tests did not run"
    return None


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _db_tests_passed
    if report.when == "call" and report.passed and DB_MARKER in report.keywords:
        _db_tests_passed += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    problem = missing_db_run_problem(dict(os.environ), _db_tests_passed)
    if problem and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if problem:
        session.config.stash[_PROBLEM_KEY] = problem


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    problem = config.stash.get(_PROBLEM_KEY, None)
    if problem:
        terminalreporter.write_line(f"❌ {problem}", red=True)


_PROBLEM_KEY = pytest.StashKey[str]()


@pytest.fixture(scope="session")
def db_dsn() -> str:
    """The local test connection string; skips or fails per the module docstring."""
    dsn, problem = resolve_dsn(dict(os.environ))
    if problem and (dsn or os.environ.get(REQUIRE_DB_VARIABLE) == "1"):
        pytest.fail(problem, pytrace=False)
    if dsn is None:
        pytest.skip(problem or "")
    return dsn


@contextmanager
def rolled_back_connection(dsn: str) -> Iterator[psycopg.Connection]:
    """A connection whose uncommitted work is rolled back on exit."""
    with psycopg.connect(dsn) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def db_conn(db_dsn: str) -> Iterator[psycopg.Connection]:
    """A connection whose uncommitted work is rolled back when the test ends."""
    with rolled_back_connection(db_dsn) as conn:
        yield conn
