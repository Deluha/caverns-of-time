"""Nozdormu's pure parts: the step and changeset model, the checksum, the directory loader."""

from __future__ import annotations

import hashlib
import re
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from caverns_of_time import _connection, _fingerprint, epochs, nozdormu
from caverns_of_time._fingerprint import Fingerprint
from caverns_of_time.errors import ChangesetError
from caverns_of_time.nozdormu import Changeset, LedgerEntry, Status, Step, StepStatus

SQL = "CREATE TABLE t (id int);"


# --- Step validation ---------------------------------------------------------------------


@pytest.mark.parametrize("label", ["", " x", "x ", "a\nb", "a\rb", "a\u2028b", "a\x85b", "\t"])
def test_step_rejects_a_bad_label(label: str) -> None:
    with pytest.raises(ChangesetError, match="step label"):
        Step(label, SQL)


def test_step_rejects_a_label_that_is_not_text() -> None:
    with pytest.raises(ChangesetError, match="not a string"):
        Step(1, SQL)  # type: ignore[arg-type]


def test_step_rejects_sql_that_is_not_text() -> None:
    with pytest.raises(ChangesetError, match="SQL that is not a string"):
        Step("x", 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("sql", ["", "   \n\t"])
def test_step_rejects_empty_sql(sql: str) -> None:
    with pytest.raises(ChangesetError, match="'x'"):
        Step("x", sql)


@pytest.mark.parametrize("check", ["", " \n"])
def test_step_rejects_an_empty_check(check: str) -> None:
    with pytest.raises(ChangesetError, match="empty check"):
        Step("x", SQL, check)


def test_step_rejects_a_check_that_is_not_text() -> None:
    with pytest.raises(ChangesetError, match="check that is not a string"):
        Step("x", SQL, 1)  # type: ignore[arg-type]


def test_step_accepts_a_label_with_inner_spaces_and_no_check() -> None:
    step = Step("add the table", SQL)
    assert step.check is None and step.label == "add the table"


# --- checksum ----------------------------------------------------------------------------


def test_checksum_is_the_sha256_of_the_normalised_sql() -> None:
    assert Step("x", "a\nb").checksum == hashlib.sha256(b"a\nb").hexdigest()


def test_checksum_ignores_line_endings_and_outer_whitespace() -> None:
    assert Step("x", "a\r\nb\rc").checksum == Step("y", "\n  a\nb\nc \n\n").checksum


def test_checksum_trims_ascii_whitespace_only() -> None:
    assert Step("x", " \t\n\r\f\va\f\v \t").checksum == Step("y", "a").checksum
    assert Step("x", "\u00a0a").checksum != Step("y", "a").checksum
    assert Step("x", "a\u00a0").checksum != Step("y", "a").checksum


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


@pytest.mark.parametrize("name", ["", " app", "a\nb", "a\u2029b"])
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


def test_the_ledger_column_lists_are_the_ledger_entry_fields() -> None:
    columns = [field.name for field in fields(LedgerEntry)]
    select = re.fullmatch(r"SELECT (.+?) FROM caverns\.ledger .*", nozdormu._SELECT_ENTRIES_SQL)
    history = re.fullmatch(r"SELECT (.+?) FROM caverns\.ledger .*", nozdormu._HISTORY_SQL)
    returning = re.fullmatch(r".* RETURNING (.+)", nozdormu._INSERT_ENTRY_SQL)
    assert select and history and returning
    assert select.group(1).split(", ") == columns
    assert history.group(1).split(", ") == columns
    assert returning.group(1).split(", ") == columns


def test_the_fingerprint_column_lists_are_the_version_and_the_fingerprint_fields() -> None:
    columns = ["version", *(field.name for field in fields(Fingerprint))]
    select = re.fullmatch(r"SELECT (.+?) FROM caverns\.fingerprint .*", _fingerprint._LATEST_FINGERPRINT_SQL)
    insert = re.fullmatch(r"INSERT INTO caverns\.fingerprint \((.+?)\) VALUES .*", _fingerprint._RECORD_FINGERPRINT_SQL)
    assert select and insert
    assert select.group(1).split(", ") == columns
    assert insert.group(1).split(", ") == columns


@pytest.mark.parametrize(
    "table", ["ledger", "fingerprint", "epoch_table", "epoch_source", "epoch_row", "epoch_deletion", "acknowledgement"]
)
def test_every_sql_constant_names_the_own_tables_in_the_own_schema(table: str) -> None:
    constants = {
        f"{module.__name__}.{name}": value
        for module in (nozdormu, _connection, _fingerprint, epochs)
        for name, value in vars(module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    assert constants
    assert any(f".{table}" in sql for sql in constants.values())
    for name, sql in constants.items():
        for qualifier in re.findall(rf"(\w+)\.{table}\b", sql):
            assert qualifier == nozdormu._OWN_CHANGESET_NAME, name


def test_the_own_changeset_is_the_ledger_fingerprint_epochs_privileges_fingerprint_server_and_acknowledgement() -> None:
    labels = [step.label for step in nozdormu._OWN_CHANGESET.steps]
    assert labels == ["ledger", "fingerprint", "epochs", "privileges", "fingerprint_server", "acknowledgement"]
    assert all(step.check for step in nozdormu._OWN_CHANGESET.steps)


# Each own step's checksum, pinned: it is in every consumer's ledger, so an edit of a statement would read
# as a conflict and `install` would refuse after a release. A new own step adds its line here.
_OWN_STEP_CHECKSUMS = {
    "ledger": "6ccdacf3430bfa341afd4f7f0d7fdba8a76c5ce344b58109387a38ca7ab1251b",
    "fingerprint": "69a4b5b9fe688d80e5089127f8f2c022dcdeb5d1aee13279a7ab0bd62bc8ba31",
    "epochs": "d35779e89163f27f628f153510a5030bac36a37e58ad9bdf59dcb4cb5ecb8b2f",
    "privileges": "e5c174e922f0240159a9531d986e7c6257df73bfa3d53ba61e94dcd149eaab76",
    "fingerprint_server": "83988cb43b79b838e7d58659a1cee8003bcf20e3670b5025bd6f2bc0d8bde5fb",
    "acknowledgement": "2495a898d4ec7122fef1051425ccc8f8567a34a7142a95c80453298b3bc8a0ba",
}


def test_every_own_step_is_byte_identical_to_its_pinned_checksum() -> None:
    assert {step.label: step.checksum for step in nozdormu._OWN_CHANGESET.steps} == _OWN_STEP_CHECKSUMS


def test_the_default_schemas_query_leaves_out_the_own_schema_by_its_name() -> None:
    assert f"'{nozdormu._OWN_CHANGESET_NAME}'" in _fingerprint._DEFAULT_SCHEMAS_SQL


# --- the status decision -----------------------------------------------------------------

A, B, C = Step("a", "SELECT 'a'", "check a"), Step("b", "SELECT 'b'", "check b"), Step("c", "SELECT 'c'")
APP = Changeset("app", [A, B, C])


def _entry(version: int, step: Step, outcome: str = "applied", *, checksum: str | None = None) -> LedgerEntry:
    return LedgerEntry(
        version, "app", step.label, checksum or step.checksum, outcome,
        "f" if outcome == "baseline" else None, "boom" if outcome == "failed" else None,
        None, datetime(2026, 1, 1, tzinfo=UTC), "me", "0",
    )  # fmt: skip


def _decide(entries: list[LedgerEntry], found: set[str] | None = None) -> list[tuple[str, Status, int | None]]:
    """(label, status, version of the deciding row) per step; *found* names the checks that are true."""
    statuses = nozdormu._decide(APP, entries, lambda label, check: label in (found or set()))
    return [(s.step.label, s.status, s.entry.version if s.entry else None) for s in statuses]


def test_an_empty_ledger_leaves_every_step_to_its_check() -> None:
    assert _decide([]) == [("a", Status.PENDING, None), ("b", Status.PENDING, None), ("c", Status.PENDING, None)]


def test_a_check_that_finds_the_effect_makes_the_step_unknown() -> None:
    assert _decide([], {"a", "c"}) == [
        ("a", Status.UNKNOWN, None),
        ("b", Status.PENDING, None),
        ("c", Status.PENDING, None),
    ]


def test_a_matching_row_makes_a_step_applied_and_skips_its_check() -> None:
    def refuse(label: str, check: str) -> bool:
        raise AssertionError(f"the check of {label} must not run")

    statuses = nozdormu._decide(Changeset("app", [A]), [_entry(1, A)], refuse)
    assert [(s.status, s.entry) for s in statuses] == [(Status.APPLIED, _entry(1, A))]


def test_a_row_with_another_checksum_is_a_conflict() -> None:
    assert _decide([_entry(1, A, checksum="0" * 64)])[0] == ("a", Status.CONFLICT, 1)


def test_the_latest_row_decides() -> None:
    old, new = _entry(1, A, checksum="0" * 64), _entry(2, A, "accepted")
    assert _decide([old, new])[0] == ("a", Status.APPLIED, 2)
    assert _decide([new, _entry(3, A, checksum="1" * 64)])[0] == ("a", Status.CONFLICT, 3)


def test_a_failed_row_does_not_count() -> None:
    assert _decide([_entry(1, A, "failed")])[0] == ("a", Status.PENDING, None)
    assert _decide([_entry(1, A), _entry(2, A, "failed")])[0] == ("a", Status.APPLIED, 1)
    assert _decide([_entry(1, A, "failed")], {"a"})[0] == ("a", Status.UNKNOWN, None)


def test_baseline_rows_decide_their_steps_as_applied() -> None:
    assert _decide([_entry(1, A, "baseline"), _entry(2, B, "baseline")]) == [
        ("a", Status.APPLIED, 1),
        ("b", Status.APPLIED, 2),
        ("c", Status.PENDING, None),
    ]


def test_a_step_before_the_baseline_without_a_row_is_before_the_baseline() -> None:
    assert _decide([_entry(1, B, "baseline")]) == [
        ("a", Status.BEFORE_BASELINE, 1),
        ("b", Status.APPLIED, 1),
        ("c", Status.PENDING, None),
    ]


def test_a_step_before_the_baseline_skips_its_check() -> None:
    assert _decide([_entry(1, B, "baseline")], {"a"})[0] == ("a", Status.BEFORE_BASELINE, 1)


def test_a_row_of_a_step_before_the_baseline_wins_over_the_baseline() -> None:
    entries = [_entry(1, B, "baseline"), _entry(2, A, checksum="0" * 64)]
    assert _decide(entries)[0] == ("a", Status.CONFLICT, 2)
    assert _decide([_entry(1, B, "baseline"), _entry(2, A)])[0] == ("a", Status.APPLIED, 2)


def test_a_failed_row_before_the_baseline_does_not_lift_the_status() -> None:
    assert _decide([_entry(1, B, "baseline"), _entry(2, A, "failed")])[0] == ("a", Status.BEFORE_BASELINE, 1)


@pytest.mark.parametrize(
    ("status", "reapply", "accept", "allowed"),
    [
        (Status.CONFLICT, True, False, True),
        (Status.BEFORE_BASELINE, True, False, True),
        (Status.UNKNOWN, True, False, False),
        (Status.PENDING, True, False, False),
        (Status.APPLIED, True, False, False),
        (Status.CONFLICT, False, True, True),
        (Status.UNKNOWN, False, True, True),
        (Status.BEFORE_BASELINE, False, True, True),
        (Status.PENDING, False, True, False),
        (Status.APPLIED, False, True, False),
        (Status.CONFLICT, False, False, False),
        (Status.UNKNOWN, False, False, False),
        (Status.BEFORE_BASELINE, False, False, False),
        (Status.PENDING, False, False, True),
        (Status.APPLIED, False, False, True),
    ],
)
def test_which_statuses_a_run_may_go_on_past(status: Status, reapply: bool, accept: bool, allowed: bool) -> None:
    given = StepStatus(A, status, None)
    reapplied, accepted = frozenset({"a"} if reapply else ()), frozenset({"a"} if accept else ())
    assert nozdormu._acknowledged(given, reapplied, accepted) is allowed


def test_a_baseline_at_a_step_the_changeset_lacks_is_an_error() -> None:
    ghost = Step("ghost", "SELECT 1")
    with pytest.raises(ChangesetError, match="'ghost'"):
        _decide([_entry(1, ghost, "baseline")])


@pytest.mark.parametrize("values", [None, "table"])
def test_a_collection_of_names_is_not_none_or_a_string(values: object) -> None:
    with pytest.raises(ChangesetError, match="must be a collection of names"):
        _connection.names("accept", values)  # type: ignore[arg-type]


def test_an_error_without_a_code_records_the_first_line_of_its_text() -> None:
    assert nozdormu._recorded_error(ValueError("first\nsecond")) == "first"
    assert nozdormu._recorded_error(ValueError("")) == "ValueError"


@pytest.mark.parametrize("value", [None, 0.001, 0.2, 1, 5, 5.0, 2147483, 2147483.647])
def test_a_lock_timeout_that_is_none_or_a_positive_number_is_accepted(value: object) -> None:
    _connection.validate_lock_timeout(value)


@pytest.mark.parametrize(
    "value",
    [
        0,
        0.0,
        -1,
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        "5",
        1e10,
        [5],
        10**400,
        -(10**400),
        1e-9,
        0.0009,
    ],
)
def test_a_lock_timeout_that_is_not_a_positive_number_is_refused(value: object) -> None:
    with pytest.raises(ChangesetError, match="lock_timeout"):
        _connection.validate_lock_timeout(value)


def test_the_default_lock_timeout_is_five_seconds() -> None:
    assert _connection.DEFAULT_LOCK_TIMEOUT == 5.0


class _CodedError(Exception):
    def __init__(self, **attributes: object) -> None:
        super().__init__("coded")
        for name, value in attributes.items():
            setattr(self, name, value)


def test_the_sqlstate_is_read_from_either_drivers_attribute() -> None:
    assert _connection.sqlstate(_CodedError(sqlstate="55P03")) == "55P03"
    assert _connection.sqlstate(_CodedError(pgcode="22012")) == "22012"
    assert _connection.sqlstate(_CodedError(sqlstate=None, pgcode="22012")) == "22012"


@pytest.mark.parametrize("error", [ValueError("x"), _CodedError(sqlstate=""), _CodedError(sqlstate=5)])
def test_an_error_without_a_text_sqlstate_has_none(error: BaseException) -> None:
    assert _connection.sqlstate(error) is None
