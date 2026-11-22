"""Nozdormu, the migration ledger: steps and changesets, the ledger, preflight, apply and baseline.

Input: a DB-API 2.0 connection (autocommit off) and `Step` / `Changeset` values built from SQL text,
a list or a directory of `.sql` files. Output: validated, immutable values, and ledger rows in the
`caverns.ledger` table of the connected database, written by Caverns itself. The module imports no
database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from caverns_of_time import __version__
from caverns_of_time.errors import (
    BaselineRefused,
    CavernsError,
    ChangesetError,
    LedgerNotInstalled,
    PreflightRefused,
    StepFailed,
)

__all__ = [
    "Changeset",
    "Connection",
    "LedgerEntry",
    "Status",
    "Step",
    "StepStatus",
    "apply",
    "baseline",
    "install",
    "preflight",
]

_log = logging.getLogger(__name__)

_ASCII_WHITESPACE = " \t\n\r\f\v"
_SQL_SUFFIX = ".sql"
_CHECK_SUFFIX = ".check.sql"
_MILLISECONDS = 1000

# Caverns' own changeset; its name is also the schema that holds the ledger.
_OWN_CHANGESET_NAME = "caverns"
_OUTCOME_APPLIED = "applied"
_OUTCOME_FAILED = "failed"
_OUTCOME_ACCEPTED = "accepted"
_OUTCOME_BASELINE = "baseline"
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
    """SHA-256 hex of the SQL as UTF-8, line endings normalised and the outer ASCII whitespace trimmed.

    ASCII whitespace is space, tab, LF, CR, FF and VT; any other character, NBSP included, is kept.
    """
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n").strip(_ASCII_WHITESPACE)
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
        if not isinstance(self.sql, str):
            raise ChangesetError(f"step {self.label!r} has SQL that is not a string")
        if not self.sql.strip():
            raise ChangesetError(f"step {self.label!r} has no SQL")
        if self.check is not None:
            if not isinstance(self.check, str):
                raise ChangesetError(f"step {self.label!r} has a check that is not a string")
            if not self.check.strip():
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
    and a connection with uncommitted work is refused.
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


class Status(enum.StrEnum):
    """Where a step stands: recorded as applied, not yet applied, present but unrecorded, or edited."""

    APPLIED = "applied"
    PENDING = "pending"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class StepStatus:
    """A step's status and the ledger row that decided it (None when no row did)."""

    step: Step
    status: Status
    entry: LedgerEntry | None


# WARNING: this statement ships with the release: never edit it once released; its checksum is recorded
# in every consumer's ledger. A change to the ledger is a new step of the own changeset (hard rule 6).
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

# NOTE: hard rule 3 forbids composing a statement, so `caverns.ledger` and the column lists are spelled
# literally in each `*_SQL` constant here; tests pin every schema spelling to `_OWN_CHANGESET_NAME` and
# every column list to the fields of `LedgerEntry`.
_SELECT_ENTRIES_SQL = (
    "SELECT version, changeset, label, checksum, outcome, fingerprint, error, duration_ms,"
    " recorded_at, recorded_by, caverns_version FROM caverns.ledger WHERE changeset = %s ORDER BY version"
)
_INSERT_ENTRY_SQL = (
    "INSERT INTO caverns.ledger (changeset, label, checksum, outcome, fingerprint, error, duration_ms, caverns_version)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING version, changeset, label, checksum, outcome,"
    " fingerprint, error, duration_ms, recorded_at, recorded_by, caverns_version"
)
# False when the connection's open transaction has written something (it holds a transaction id).
_CLEAN_TRANSACTION_SQL = "SELECT pg_current_xact_id_if_assigned() IS NULL"
_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s::bigint)"
# Under REPEATABLE READ or SERIALIZABLE the lock statement would take the snapshot before it waits.
_READ_COMMITTED_SQL = "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"
# The schemas a baseline fingerprints when the caller names none: every one but the system's and Caverns' own.
_DEFAULT_SCHEMAS_SQL = (
    "SELECT nspname::text FROM pg_namespace"
    " WHERE left(nspname, 3) <> 'pg_' AND nspname NOT IN ('information_schema', 'caverns') ORDER BY nspname"
)
_EXISTING_SCHEMAS_SQL = "SELECT nspname::text FROM pg_namespace WHERE nspname = ANY(%s::text[])"
# A LEFT JOIN keeps a table without columns in the result.
_TABLE_COLUMNS_SQL = """
SELECT n.nspname::text, c.relname::text, c.relkind::text, a.attname::text,
       format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s::text[])
ORDER BY n.nspname, c.relname, a.attnum
"""


def _check_connection(connection: Connection) -> None:
    """Refuse a connection with autocommit on or with uncommitted writes; leave a clean one with no open transaction."""
    if connection.autocommit:
        raise CavernsError(
            "the connection has autocommit on; Caverns commits and rolls back itself, "
            "so pass a connection with autocommit off"
        )
    with _cursor(connection) as cursor:
        cursor.execute(_CLEAN_TRANSACTION_SQL)
        row = cursor.fetchone()
        if row is None or not row[0]:
            # No rollback: the caller's work must survive the refusal.
            raise CavernsError("the connection has uncommitted work; commit or roll it back before calling Caverns")
    connection.rollback()  # only reads so far: nothing is lost


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
            # The ledger is re-read after the lock: that read needs a snapshot taken after the wait.
            cursor.execute(_READ_COMMITTED_SQL)
            yield cursor
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


@contextmanager
def _read_only(connection: Connection) -> Iterator[_Cursor]:
    """A read-only transaction that is always rolled back."""
    try:
        with _cursor(connection) as cursor:
            cursor.execute(_READ_ONLY_SQL)
            yield cursor
    finally:
        connection.rollback()


def _reject_reserved(changeset: Changeset) -> None:
    if changeset.name == _OWN_CHANGESET_NAME:
        raise ChangesetError(f"the changeset name {changeset.name!r} is reserved for Caverns' own changeset")


def _lock(cursor: _Cursor) -> None:
    """Take the ledger's advisory lock until the transaction ends."""
    cursor.execute(_LOCK_SQL, (_ADVISORY_LOCK_KEY,))


def _ledger_exists(cursor: _Cursor) -> bool:
    cursor.execute(_LEDGER_EXISTS_SQL)
    row = cursor.fetchone()
    return row is not None and bool(row[0])


def _read_entries(cursor: _Cursor, changeset: str) -> list[LedgerEntry]:
    """The changeset's ledger rows, oldest first; none while the ledger does not exist yet."""
    if not _ledger_exists(cursor):
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


def _baseline_position(changeset: Changeset, entries: Sequence[LedgerEntry]) -> tuple[LedgerEntry, int] | None:
    """The changeset's baseline row and the index of its step, or None when it has no baseline."""
    baseline = next((entry for entry in reversed(entries) if entry.outcome == _OUTCOME_BASELINE), None)
    if baseline is None:
        return None
    labels = [step.label for step in changeset.steps]
    if baseline.label not in labels:
        raise ChangesetError(
            f"the baseline of changeset {changeset.name!r} is at step {baseline.label!r}, which the changeset lacks"
        )
    return baseline, labels.index(baseline.label)


def _ledger_decision(
    changeset: Changeset, index: int, entries: Sequence[LedgerEntry], baseline: tuple[LedgerEntry, int] | None
) -> tuple[Status, LedgerEntry] | None:
    """What the ledger alone says of the step at *index*, or None when only its check can tell.

    The latest row that is not a failure decides: the same checksum is applied, another is a conflict.
    Without one, a step at or before the *baseline*'s step (see `_baseline_position`) is applied.
    """
    step = changeset.steps[index]
    latest = next((e for e in reversed(entries) if e.label == step.label and e.outcome != _OUTCOME_FAILED), None)
    if latest is not None:
        return (Status.APPLIED if latest.checksum == step.checksum else Status.CONFLICT), latest
    if baseline is not None and index <= baseline[1]:
        return Status.APPLIED, baseline[0]
    return None


def _decide(
    changeset: Changeset, entries: Sequence[LedgerEntry], run_check: Callable[[str, str], bool]
) -> list[StepStatus]:
    """The status of every step: the ledger first, then the step's check, run only when the ledger is silent.

    *entries* are the changeset's ledger rows, oldest first; *run_check* takes a step's label and check
    SQL and says whether the check found the step's effect.
    """
    baseline = _baseline_position(changeset, entries)
    statuses = []
    for index, step in enumerate(changeset.steps):
        decision = _ledger_decision(changeset, index, entries, baseline)
        if decision is not None:
            statuses.append(StepStatus(step, *decision))
        elif step.check is not None and run_check(step.label, step.check):
            statuses.append(StepStatus(step, Status.UNKNOWN, None))
        else:
            statuses.append(StepStatus(step, Status.PENDING, None))
    return statuses


def _run_check(cursor: _Cursor, label: str, check: str) -> bool:
    """Run a step's check; it must return exactly one row with one boolean column."""
    try:
        cursor.execute(check)
        rows = cursor.fetchall()
    except Exception as error:
        raise ChangesetError(f"the check of step {label!r} failed: {error}") from error
    if len(rows) == 1 and len(rows[0]) == 1 and isinstance(rows[0][0], bool):
        return rows[0][0]
    raise ChangesetError(f"the check of step {label!r} must return one row with one boolean column")


def _statuses(cursor: _Cursor, changeset: Changeset) -> list[StepStatus]:
    entries = _read_entries(cursor, changeset.name)
    return _decide(changeset, entries, lambda label, check: _run_check(cursor, label, check))


def _require_installed(cursor: _Cursor) -> None:
    own = _statuses(cursor, _OWN_CHANGESET)
    if any(status.status is not Status.APPLIED for status in own):
        raise LedgerNotInstalled("the ledger is not installed (Caverns' own changeset is not applied): call install()")


def preflight(connection: Connection, changeset: Changeset) -> list[StepStatus]:
    """The status of every step of *changeset*, in changeset order; writes nothing.

    Raises `LedgerNotInstalled` before `install()` and `ChangesetError` for a reserved name, a broken
    check or a baseline at a step the changeset lacks.
    """
    _check_connection(connection)
    _reject_reserved(changeset)
    with _read_only(connection) as cursor:
        _require_installed(cursor)
        return _statuses(cursor, changeset)


def _names(argument: str, values: Collection[str]) -> frozenset[str]:
    """*values* as a set; a bare string is refused because it would be read as its characters."""
    if isinstance(values, str):
        raise ChangesetError(f"{argument} must be a collection of names, not the string {values!r}")
    return frozenset(values)


def _acknowledged(status: StepStatus, reapply: frozenset[str], accept: frozenset[str]) -> bool:
    """Whether the run may go on past this step as the caller asked."""
    label, current = status.step.label, status.status
    if label in reapply:
        return current is Status.CONFLICT
    if label in accept:
        return current in (Status.CONFLICT, Status.UNKNOWN)
    return current not in (Status.CONFLICT, Status.UNKNOWN)


def _plan(
    changeset: Changeset, statuses: Sequence[StepStatus], reapply: frozenset[str], accept: frozenset[str]
) -> list[tuple[StepStatus, str]]:
    """The steps a run writes, in order, each with the outcome to record; refuses before any is written."""
    missing = sorted((reapply | accept) - {step.label for step in changeset.steps})
    if missing:
        raise ChangesetError(f"changeset {changeset.name!r} has no step {missing[0]!r} to reapply or accept")
    both = sorted(reapply & accept)
    if both:
        raise ChangesetError(f"step {both[0]!r} is in both reapply and accept")
    refused = [status for status in statuses if not _acknowledged(status, reapply, accept)]
    if refused:
        raise PreflightRefused(refused)
    plan = []
    for status in statuses:
        if status.step.label in accept:
            plan.append((status, _OUTCOME_ACCEPTED))
        elif status.step.label in reapply or status.status is Status.PENDING:
            plan.append((status, _OUTCOME_APPLIED))
    return plan


def _recheck(changeset: Changeset, status: StepStatus, entries: Sequence[LedgerEntry]) -> StepStatus:
    """The step's status as the ledger says it now; where the ledger is silent the earlier status stands."""
    index = changeset.steps.index(status.step)
    decision = _ledger_decision(changeset, index, entries, _baseline_position(changeset, entries))
    return status if decision is None else StepStatus(status.step, *decision)


def _record_failure(
    connection: Connection, changeset: Changeset, step: Step, error: Exception, duration_ms: int
) -> StepFailed:
    """Record a failed step in a transaction of its own; the error to raise.

    Raises `CavernsError` from *error* when the ledger does not exist: the failed step was its own.
    """
    with _transaction(connection) as cursor:
        _lock(cursor)
        if not _ledger_exists(cursor):
            raise CavernsError(
                f"step {step.label!r} of changeset {changeset.name!r} failed and could not be recorded: "
                f"the ledger does not exist: {error}"
            ) from error
        entry = _insert(cursor, changeset.name, step, _OUTCOME_FAILED, error=str(error), duration_ms=duration_ms)
    _log.warning("step %s of changeset %s failed: ledger version %d", step.label, changeset.name, entry.version)
    return StepFailed(step.label, entry.version)


def _execute_step(connection: Connection, changeset: Changeset, cursor: _Cursor, step: Step) -> int:
    """Run the step's SQL as written; its duration in milliseconds.

    A failure rolls the transaction back, records a `failed` row and raises `StepFailed`; when the
    ledger does not exist, nothing is recorded and `CavernsError` is raised from the database error.
    """
    started = time.monotonic()
    try:
        cursor.execute(step.sql)
    except Exception as error:
        connection.rollback()
        raise _record_failure(connection, changeset, step, error, _elapsed_ms(started)) from error
    return _elapsed_ms(started)


def _write_step(connection: Connection, changeset: Changeset, status: StepStatus, outcome: str) -> LedgerEntry | None:
    """Run or accept one step in one transaction; None when another runner applied it first.

    The ledger is read again under the lock: a status that changed since preflight (an `applied`
    one means the work is done) must not be acted on.
    """
    step = status.step
    with _transaction(connection) as cursor:
        _lock(cursor)
        current = _recheck(changeset, status, _read_entries(cursor, changeset.name))
        if current.status is Status.APPLIED:
            return None
        if current.status is not status.status:
            raise PreflightRefused([current])
        duration = _execute_step(connection, changeset, cursor, step) if outcome == _OUTCOME_APPLIED else None
        return _insert(cursor, changeset.name, step, outcome, duration_ms=duration)


def _apply_statuses(
    connection: Connection,
    changeset: Changeset,
    statuses: Sequence[StepStatus],
    reapply: frozenset[str],
    accept: frozenset[str],
) -> list[LedgerEntry]:
    """Walk the planned steps in order, each in its own transaction; the rows written."""
    written = []
    for status, outcome in _plan(changeset, statuses, reapply, accept):
        entry = _write_step(connection, changeset, status, outcome)
        if entry is not None:
            _log.info(
                "%s step %s of changeset %s: ledger version %d", outcome, entry.label, entry.changeset, entry.version
            )
            written.append(entry)
    return written


def install(connection: Connection) -> list[LedgerEntry]:
    """Create the ledger by applying Caverns' own changeset; the rows written ([] when installed)."""
    _check_connection(connection)
    with _read_only(connection) as cursor:
        statuses = _statuses(cursor, _OWN_CHANGESET)
    return _apply_statuses(connection, _OWN_CHANGESET, statuses, frozenset(), frozenset())


def apply(
    connection: Connection,
    changeset: Changeset,
    *,
    reapply: Collection[str] = (),
    accept: Collection[str] = (),
) -> list[LedgerEntry]:
    """Apply the steps of *changeset* that are pending, one transaction and one ledger row each.

    A step already applied is skipped. A `conflict` or `unknown` step refuses the whole run
    (`PreflightRefused`, nothing written) unless named in *reapply* (conflict only: run the edited
    SQL, record a new `applied` row) or *accept* (conflict or unknown: record `accepted` without
    running). A step that fails is rolled back, recorded as `failed` and raised as `StepFailed`;
    later steps do not run. Returns the rows written, in order.
    """
    reapply_names, accept_names = _names("reapply", reapply), _names("accept", accept)
    statuses = preflight(connection, changeset)
    return _apply_statuses(connection, changeset, statuses, reapply_names, accept_names)


def _baseline_step(changeset: Changeset, through: str | None) -> Step:
    """The step a baseline stands at: *through*, by default the last."""
    if through is None:
        return changeset.steps[-1]
    for step in changeset.steps:
        if step.label == through:
            return step
    raise ChangesetError(f"changeset {changeset.name!r} has no step {through!r} to baseline through")


def _resolve_schemas(cursor: _Cursor, schemas: list[str] | None) -> list[str]:
    """The schemas to fingerprint: the given ones, which must exist, else every non-system one."""
    if schemas is None:
        cursor.execute(_DEFAULT_SCHEMAS_SQL)
        return [row[0] for row in cursor.fetchall()]
    cursor.execute(_EXISTING_SCHEMAS_SQL, (schemas,))
    missing = sorted(set(schemas) - {row[0] for row in cursor.fetchall()})
    if missing:
        raise BaselineRefused(f"the schema {missing[0]!r} does not exist")
    return schemas


def _read_document(cursor: _Cursor, schemas: list[str]) -> dict[str, Any]:
    """The tables and columns of *schemas* as a canonical document, read from the system catalogs."""
    cursor.execute(_TABLE_COLUMNS_SQL, (schemas,))
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    for schema, name, kind, column, column_type, not_null, default in cursor.fetchall():
        table = tables.setdefault((schema, name), {"schema": schema, "name": name, "kind": kind, "columns": []})
        if column is not None:
            table["columns"].append({"name": column, "type": column_type, "not_null": not_null, "default": default})
    return {"tables": [tables[key] for key in sorted(tables)]}


def _hash_document(document: dict[str, Any]) -> str:
    """SHA-256 hex of the document's canonical JSON."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def baseline(
    connection: Connection,
    changeset: Changeset,
    *,
    through: str | None = None,
    schemas: Collection[str] | None = None,
) -> LedgerEntry:
    """Record an existing database as the starting point of *changeset*; nothing is executed.

    One `baseline` row for the step *through* (default the last) carries that step's checksum and a
    fingerprint of the tables and columns in *schemas* (default: every schema but `pg_*`,
    `information_schema` and `caverns`). Afterwards every step up to *through* is `applied`. Refused
    (`BaselineRefused`) when the changeset already has ledger rows or a named schema does not exist.
    """
    _check_connection(connection)
    _reject_reserved(changeset)
    step = _baseline_step(changeset, through)
    names = None if schemas is None else sorted(_names("schemas", schemas))
    if names is not None and not names:
        raise ChangesetError("schemas is empty; name at least one schema or leave it out")
    with _read_only(connection) as cursor:
        _require_installed(cursor)
    with _transaction(connection) as cursor:
        _lock(cursor)
        if _read_entries(cursor, changeset.name):
            raise BaselineRefused(f"changeset {changeset.name!r} already has ledger rows")
        fingerprint = _hash_document(_read_document(cursor, _resolve_schemas(cursor, names)))
        entry = _insert(cursor, changeset.name, step, _OUTCOME_BASELINE, fingerprint=fingerprint)
    _log.info("baseline of changeset %s at step %s: ledger version %d", entry.changeset, entry.label, entry.version)
    return entry
