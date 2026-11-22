"""Nozdormu's pure parts: the step and changeset model, the checksum, the directory loader."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from caverns_of_time import nozdormu
from caverns_of_time.errors import ChangesetError
from caverns_of_time.nozdormu import Changeset, Step

SQL = "CREATE TABLE t (id int);"


# --- Step validation ---------------------------------------------------------------------


@pytest.mark.parametrize("label", ["", " x", "x ", "a\nb", "a\rb", "\t"])
def test_step_rejects_a_bad_label(label: str) -> None:
    with pytest.raises(ChangesetError, match="step label"):
        Step(label, SQL)


def test_step_rejects_a_label_that_is_not_text() -> None:
    with pytest.raises(ChangesetError, match="not a string"):
        Step(1, SQL)  # type: ignore[arg-type]


@pytest.mark.parametrize("sql", ["", "   \n\t"])
def test_step_rejects_empty_sql(sql: str) -> None:
    with pytest.raises(ChangesetError, match="'x'"):
        Step("x", sql)


@pytest.mark.parametrize("check", ["", " \n"])
def test_step_rejects_an_empty_check(check: str) -> None:
    with pytest.raises(ChangesetError, match="empty check"):
        Step("x", SQL, check)


def test_step_accepts_a_label_with_inner_spaces_and_no_check() -> None:
    step = Step("add the table", SQL)
    assert step.check is None and step.label == "add the table"


# --- checksum ----------------------------------------------------------------------------


def test_checksum_is_the_sha256_of_the_normalised_sql() -> None:
    assert Step("x", "a\nb").checksum == hashlib.sha256(b"a\nb").hexdigest()


def test_checksum_ignores_line_endings_and_outer_whitespace() -> None:
    assert Step("x", "a\r\nb\rc").checksum == Step("y", "\n  a\nb\nc \n\n").checksum


def test_checksum_sees_an_inner_comment_change() -> None:
    assert Step("x", "SELECT 1; -- one").checksum != Step("x", "SELECT 1; -- two").checksum


def test_checksum_ignores_the_label_and_the_check() -> None:
    assert Step("x", SQL).checksum == Step("y", SQL, "SELECT true").checksum


def test_checksum_encodes_as_utf8() -> None:
    assert Step("x", "SELECT 'é'").checksum == hashlib.sha256("SELECT 'é'".encode()).hexdigest()


# --- Step.from_file ----------------------------------------------------------------------


def test_from_file_reads_utf8_and_names_the_step_after_the_file(tmp_path: Path) -> None:
    (tmp_path / "0001_users.sql").write_bytes("SELECT 'é';\r\n".encode())
    step = Step.from_file(tmp_path / "0001_users.sql", check="SELECT true")
    assert (step.label, step.check) == ("0001_users", "SELECT true")
    assert step.checksum == Step("x", "SELECT 'é';").checksum


def test_from_file_accepts_a_label_and_a_string_path(tmp_path: Path) -> None:
    (tmp_path / "a.sql").write_text(SQL, encoding="utf-8")
    assert Step.from_file(str(tmp_path / "a.sql"), label="custom").label == "custom"


def test_from_file_names_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ChangesetError, match="missing.sql"):
        Step.from_file(tmp_path / "missing.sql")


def test_from_file_rejects_text_that_is_not_utf8(tmp_path: Path) -> None:
    (tmp_path / "bad.sql").write_bytes(b"\xff\xfe")
    with pytest.raises(ChangesetError, match="bad.sql"):
        Step.from_file(tmp_path / "bad.sql")


# --- Changeset ---------------------------------------------------------------------------


def test_changeset_keeps_its_steps_as_a_tuple() -> None:
    steps = [Step("a", SQL), Step("b", SQL)]
    changeset = Changeset("app", steps)
    steps.append(Step("c", SQL))
    assert changeset.steps == (Step("a", SQL), Step("b", SQL))


@pytest.mark.parametrize("name", ["", " app", "a\nb"])
def test_changeset_rejects_a_bad_name(name: str) -> None:
    with pytest.raises(ChangesetError, match="changeset name"):
        Changeset(name, [Step("a", SQL)])


def test_changeset_rejects_no_steps() -> None:
    with pytest.raises(ChangesetError, match="no steps"):
        Changeset("app", [])


def test_changeset_rejects_an_item_that_is_not_a_step() -> None:
    with pytest.raises(ChangesetError, match="not a Step"):
        Changeset("app", ["SELECT 1"])  # type: ignore[list-item]


def test_changeset_names_a_duplicate_label() -> None:
    with pytest.raises(ChangesetError, match="'a'"):
        Changeset("app", [Step("a", SQL), Step("b", SQL), Step("a", "SELECT 1")])


def test_the_reserved_name_is_a_valid_changeset_name() -> None:
    # Caverns' own changeset uses it; the public functions refuse it for a consumer's changeset.
    assert Changeset("caverns", [Step("a", SQL)]).name == "caverns"


# --- Changeset.from_directory ------------------------------------------------------------


def _write(directory: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def test_from_directory_orders_steps_by_file_name_and_pairs_checks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "0002_b.sql": "SELECT 2;",
            "0001_a.sql": "SELECT 1;",
            "0001_a.check.sql": "SELECT true",
            "0003_c.sql": "SELECT 3;",
        },
    )
    changeset = Changeset.from_directory("app", tmp_path)
    assert [(s.label, s.check) for s in changeset.steps] == [
        ("0001_a", "SELECT true"),
        ("0002_b", None),
        ("0003_c", None),
    ]
    assert changeset.name == "app"


def test_from_directory_ignores_other_files_and_sub_directories(tmp_path: Path) -> None:
    _write(tmp_path, {"a.sql": "SELECT 1;", "notes.txt": "x", "b.sql.bak": "x", "README.md": "x"})
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.sql").write_text("SELECT 3;", encoding="utf-8")
    (tmp_path / "dir.sql").mkdir()
    assert [s.label for s in Changeset.from_directory("app", tmp_path).steps] == ["a"]


def test_from_directory_refuses_a_check_without_its_step(tmp_path: Path) -> None:
    _write(tmp_path, {"a.sql": "SELECT 1;", "b.check.sql": "SELECT true"})
    with pytest.raises(ChangesetError, match=r"b\.check\.sql"):
        Changeset.from_directory("app", tmp_path)


def test_from_directory_refuses_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    _write(tmp_path, {"a.sql": "SELECT 1;"})
    for path in (tmp_path / "a.sql", tmp_path / "missing"):
        with pytest.raises(ChangesetError, match="not a directory"):
            Changeset.from_directory("app", path)


def test_from_directory_refuses_a_directory_without_steps(tmp_path: Path) -> None:
    _write(tmp_path, {"notes.txt": "x"})
    with pytest.raises(ChangesetError, match="no steps"):
        Changeset.from_directory("app", tmp_path)


def test_from_directory_names_an_empty_step_file(tmp_path: Path) -> None:
    _write(tmp_path, {"a.sql": "  \n"})
    with pytest.raises(ChangesetError, match="'a'"):
        Changeset.from_directory("app", tmp_path)


# --- the SQL constants ---------------------------------------------------------------------


def test_every_sql_constant_names_the_one_ledger_table() -> None:
    constants = {
        name: value for name, value in vars(nozdormu).items() if name.endswith("_SQL") and isinstance(value, str)
    }
    assert constants
    for name, sql in constants.items():
        for qualifier in re.findall(r"(\w+)\.ledger\b", sql):
            assert qualifier == nozdormu._OWN_CHANGESET_NAME, name
