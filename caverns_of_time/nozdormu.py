"""Nozdormu, the migration ledger: steps and changesets, the ledger, preflight, apply and baseline.

Input: a DB-API 2.0 connection (autocommit off) and `Step` / `Changeset` values built from SQL text,
a list or a directory of `.sql` files. Output: validated, immutable values, and ledger rows in the
`caverns.ledger` table of the connected database, written by Caverns itself. The module imports no
database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from caverns_of_time import __version__
from caverns_of_time._connection import (
    DEFAULT_LOCK_TIMEOUT,
    EPOCHS_EXISTS_SQL,
    Connection,
    Cursor,
    check_connection,
    lock,
    names,
    read_only,
    run_check,
    sqlstate,
    transaction,
    validate_limit,
    validate_lock_timeout,
    validate_name,
)
from caverns_of_time._fingerprint import (
    FINGERPRINT_EXISTS_SQL,
    capture,
    compare,
    compared_schemas,
    default_schemas,
    named_schemas,
    read_document,
    record,
    resolve_schemas,
    restrict,
    watched_schemas,
)
from caverns_of_time._fingerprint import latest as latest_fingerprint
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
    "history",
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
        validate_name("step label", self.label)
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
        validate_name("changeset name", self.name)
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
    """Where a step stands: applied, pending, present but unrecorded, edited, or added before the baseline."""

    APPLIED = "applied"
    PENDING = "pending"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    BEFORE_BASELINE = "before_baseline"


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
# WARNING: this statement ships with the release: never edit it once released; its checksum is recorded
# in every consumer's ledger. A change to the fingerprint table is a new step of the own changeset (hard rule 6).
_FINGERPRINT_DDL_SQL = """
CREATE TABLE IF NOT EXISTS caverns.fingerprint (
    version     bigint      PRIMARY KEY REFERENCES caverns.ledger (version),
    schemas     text[]      NOT NULL,
    document    jsonb       NOT NULL,
    hash        text        NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION caverns.fingerprint_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'caverns.fingerprint is append-only: % refused', TG_OP;
END
$$;
CREATE OR REPLACE TRIGGER fingerprint_no_update_delete BEFORE UPDATE OR DELETE ON caverns.fingerprint
    FOR EACH ROW EXECUTE FUNCTION caverns.fingerprint_append_only();
CREATE OR REPLACE TRIGGER fingerprint_no_truncate BEFORE TRUNCATE ON caverns.fingerprint
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.fingerprint_append_only();
"""
# WARNING: this statement ships with the release: never edit it once released; its checksum is recorded
# in every consumer's ledger. A change to the epoch tables is a new step of the own changeset (hard rule 6).
_EPOCHS_DDL_SQL = """
CREATE TABLE IF NOT EXISTS caverns.epoch_table (
    schema_name   text        NOT NULL,
    table_name    text        NOT NULL,
    key           text[]      NOT NULL CHECK (cardinality(key) > 0),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    registered_by text        NOT NULL DEFAULT current_user,
    PRIMARY KEY (schema_name, table_name)
);
CREATE TABLE IF NOT EXISTS caverns.epoch (
    epoch           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text        NOT NULL UNIQUE,
    ledger_version  bigint      NOT NULL REFERENCES caverns.ledger (version),
    taken_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    taken_by        text        NOT NULL DEFAULT current_user,
    caverns_version text        NOT NULL
);
CREATE TABLE IF NOT EXISTS caverns.epoch_source (
    epoch       bigint NOT NULL REFERENCES caverns.epoch (epoch),
    schema_name text   NOT NULL,
    table_name  text   NOT NULL,
    key         text[] NOT NULL CHECK (cardinality(key) > 0),
    columns     jsonb  NOT NULL,
    row_count   bigint NOT NULL CHECK (row_count >= 0),
    PRIMARY KEY (epoch, schema_name, table_name)
);
CREATE TABLE IF NOT EXISTS caverns.epoch_row (
    epoch       bigint NOT NULL,
    schema_name text   NOT NULL,
    table_name  text   NOT NULL,
    key         jsonb  NOT NULL,
    row         jsonb  NOT NULL,
    PRIMARY KEY (epoch, schema_name, table_name, key),
    FOREIGN KEY (epoch, schema_name, table_name)
        REFERENCES caverns.epoch_source (epoch, schema_name, table_name) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS caverns.epoch_deletion (
    epoch           bigint      PRIMARY KEY REFERENCES caverns.epoch (epoch),
    deleted_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    deleted_by      text        NOT NULL DEFAULT current_user,
    caverns_version text        NOT NULL
);
CREATE OR REPLACE FUNCTION caverns.epoch_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'caverns.% is append-only: % refused', TG_TABLE_NAME, TG_OP;
END
$$;
CREATE OR REPLACE FUNCTION caverns.epoch_row_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND EXISTS (SELECT 1 FROM caverns.epoch_deletion d WHERE d.epoch = OLD.epoch) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'caverns.epoch_row keeps an epoch''s rows until the epoch is deleted: % refused', TG_OP;
END
$$;
CREATE OR REPLACE TRIGGER epoch_no_update_delete BEFORE UPDATE OR DELETE ON caverns.epoch
    FOR EACH ROW EXECUTE FUNCTION caverns.epoch_append_only();
CREATE OR REPLACE TRIGGER epoch_no_truncate BEFORE TRUNCATE ON caverns.epoch
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.epoch_append_only();
CREATE OR REPLACE TRIGGER epoch_source_no_update_delete BEFORE UPDATE OR DELETE ON caverns.epoch_source
    FOR EACH ROW EXECUTE FUNCTION caverns.epoch_append_only();
CREATE OR REPLACE TRIGGER epoch_source_no_truncate BEFORE TRUNCATE ON caverns.epoch_source
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.epoch_append_only();
CREATE OR REPLACE TRIGGER epoch_deletion_no_update_delete BEFORE UPDATE OR DELETE ON caverns.epoch_deletion
    FOR EACH ROW EXECUTE FUNCTION caverns.epoch_append_only();
CREATE OR REPLACE TRIGGER epoch_deletion_no_truncate BEFORE TRUNCATE ON caverns.epoch_deletion
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.epoch_append_only();
CREATE OR REPLACE TRIGGER epoch_row_guard_update_delete BEFORE UPDATE OR DELETE ON caverns.epoch_row
    FOR EACH ROW EXECUTE FUNCTION caverns.epoch_row_guard();
CREATE OR REPLACE TRIGGER epoch_row_no_truncate BEFORE TRUNCATE ON caverns.epoch_row
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.epoch_append_only();
"""
# WARNING: this statement ships with the release: never edit it once released; its checksum is recorded
# in every consumer's ledger. A change to the grants is a new step of the own changeset (hard rule 6).
_PRIVILEGES_DDL_SQL = """
REVOKE ALL ON SCHEMA caverns FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA caverns FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA caverns FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA caverns FROM PUBLIC;
"""
# True only when PUBLIC holds no privilege on the schema, a relation or a function of it; false (never an error)
# while the schema or the last table of the earlier own steps does not exist (an empty schema holds nothing
# to revoke yet, and the step must still run).
_PRIVILEGES_REVOKED_SQL = """
SELECT to_regnamespace('caverns') IS NOT NULL AND to_regclass('caverns.epoch_deletion') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a WHERE n.nspname = 'caverns' AND a.grantee = 0)
   AND NOT EXISTS (
       SELECT 1 FROM pg_class c, aclexplode(c.relacl) a
       WHERE c.relnamespace = to_regnamespace('caverns') AND a.grantee = 0)
   AND NOT EXISTS (
       SELECT 1 FROM pg_proc p
       WHERE p.pronamespace = to_regnamespace('caverns') AND has_function_privilege('public', p.oid, 'EXECUTE'))
"""
# The first object of the own schema (the schema, then its relations by name) whose owner the current role is not a
# member of: what it is called, its owner and the current role. No row when every owner is the role or one of its roles.
_FOREIGN_OWNER_SQL = """
SELECT description, owner, current_user::text FROM (
    SELECT 0 AS rank, 'schema caverns' AS description, n.nspowner AS owner_oid,
           pg_get_userbyid(n.nspowner)::text AS owner
    FROM pg_namespace n WHERE n.nspname = 'caverns'
    UNION ALL
    SELECT 1, 'relation caverns.' || c.relname, c.relowner, pg_get_userbyid(c.relowner)::text
    FROM pg_class c WHERE c.relnamespace = to_regnamespace('caverns')
) objects
WHERE NOT pg_has_role(current_user, owner_oid, 'MEMBER')
ORDER BY rank, description
LIMIT 1
"""
_OWN_CHANGESET = Changeset(
    _OWN_CHANGESET_NAME,
    [
        Step("ledger", _LEDGER_DDL_SQL, check=_LEDGER_EXISTS_SQL),
        Step("fingerprint", _FINGERPRINT_DDL_SQL, check=FINGERPRINT_EXISTS_SQL),
        Step("epochs", _EPOCHS_DDL_SQL, check=EPOCHS_EXISTS_SQL),
        Step("privileges", _PRIVILEGES_DDL_SQL, check=_PRIVILEGES_REVOKED_SQL),
    ],
)

# NOTE: hard rule 3 forbids composing a statement, so `caverns.ledger` and the column lists are spelled
# literally in each `*_SQL` constant here; tests pin every schema spelling to `_OWN_CHANGESET_NAME` and
# every column list to the fields of `LedgerEntry`.
_SELECT_ENTRIES_SQL = (
    "SELECT version, changeset, label, checksum, outcome, fingerprint, error, duration_ms,"
    " recorded_at, recorded_by, caverns_version FROM caverns.ledger WHERE changeset = %s ORDER BY version"
)
_HISTORY_SQL = (
    "SELECT version, changeset, label, checksum, outcome, fingerprint, error, duration_ms,"
    " recorded_at, recorded_by, caverns_version FROM caverns.ledger"
    " WHERE (%s::text IS NULL OR changeset = %s) ORDER BY version DESC LIMIT %s"
)
_INSERT_ENTRY_SQL = (
    "INSERT INTO caverns.ledger (changeset, label, checksum, outcome, fingerprint, error, duration_ms, caverns_version)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING version, changeset, label, checksum, outcome,"
    " fingerprint, error, duration_ms, recorded_at, recorded_by, caverns_version"
)


def _reject_reserved(changeset: Changeset) -> None:
    if changeset.name == _OWN_CHANGESET_NAME:
        raise ChangesetError(f"the changeset name {changeset.name!r} is reserved for Caverns' own changeset")


def _ledger_exists(cursor: Cursor) -> bool:
    cursor.execute(_LEDGER_EXISTS_SQL)
    row = cursor.fetchone()
    return row is not None and bool(row[0])


def _read_entries(cursor: Cursor, changeset: str) -> list[LedgerEntry]:
    """The changeset's ledger rows, oldest first; none while the ledger does not exist yet."""
    if not _ledger_exists(cursor):
        return []
    cursor.execute(_SELECT_ENTRIES_SQL, (changeset,))
    return [LedgerEntry(*values) for values in cursor.fetchall()]


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * _MILLISECONDS)


def _insert(
    cursor: Cursor,
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

    The latest row that is not a failure decides (a `baseline` row included): the same checksum is applied,
    another is a conflict. Without one, a step at or before the *baseline*'s step (see `_baseline_position`)
    was added after the baseline was taken: `before_baseline`, decided by the baseline row, its check not run.
    """
    step = changeset.steps[index]
    latest = next((e for e in reversed(entries) if e.label == step.label and e.outcome != _OUTCOME_FAILED), None)
    if latest is not None:
        return (Status.APPLIED if latest.checksum == step.checksum else Status.CONFLICT), latest
    if baseline is not None and index <= baseline[1]:
        return Status.BEFORE_BASELINE, baseline[0]
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


def _statuses(cursor: Cursor, changeset: Changeset) -> list[StepStatus]:
    entries = _read_entries(cursor, changeset.name)
    return _decide(changeset, entries, lambda label, check: run_check(cursor, label, check))


def _require_installed(cursor: Cursor) -> None:
    own = _statuses(cursor, _OWN_CHANGESET)
    if any(status.status is not Status.APPLIED for status in own):
        raise LedgerNotInstalled("the ledger is not installed (Caverns' own changeset is not applied): call install()")


def preflight(connection: Connection, changeset: Changeset) -> list[StepStatus]:
    """The status of every step of *changeset*, in changeset order; writes nothing.

    Raises `LedgerNotInstalled` before `install()` and `ChangesetError` for a reserved name, a broken
    check or a baseline at a step the changeset lacks.
    """
    check_connection(connection)
    _reject_reserved(changeset)
    with read_only(connection) as cursor:
        _require_installed(cursor)
        return _statuses(cursor, changeset)


def history(connection: Connection, *, changeset: str | None = None, limit: int | None = None) -> list[LedgerEntry]:
    """The ledger rows, newest first: only *changeset*'s when given, at most *limit* (None: all); writes nothing.

    Raises `LedgerNotInstalled` before `install()` and `ChangesetError` for an invalid *changeset* or a
    *limit* that is not a positive integer up to 9223372036854775807.
    """
    check_connection(connection)
    if changeset is not None:
        validate_name("changeset name", changeset)
    validate_limit(limit)
    with read_only(connection) as cursor:
        if not _ledger_exists(cursor):
            raise LedgerNotInstalled("the ledger is not installed (caverns.ledger does not exist): call install()")
        cursor.execute(_HISTORY_SQL, (changeset, changeset, limit))
        return [LedgerEntry(*values) for values in cursor.fetchall()]


# The statuses that refuse a run until the caller names the step in `accept` (or, bar `unknown`, `reapply`).
_ACKNOWLEDGEABLE = (Status.CONFLICT, Status.UNKNOWN, Status.BEFORE_BASELINE)


def _acknowledged(status: StepStatus, reapply: frozenset[str], accept: frozenset[str]) -> bool:
    """Whether the run may go on past this step as the caller asked."""
    label, current = status.step.label, status.status
    if label in reapply:
        return current in (Status.CONFLICT, Status.BEFORE_BASELINE)
    if label in accept:
        return current in _ACKNOWLEDGEABLE
    return current not in _ACKNOWLEDGEABLE


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


def _recorded_error(error: Exception) -> str:
    """What a `failed` row keeps of *error*: `<SQLSTATE>: <primary message>`, never DETAIL, HINT or CONTEXT.

    Read by duck typing (psycopg 3 `sqlstate`, psycopg2 `pgcode`, both `diag.message_primary`) so that no driver is
    imported; an error without them keeps the first line of its text, else its class name.
    """
    code = sqlstate(error)
    primary = getattr(getattr(error, "diag", None), "message_primary", None)
    if code and primary and isinstance(primary, str):
        return f"{code}: {primary}"
    lines = str(error).strip().splitlines()
    return lines[0].strip() if lines and lines[0].strip() else type(error).__name__


def _record_failure(
    connection: Connection,
    changeset: Changeset,
    step: Step,
    error: Exception,
    duration_ms: int,
    *,
    record_fingerprint: bool,
    lock_timeout: float | None,
) -> StepFailed:
    """Record a failed step in a transaction of its own; the error to raise.

    With *record_fingerprint* the transaction also stores the fingerprint of the database as the steps
    before the failed one left it. Raises `CavernsError` from *error* when the failure cannot be recorded:
    the ledger does not exist (the failed step was its own) or writing the row fails (a read-only
    session). The transaction is rolled back either way, so no fingerprint is recorded then.
    """
    try:
        with transaction(connection) as cursor:
            lock(cursor, lock_timeout=lock_timeout)
            if not _ledger_exists(cursor):
                raise CavernsError("the ledger does not exist")
            entry = _insert(
                cursor, changeset.name, step, _OUTCOME_FAILED, error=_recorded_error(error), duration_ms=duration_ms
            )
            if record_fingerprint:
                _record_fingerprint(cursor, entry)
    except Exception as record_error:
        raise CavernsError(
            f"step {step.label!r} of changeset {changeset.name!r} failed and could not be recorded: {record_error}"
        ) from error
    _log.warning("step %s of changeset %s failed: ledger version %d", step.label, changeset.name, entry.version)
    return StepFailed(step.label, entry.version)


def _record_fingerprint(cursor: Cursor, entry: LedgerEntry) -> None:
    """Store the fingerprint of the watched schemas, as this transaction sees them, at *entry*'s version."""
    record(cursor, entry.version, capture(cursor, watched_schemas(cursor)))


def _warn_of_drift(connection: Connection, changeset: Changeset) -> None:
    """Log one warning per difference between the latest recorded fingerprint and the live database; no refusal.

    Compares the watched schemas plus those the fingerprint covers, so a schema dropped since reads as removed.
    """
    with read_only(connection) as cursor:
        found = latest_fingerprint(cursor)
        if found is None:
            return
        compared = compared_schemas(cursor, found[1])
        differences = compare(restrict(found[1].document, compared), read_document(cursor, compared))
    for difference in differences:
        _log.warning(
            "drift before applying changeset %s: %s %s %s",
            changeset.name,
            difference.change,
            difference.kind,
            difference.name,
        )


def _execute_step(
    connection: Connection,
    changeset: Changeset,
    cursor: Cursor,
    step: Step,
    *,
    record_fingerprint: bool,
    lock_timeout: float | None,
) -> int:
    """Run the step's SQL as written; its duration in milliseconds.

    A failure rolls the transaction back, records a `failed` row and raises `StepFailed`; when that row
    cannot be written, nothing is recorded and `CavernsError` is raised from the database error.
    """
    started = time.monotonic()
    try:
        cursor.execute(step.sql)
    except Exception as error:
        connection.rollback()
        raise _record_failure(
            connection,
            changeset,
            step,
            error,
            _elapsed_ms(started),
            record_fingerprint=record_fingerprint,
            lock_timeout=lock_timeout,
        ) from error
    return _elapsed_ms(started)


def _write_step(
    connection: Connection,
    changeset: Changeset,
    status: StepStatus,
    outcome: str,
    *,
    record_fingerprint: bool,
    last: bool,
    lock_timeout: float | None,
) -> LedgerEntry | None:
    """Run or accept one step in one transaction; None when another runner applied it first.

    The ledger is read again under the lock: a status that changed since preflight (an `applied`
    one means the work is done) must not be acted on. With *record_fingerprint* a failure of the step
    records a fingerprint with its `failed` row, and, when the step is the *last* of the run, so does
    its own row.
    """
    step = status.step
    with transaction(connection) as cursor:
        lock(cursor, lock_timeout=lock_timeout)
        current = _recheck(changeset, status, _read_entries(cursor, changeset.name))
        if current.status is Status.APPLIED:
            return None
        if current.status is not status.status:
            raise PreflightRefused([current])
        duration = None
        if outcome == _OUTCOME_APPLIED:
            duration = _execute_step(
                connection, changeset, cursor, step, record_fingerprint=record_fingerprint, lock_timeout=lock_timeout
            )
        entry = _insert(cursor, changeset.name, step, outcome, duration_ms=duration)
        if record_fingerprint and last:
            _record_fingerprint(cursor, entry)
        return entry


def _apply_statuses(
    connection: Connection,
    changeset: Changeset,
    statuses: Sequence[StepStatus],
    reapply: frozenset[str],
    accept: frozenset[str],
    *,
    record_fingerprint: bool,
    lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT,
) -> list[LedgerEntry]:
    """Walk the planned steps in order, each in its own transaction; the rows written.

    With *record_fingerprint* (a consumer's changeset, never Caverns' own) a run that has steps to write
    first warns of drift, then stores one fingerprint, in the transaction of its last ledger row.
    """
    plan = _plan(changeset, statuses, reapply, accept)
    if record_fingerprint and plan:
        _warn_of_drift(connection, changeset)
    written = []
    for position, (status, outcome) in enumerate(plan, start=1):
        entry = _write_step(
            connection,
            changeset,
            status,
            outcome,
            record_fingerprint=record_fingerprint,
            last=position == len(plan),
            lock_timeout=lock_timeout,
        )
        if entry is not None:
            _log.info(
                "%s step %s of changeset %s: ledger version %d", outcome, entry.label, entry.changeset, entry.version
            )
            written.append(entry)
    return written


def _require_owned(cursor: Cursor) -> None:
    """Refuse (`CavernsError`) the own schema or a relation in it that the current role neither owns nor inherits."""
    cursor.execute(_FOREIGN_OWNER_SQL)
    row = cursor.fetchone()
    if row is not None:
        description, owner, current = row
        raise CavernsError(
            f"{description} is owned by role {owner}, of which role {current} is not a member: "
            f"run install as {owner} or a member of it"
        )


def install(connection: Connection) -> list[LedgerEntry]:
    """Create the ledger by applying Caverns' own changeset; the rows written ([] when installed).

    Refuses (`CavernsError`, nothing written) a `caverns` schema or a relation in it owned by a role the current
    role is not a member of.
    """
    check_connection(connection)
    with read_only(connection) as cursor:
        _require_owned(cursor)
        statuses = _statuses(cursor, _OWN_CHANGESET)
    # An effect without its row (a schema-only restore) is recorded as accepted: re-running would only repeat it.
    unknown = frozenset(s.step.label for s in statuses if s.status is Status.UNKNOWN)
    for label in sorted(unknown):
        _log.warning("own step %s is present without a ledger row: recording it as accepted", label)
    return _apply_statuses(connection, _OWN_CHANGESET, statuses, frozenset(), unknown, record_fingerprint=False)


def apply(
    connection: Connection,
    changeset: Changeset,
    *,
    reapply: Collection[str] = (),
    accept: Collection[str] = (),
    lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT,
) -> list[LedgerEntry]:
    """Apply the steps of *changeset* that are pending, one transaction and one ledger row each.

    A step already applied is skipped. A `conflict`, `unknown` or `before_baseline` step refuses the
    whole run (`PreflightRefused`, nothing written) unless named in *reapply* (conflict or before_baseline:
    run the SQL, record a new `applied` row) or *accept* (any of the three: record `accepted` without
    running). A step that fails is rolled back, recorded as `failed` and raised as `StepFailed`;
    later steps do not run. Returns the rows written, in order.

    A run that writes ledger rows stores one fingerprint of the watched schemas, in the transaction of its
    last row (a failed step's `failed` row included); before its first step it logs a warning per difference
    between the live watched schemas and the latest recorded fingerprint.

    *lock_timeout* is the seconds each step's transaction waits for any lock, Caverns' advisory lock and the
    locks the step's SQL needs alike (None keeps the session's own `lock_timeout`; a number must be positive).
    A wait for Caverns' lock that times out raises `CavernsError`, nothing written; one inside a step fails the
    step like any other error (`failed` row `55P03: ...`, `StepFailed`).
    """
    validate_lock_timeout(lock_timeout)
    reapply_names, accept_names = names("reapply", reapply), names("accept", accept)
    statuses = preflight(connection, changeset)
    return _apply_statuses(
        connection,
        changeset,
        statuses,
        reapply_names,
        accept_names,
        record_fingerprint=True,
        lock_timeout=lock_timeout,
    )


def _baseline_steps(changeset: Changeset, through: str | None) -> Sequence[Step]:
    """The steps a baseline records: the first through *through*, by default the last."""
    if through is None:
        return changeset.steps
    for index, step in enumerate(changeset.steps):
        if step.label == through:
            return changeset.steps[: index + 1]
    raise ChangesetError(f"changeset {changeset.name!r} has no step {through!r} to baseline through")


def baseline(
    connection: Connection,
    changeset: Changeset,
    *,
    through: str | None = None,
    schemas: Collection[str] | None = None,
) -> LedgerEntry:
    """Record an existing database as the starting point of *changeset*; nothing is executed.

    One `baseline` row per step through *through* (default the last), in changeset order, each with its own
    step's checksum and the same fingerprint hash (`infinite.fingerprint`'s) of *schemas* (default: every
    schema but `pg_*`, `information_schema` and `caverns`); the fingerprint itself is stored once in
    `caverns.fingerprint`, at the last row's version, in the same transaction. Returns the *through* step's row.
    The schemas of the latest baseline are the watched schemas of later runs. Afterwards every step up to
    *through* is `applied`; a step added before it later is `before_baseline`. Refused (`BaselineRefused`)
    when the changeset already has ledger rows or a named schema does not exist.
    """
    check_connection(connection)
    _reject_reserved(changeset)
    steps = _baseline_steps(changeset, through)
    named = named_schemas(schemas)
    with read_only(connection) as cursor:
        _require_installed(cursor)
    with transaction(connection) as cursor:
        lock(cursor)
        if _read_entries(cursor, changeset.name):
            raise BaselineRefused(f"changeset {changeset.name!r} already has ledger rows")
        fingerprint = capture(cursor, resolve_schemas(cursor, named, BaselineRefused, default_schemas))
        for step in steps:
            entry = _insert(cursor, changeset.name, step, _OUTCOME_BASELINE, fingerprint=fingerprint.hash)
        record(cursor, entry.version, fingerprint)
    _log.info("baseline of changeset %s at step %s: ledger version %d", entry.changeset, entry.label, entry.version)
    return entry
