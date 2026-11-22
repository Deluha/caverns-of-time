"""Nozdormu, the migration ledger: the step and changeset model, the ledger and its installation.

Input: a DB-API 2.0 connection (autocommit off) and `Step` / `Changeset` values built from SQL text,
a list or a directory of `.sql` files. Output: validated, immutable values, and ledger rows in the
`caverns.ledger` table of the connected database, written by Caverns itself. The module imports no
database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from caverns_of_time import __version__
from caverns_of_time.errors import CavernsError, ChangesetError

__all__ = ["Changeset", "Connection", "LedgerEntry", "Step", "install"]

_log = logging.getLogger(__name__)

_SQL_SUFFIX = ".sql"
_CHECK_SUFFIX = ".check.sql"
_MILLISECONDS = 1000

# Caverns' own changeset; its name is also the schema that holds the ledger.
_OWN_CHANGESET_NAME = "caverns"
_OUTCOME_APPLIED = "applied"
# One key for every ledger write: Caverns' writers take turns, whatever the changeset.
_ADVISORY_LOCK_KEY = 0x43617665726E73  # "Caverns"


def _validate_name(kind: str, value: object) -> None:
    """A label or changeset name: a non-empty single line without surrounding whitespace."""
    if not isinstance(value, str):
        raise ChangesetError(f"{kind} {value!r} is not a string")
    if not value:
        raise ChangesetError(f"{kind} is empty")
    if value != value.strip():
        raise ChangesetError(f"{kind} {value!r} has surrounding whitespace")
    if "\n" in value or "\r" in value:
        raise ChangesetError(f"{kind} {value!r} spans more than one line")


def _checksum(sql: str) -> str:
    """SHA-256 hex of the SQL as UTF-8 with line endings normalised and the outer whitespace trimmed."""
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ChangesetError(f"cannot read {str(path)!r}: {error}") from error


@dataclass(frozen=True)
class Step:
    """One change: a label, the SQL that makes it and an optional check that it is present.

    A check is SQL returning exactly one row with one boolean column, true when the step's effect
    is already in the database. The checksum covers the SQL only.
    """

    label: str
    sql: str
    check: str | None = None

    def __post_init__(self) -> None:
        _validate_name("step label", self.label)
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ChangesetError(f"step {self.label!r} has no SQL")
        if self.check is not None and (not isinstance(self.check, str) or not self.check.strip()):
            raise ChangesetError(f"step {self.label!r} has an empty check")

    @classmethod
    def from_file(cls, path: str | os.PathLike[str], *, label: str | None = None, check: str | None = None) -> Step:
        """A step whose SQL is the UTF-8 text of *path*; the label defaults to the file name without `.sql`."""
        file = Path(path)
        return cls(label if label is not None else file.stem, _read_text(file), check)

    @property
    def checksum(self) -> str:
        """SHA-256 hex of the normalised SQL."""
        return _checksum(self.sql)


@dataclass(frozen=True)
class Changeset:
    """A named, ordered list of steps with unique labels."""

    name: str
    steps: Sequence[Step]

    def __post_init__(self) -> None:
        _validate_name("changeset name", self.name)
        steps = tuple(self.steps)
        if not steps:
            raise ChangesetError(f"changeset {self.name!r} has no steps")
        for step in steps:
            if not isinstance(step, Step):
                raise ChangesetError(f"changeset {self.name!r} holds {step!r}, which is not a Step")
        labels = [step.label for step in steps]
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        if duplicates:
            raise ChangesetError(f"changeset {self.name!r} repeats the step label {duplicates[0]!r}")
        object.__setattr__(self, "steps", steps)

    @classmethod
    def from_directory(cls, name: str, path: str | os.PathLike[str]) -> Changeset:
        """The `.sql` files of one directory, in file-name order, as steps.

        `<label>.check.sql` is the check of the step `<label>.sql`; other files are ignored and
        sub-directories are not read.
        """
        directory = Path(path)
        if not directory.is_dir():
            raise ChangesetError(f"{str(directory)!r} is not a directory")
        files = sorted(f for f in directory.iterdir() if f.is_file() and f.name.endswith(_SQL_SUFFIX))
        checks = {f.name.removesuffix(_CHECK_SUFFIX): f for f in files if f.name.endswith(_CHECK_SUFFIX)}
        steps = []
        for file in files:
            if file.name.endswith(_CHECK_SUFFIX):
                continue
            check_file = checks.pop(file.name.removesuffix(_SQL_SUFFIX), None)
            steps.append(Step.from_file(file, check=_read_text(check_file) if check_file else None))
        if checks:
            orphan = min(checks.values())
            step_file = orphan.name.removesuffix(_CHECK_SUFFIX) + _SQL_SUFFIX
            raise ChangesetError(f"check file {orphan.name!r} has no step file {step_file!r}")
        return cls(name, steps)


class _Cursor(Protocol):
    """The part of a DB-API 2.0 cursor Caverns uses; psycopg 3 and psycopg2 both fit."""

    def execute(self, query: str, params: Sequence[Any] | None = ..., /) -> object: ...

    def fetchone(self) -> Sequence[Any] | None: ...

    def fetchall(self) -> Sequence[Sequence[Any]]: ...

    def close(self) -> object: ...


class Connection(Protocol):
    """The part of a DB-API 2.0 connection Caverns uses; psycopg 3 and psycopg2 both fit.

    Parameters are `%s` placeholders. Autocommit must be off: Caverns commits and rolls back itself,
    and the caller passes a connection with no uncommitted work.
    """

    @property
    def autocommit(self) -> bool: ...

    def cursor(self) -> _Cursor: ...

    def commit(self) -> object: ...

    def rollback(self) -> object: ...


@dataclass(frozen=True)
class LedgerEntry:
    """One row of `caverns.ledger`, the columns in table order."""

    version: int
    changeset: str
    label: str
    checksum: str
    outcome: str
    fingerprint: str | None
    error: str | None
    duration_ms: int | None
    recorded_at: datetime
    recorded_by: str
    caverns_version: str


# WARNING: this statement shipped once it is released. Never edit it: its checksum is recorded in
# every consumer's ledger. A change to the ledger is a new step of the own changeset (hard rule 6).
_LEDGER_DDL_SQL = """
CREATE SCHEMA IF NOT EXISTS caverns;
CREATE TABLE IF NOT EXISTS caverns.ledger (
    version         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    changeset       text        NOT NULL,
    label           text        NOT NULL,
    checksum        text        NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    outcome         text        NOT NULL CHECK (outcome IN ('applied', 'failed', 'baseline', 'accepted')),
    fingerprint     text        CHECK (outcome <> 'baseline' OR fingerprint IS NOT NULL),
    error           text        CHECK ((outcome = 'failed') = (error IS NOT NULL)),
    duration_ms     integer     CHECK (duration_ms >= 0),
    recorded_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    recorded_by     text        NOT NULL DEFAULT current_user,
    caverns_version text        NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_step_idx ON caverns.ledger (changeset, label, version);
CREATE OR REPLACE FUNCTION caverns.ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'caverns.ledger is append-only: % refused', TG_OP;
END
$$;
CREATE OR REPLACE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON caverns.ledger
    FOR EACH ROW EXECUTE FUNCTION caverns.ledger_append_only();
CREATE OR REPLACE TRIGGER ledger_no_truncate BEFORE TRUNCATE ON caverns.ledger
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.ledger_append_only();
"""
_LEDGER_EXISTS_SQL = "SELECT to_regclass('caverns.ledger') IS NOT NULL"
_OWN_CHANGESET = Changeset(_OWN_CHANGESET_NAME, [Step("ledger", _LEDGER_DDL_SQL, check=_LEDGER_EXISTS_SQL)])

# NOTE: hard rule 3 forbids composing a statement, so `caverns.ledger` is spelled literally in each
# `*_SQL` constant here; a test pins every spelling to `_OWN_CHANGESET_NAME`.
_LEDGER_COLUMNS = ", ".join(field.name for field in fields(LedgerEntry))
_SELECT_ENTRIES_SQL = "SELECT " + _LEDGER_COLUMNS + " FROM caverns.ledger WHERE changeset = %s ORDER BY version"
_INSERT_ENTRY_SQL = (
    "INSERT INTO caverns.ledger (changeset, label, checksum, outcome, fingerprint, error, duration_ms, caverns_version)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING " + _LEDGER_COLUMNS
)
_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s::bigint)"


def _check_connection(connection: Connection) -> None:
    if connection.autocommit:
        raise CavernsError(
            "the connection has autocommit on; Caverns commits and rolls back itself, "
            "so pass a connection with autocommit off"
        )


@contextmanager
def _cursor(connection: Connection) -> Iterator[_Cursor]:
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


@contextmanager
def _transaction(connection: Connection) -> Iterator[_Cursor]:
    """One transaction: committed when the block ends, rolled back when it raises."""
    try:
        with _cursor(connection) as cursor:
            yield cursor
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _lock(cursor: _Cursor) -> None:
    """Take the ledger's advisory lock until the transaction ends."""
    cursor.execute(_LOCK_SQL, (_ADVISORY_LOCK_KEY,))


def _read_entries(cursor: _Cursor, changeset: str) -> list[LedgerEntry]:
    """The changeset's ledger rows, oldest first; none while the ledger does not exist yet."""
    cursor.execute(_LEDGER_EXISTS_SQL)
    row = cursor.fetchone()
    if row is None or not row[0]:
        return []
    cursor.execute(_SELECT_ENTRIES_SQL, (changeset,))
    return [LedgerEntry(*values) for values in cursor.fetchall()]


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * _MILLISECONDS)


def _insert(
    cursor: _Cursor,
    changeset: str,
    step: Step,
    outcome: str,
    *,
    fingerprint: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> LedgerEntry:
    """Append one ledger row for *step* and return it."""
    values = (changeset, step.label, step.checksum, outcome, fingerprint, error, duration_ms, __version__)
    cursor.execute(_INSERT_ENTRY_SQL, values)
    (row,) = cursor.fetchall()
    return LedgerEntry(*row)


def install(connection: Connection) -> list[LedgerEntry]:
    """Create the ledger by applying Caverns' own changeset; the rows written ([] when installed)."""
    _check_connection(connection)
    step = _OWN_CHANGESET.steps[0]
    with _transaction(connection) as cursor:
        _lock(cursor)
        if _read_entries(cursor, _OWN_CHANGESET_NAME):
            return []
        started = time.monotonic()
        cursor.execute(step.sql)
        entry = _insert(cursor, _OWN_CHANGESET_NAME, step, _OUTCOME_APPLIED, duration_ms=_elapsed_ms(started))
    _log.info("installed the ledger: version %d", entry.version)
    return [entry]
