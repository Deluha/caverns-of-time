"""The test instance itself: reachable, the agreed major version, isolated per test."""

from __future__ import annotations

from pathlib import Path

import psycopg

from dev_tools.database import DEFAULT_DSN, IMAGE, POSTGRES_MAJOR
from tests.conftest import DSN_VARIABLE, resolve_dsn

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_server_is_the_agreed_major_version(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    assert int(row[0]) // 10000 == POSTGRES_MAJOR


def test_work_is_rolled_back_between_tests_first(db_conn: psycopg.Connection) -> None:
    db_conn.execute("CREATE TABLE caverns_isolation_probe (id int)")
    assert db_conn.execute("SELECT to_regclass('caverns_isolation_probe')").fetchone() != (None,)


def test_work_is_rolled_back_between_tests_second(db_conn: psycopg.Connection) -> None:
    assert db_conn.execute("SELECT to_regclass('caverns_isolation_probe')").fetchone() == (None,)


def test_ci_and_devcontainer_use_the_same_image() -> None:
    for path in (".github/workflows/ci-tests.yml", ".devcontainer/docker-compose.yml"):
        assert f"image: {IMAGE}" in (REPO_ROOT / path).read_text(encoding="utf-8"), path


def test_ci_uses_the_default_connection_string() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci-tests.yml").read_text(encoding="utf-8")
    assert f"CAVERNS_TEST_DSN: {DEFAULT_DSN}" in workflow


def test_default_dsn_is_local() -> None:
    assert resolve_dsn({DSN_VARIABLE: DEFAULT_DSN}) == (DEFAULT_DSN, None)


def test_missing_dsn_is_reported() -> None:
    dsn, problem = resolve_dsn({})
    assert dsn is None and problem is not None and DSN_VARIABLE in problem


def test_remote_dsn_is_refused() -> None:
    remote = DEFAULT_DSN.replace("127.0.0.1", "db.example.com")
    dsn, problem = resolve_dsn({DSN_VARIABLE: remote})
    assert dsn == remote and problem is not None and "db.example.com" in problem
