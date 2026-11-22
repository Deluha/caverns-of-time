"""The test instance itself: reachable, the agreed major version, isolated per test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from dev_tools.database import DEFAULT_DSN, IMAGE, POSTGRES_MAJOR
from tests.conftest import (
    DSN_VARIABLE,
    REQUIRE_DB_VARIABLE,
    missing_db_run_problem,
    non_local_target,
    resolve_dsn,
    rolled_back_connection,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEME = "postgresql" + "://"


def test_server_is_the_agreed_major_version(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    assert int(row[0]) // 10000 == POSTGRES_MAJOR


def test_uncommitted_work_is_rolled_back(db_dsn: str) -> None:
    probe = "SELECT to_regclass('caverns_isolation_probe')"
    with rolled_back_connection(db_dsn) as conn:
        conn.execute("CREATE TABLE caverns_isolation_probe (id int)")
        assert conn.execute(probe).fetchone() != (None,)
    with rolled_back_connection(db_dsn) as conn:
        assert conn.execute(probe).fetchone() == (None,)


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


@pytest.mark.parametrize(
    ("suffix", "target"),
    [
        ("@localhost:5432,db.example.com:5432/x", "`db.example.com`"),
        ("@localhost/x?host=db.example.com", "`db.example.com`"),
        ("@localhost/x?hostaddr=10.1.2.3", "`10.1.2.3`"),
        ("@localhost/x?service=prod", "service `prod`"),
        ("@/x", "no named host"),
    ],
)
def test_every_way_to_reach_another_machine_is_refused(suffix: str, target: str) -> None:
    assert non_local_target(SCHEME + "u:p" + suffix) == target


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "localhost,127.0.0.1"])
def test_local_hosts_are_accepted(host: str) -> None:
    assert non_local_target(f"{SCHEME}u:p@{host}/x") is None


def test_a_required_run_without_a_passed_database_test_is_a_problem() -> None:
    assert missing_db_run_problem({REQUIRE_DB_VARIABLE: "1"}, 0) is not None
    assert missing_db_run_problem({REQUIRE_DB_VARIABLE: "1"}, 1) is None
    assert missing_db_run_problem({}, 0) is None


def test_deselecting_the_database_tests_fails_a_required_run() -> None:
    env = {**os.environ, REQUIRE_DB_VARIABLE: "1"}
    env.pop("PYTEST_ADDOPTS", None)
    command = [sys.executable, "-m", "pytest", "-q", "-m", "not db", "-p", "no:cacheprovider", "tests/test_verify.py"]
    run = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert run.returncode == 1 and "no database test passed" in run.stdout
