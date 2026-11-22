"""Tests for dev_tools/check_conventions.py: each rule C1–C9 on synthetic input, then the real repo."""

from __future__ import annotations

from dev_tools import check_conventions as cc

PKG = "caverns_of_time/"
DSN_SCHEME = "postgresql" + "://"


def test_c1_consumer_name_in_package_only() -> None:
    out = cc.check_generic_core([(PKG + "a.py", '"""Doc."""\nX = "Project Acme"\n'), ("tests/t.py", "acme")])
    assert out == [f"{PKG}a.py:2: C1 consumer name `acme` inside the package (R1)"]


def test_c2_print_outside_console_module() -> None:
    sources = [(PKG + "a.py", "print('x')\n"), (PKG + "console.py", "print('x')\n"), ("dev_tools/v.py", "print('x')\n")]
    out = cc.check_no_print(sources)
    assert len(out) == 1 and out[0].startswith(f"{PKG}a.py:1: C2")


def test_c3_formatted_sql() -> None:
    bad = (
        "def f(cur, t):\n"
        "    cur.execute(f'SELECT * FROM {t}')\n"
        "    cur.execute('SELECT * FROM %s' % t)\n"
        "    cur.execute('SELECT * FROM ' + t)\n"
        "    cur.executemany('SELECT {}'.format(t), [])\n"
    )
    good = (
        "def f(cur, q, t, sql):\n"
        "    cur.execute(q)\n"
        "    cur.execute('SELECT %s', (t,))\n"
        "    cur.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(t)))\n"
        "    cur.execute(sql.SQL('DROP TABLE ') + sql.Identifier(t))\n"
    )
    assert [line.split(":")[1] for line in cc.check_safe_sql([(PKG + "a.py", bad)])] == ["2", "3", "4", "5"]
    assert cc.check_safe_sql([(PKG + "a.py", good), ("tests/t.py", bad)]) == []


def test_c4_file_locations() -> None:
    files = [
        "tests/test_a.py", "caverns_of_time/test_b.py", "dev_tools/c_test.py", "dev_tools/test_db.py",
        "README.md", "ROADMAP.md", "caverns_of_time/README.md", ".claude/agents/x.md",
        ".github/pull_request_template.md", "NOTES.md", "docs/plan.md",
    ]  # fmt: skip
    out = cc.check_file_locations(files)
    assert [line.split(":")[0] for line in out] == [
        "caverns_of_time/test_b.py", "dev_tools/c_test.py", "dev_tools/test_db.py", "NOTES.md", "docs/plan.md",
    ]  # fmt: skip


def test_c5_module_docstring() -> None:
    sources = [(PKG + "a.py", "X = 1\n"), (PKG + "b.py", '"""Doc."""\n'), ("dev_tools/c.py", "X = 1\n")]
    assert cc.check_module_docstrings(sources) == [f"{PKG}a.py:1: C5 module docstring missing"]


def test_c6_test_connections() -> None:
    bad = f"import psycopg\nc = psycopg.connect(dsn)\nD = '{DSN_SCHEME}u@h/db'\n"
    out = cc.check_test_connections([("tests/test_a.py", bad), ("tests/conftest.py", bad), (PKG + "a.py", bad)])
    assert [line.split(":")[1] for line in out] == ["2", "3"]
    assert all(line.startswith("tests/test_a.py") for line in out)
    bare = "from psycopg import connect\nc = connect(dsn)\n"
    assert [line.split(":")[1] for line in cc.check_test_connections([("tests/test_b.py", bare)])] == ["1", "2"]


def test_c7_cli_entry() -> None:
    sources = [
        (PKG + "a.py", "import argparse\nfrom click import command\nimport os\n"),
        (PKG + "chromie.py", "import argparse\n"),
        ("dev_tools/v.py", "import argparse\n"),
    ]
    out = cc.check_cli_entry(sources)
    assert [line.split(":")[1] for line in out] == ["1", "2"]


def test_c8_forbidden_tracked() -> None:
    files = [".env", "a/.env.local", "certs/server.pem", "x.key", "pkg.egg-info/PKG-INFO",
             ".claude/settings.local.json", "environment.py", ".claude/settings.json", "keys.py"]  # fmt: skip
    out = cc.check_forbidden_tracked(files)
    assert [line.split(":")[0] for line in out] == files[:6]


def test_c9_test_skips() -> None:
    bad = "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    pytest.skip('x')\n"
    out = cc.check_test_skips([("tests/test_a.py", bad), ("tests/conftest.py", bad)])
    assert [line.split(":")[1] for line in out] == ["2", "4"]
    bare = "import unittest\nfrom pytest import skip\ndef test_a():\n    skip('x')\n    raise unittest.SkipTest\n"
    assert [line.split(":")[1] for line in cc.check_test_skips([("tests/test_b.py", bare)])] == ["2", "4", "5"]


def test_syntax_errors_are_left_to_ruff() -> None:
    assert cc.run_all([], [(PKG + "a.py", "def (:\n"), ("tests/test_a.py", "def (:\n")]) == []


def test_real_repository_is_clean() -> None:
    assert cc.run_all(*cc.repo_inputs()) == []
