"""Shared fixtures: the PostgreSQL test connection and its guards (tests/README.md).

The connection string comes from CAVERNS_TEST_DSN and must point at this machine.
Without it a database test is skipped with a notice, unless CAVERNS_REQUIRE_DB=1
(set by dev_tools/verify.py and CI), which turns the skip into a failure.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import psycopg
import pytest

DSN_VARIABLE = "CAVERNS_TEST_DSN"
REQUIRE_DB_VARIABLE = "CAVERNS_REQUIRE_DB"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DB_FIXTURES = {"db_dsn", "db_conn"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that takes a database fixture with `db`."""
    for item in items:
        if DB_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.db)


def resolve_dsn(environ: dict[str, str]) -> tuple[str | None, str | None]:
    """(dsn, problem): the test connection string, or why there is none.

    A problem with a dsn of None is a failure when the database is required and a
    skip otherwise; a non-local host is always a failure (the dsn is returned too).
    """
    dsn = environ.get(DSN_VARIABLE)
    if not dsn:
        return None, f"{DSN_VARIABLE} is not set — database tests did not run (tests/README.md)"
    host = urlsplit(dsn).hostname
    if host not in LOCAL_HOSTS:
        return dsn, f"{DSN_VARIABLE} points at `{host}` — tests connect to this machine only"
    return dsn, None


@pytest.fixture(scope="session")
def db_dsn() -> str:
    """The local test connection string; skips or fails per the module docstring."""
    dsn, problem = resolve_dsn(dict(os.environ))
    if problem and (dsn or os.environ.get(REQUIRE_DB_VARIABLE) == "1"):
        pytest.fail(problem, pytrace=False)
    if dsn is None:
        pytest.skip(problem or "")
    return dsn


@pytest.fixture
def db_conn(db_dsn: str) -> Iterator[psycopg.Connection]:
    """A connection whose work is rolled back when the test ends."""
    with psycopg.connect(db_dsn) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
