"""Nozdormu, the migration ledger: steps and changesets, the ledger, preflight, apply and baseline.

Input: a DB-API 2.0 connection (autocommit off) and `Step` / `Changeset` values built from SQL text,
a list or a directory of `.sql` files. Output: validated, immutable values, and ledger rows in the
`caverns.ledger` table of the connected database, written by Caverns itself, plus the
`caverns.acknowledgement` rows of the risky steps `apply` was told to run (Murozond guards `apply`) and the
`caverns.drift` row of a run that proceeded past drift.
The module imports no database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import reprlib
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from caverns_of_time import __version__
from caverns_of_time._connection import (
    DEFAULT_LOCK_TIMEOUT,
    EPOCHS_EXISTS_SQL,
    MILLISECONDS,
    Connection,
    Cursor,
    autocommit,
    check_connection,
    check_writable,
    lock,
    names,
    read_only,
    run_check,
    session_lock,
    sqlstate,
    transaction,
    validate_limit,
    validate_lock_timeout,
    validate_name,
)
from caverns_of_time._fingerprint import (
    FINGERPRINT_EXISTS_SQL,
    FINGERPRINT_SERVER_SQL,
    Difference,
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
from caverns_of_time._sql import split
from caverns_of_time.errors import (
    BaselineRefused,
    CavernsError,
    ChangesetError,
    DriftRefused,
    GuardRefused,
    LedgerNotInstalled,
    PreflightRefused,
    StepFailed,
)
from caverns_of_time.murozond import Classification, Risk, classify

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
# The first line of a step's SQL that makes it run outside a transaction (for `CREATE INDEX CONCURRENTLY`, `VACUUM`).
_NO_TRANSACTION_MARKER = "-- caverns:no-transaction"
_SQL_SUFFIX = ".sql"
_CHECK_SUFFIX = ".check.sql"

# Caverns' own changeset; its name is also the schema that holds the ledger.
_OWN_CHANGESET_NAME = "caverns"
_OUTCOME_APPLIED = "applied"
_OUTCOME_FAILED = "failed"
_OUTCOME_ACCEPTED = "accepted"
_OUTCOME_BASELINE = "baseline"


def _normalised(sql: str) -> str:
    """The SQL with line endings normalised and the outer ASCII whitespace trimmed.

    ASCII whitespace is space, tab, LF, CR, FF and VT; any other character, NBSP included, is kept.
    """
    return sql.replace("\r\n", "\n").replace("\r", "\n").strip(_ASCII_WHITESPACE)


def _checksum(sql: str) -> str:
    """SHA-256 hex of the normalised SQL as UTF-8."""
    return hashlib.sha256(_normalised(sql).encode("utf-8")).hexdigest()


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

    A step whose SQL starts with the line `-- caverns:no-transaction` (after leading whitespace, case-sensitive,
    trailing whitespace on that line ignored) is not `transactional`: `apply` runs it outside a transaction, for
    a statement PostgreSQL refuses inside one (`CREATE INDEX CONCURRENTLY`, `VACUUM`). It must hold exactly one
    statement (`ChangesetError` otherwise). The marker is part of the SQL, so the checksum covers it.
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
        if not self.transactional and (count := len(split(self.sql))) != 1:
            raise ChangesetError(
                f"step {self.label!r} runs outside a transaction and must hold exactly one statement, not {count}"
            )

    @classmethod
    def from_file(cls, path: str | os.PathLike[str], *, label: str | None = None, check: str | None = None) -> Step:
        """A step whose SQL is the UTF-8 text of *path*; the label defaults to the file name without `.sql`."""
        file = Path(path)
        return cls(label if label is not None else file.stem, _read_text(file), check)

    @property
    def checksum(self) -> str:
        """SHA-256 hex of the normalised SQL."""
        return _checksum(self.sql)

    @property
    def transactional(self) -> bool:
        """False when the SQL's first line is the `-- caverns:no-transaction` marker."""
        first_line = _normalised(self.sql).split("\n", 1)[0]
        return first_line.rstrip(_ASCII_WHITESPACE) != _NO_TRANSACTION_MARKER


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
# member of that inherits its privileges (`USAGE`: a member without inherit cannot REVOKE or ALTER): what it is called,
# its owner and the current role. No row when every owner is the role or a role whose privileges it inherits.
_FOREIGN_OWNER_SQL = """
SELECT description, owner, current_user::text FROM (
    SELECT 0 AS rank, 'schema caverns' AS description, n.nspowner AS owner_oid,
           pg_get_userbyid(n.nspowner)::text AS owner
    FROM pg_namespace n WHERE n.nspname = 'caverns'
    UNION ALL
    SELECT 1, 'relation caverns.' || c.relname, c.relowner, pg_get_userbyid(c.relowner)::text
    FROM pg_class c WHERE c.relnamespace = to_regnamespace('caverns')
) objects
WHERE NOT pg_has_role(current_user, owner_oid, 'USAGE')
ORDER BY rank, description
LIMIT 1
"""
# WARNING: this statement ships with the release: never edit it once released; its checksum is recorded
# in every consumer's ledger. A change to the fingerprint table is a new step of the own changeset (hard rule 6).
_FINGERPRINT_SERVER_DDL_SQL = (
    "ALTER TABLE caverns.fingerprint ADD COLUMN IF NOT EXISTS server_version integer CHECK (server_version > 0);"
)
# WARNING: this statement ships with the release: never edit it once released; its checksum is recorded
# in every consumer's ledger. A change to the acknowledgements is a new step of the own changeset (hard rule 6).
_ACKNOWLEDGEMENT_DDL_SQL = """
CREATE TABLE IF NOT EXISTS caverns.acknowledgement (
    version     bigint      PRIMARY KEY REFERENCES caverns.ledger (version),
    risk        text        NOT NULL CHECK (risk IN ('destructive', 'lock_hazard', 'unknown')),
    findings    jsonb       NOT NULL CHECK (jsonb_typeof(findings) = 'array' AND jsonb_array_length(findings) > 0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON caverns.acknowledgement FROM PUBLIC;
CREATE OR REPLACE FUNCTION caverns.append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'caverns.% is append-only: % refused', TG_TABLE_NAME, TG_OP;
END
$$;
REVOKE ALL ON FUNCTION caverns.append_only() FROM PUBLIC;
CREATE OR REPLACE TRIGGER acknowledgement_no_update_delete BEFORE UPDATE OR DELETE ON caverns.acknowledgement
    FOR EACH ROW EXECUTE FUNCTION caverns.append_only();
CREATE OR REPLACE TRIGGER acknowledgement_no_truncate BEFORE TRUNCATE ON caverns.acknowledgement
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.append_only();
"""
_ACKNOWLEDGEMENT_EXISTS_SQL = "SELECT to_regclass('caverns.acknowledgement') IS NOT NULL"
# WARNING: this statement ships with the release: never edit it once released; its checksum is recorded
# in every consumer's ledger. A change to the drift records is a new step of the own changeset (hard rule 6).
_DRIFT_DDL_SQL = """
CREATE TABLE IF NOT EXISTS caverns.drift (
    version             bigint      PRIMARY KEY REFERENCES caverns.ledger (version),
    fingerprint_version bigint      NOT NULL REFERENCES caverns.fingerprint (version),
    schemas             text[]      NOT NULL,
    differences         jsonb       NOT NULL
        CHECK (jsonb_typeof(differences) = 'array' AND jsonb_array_length(differences) > 0),
    recorded_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON caverns.drift FROM PUBLIC;
CREATE OR REPLACE TRIGGER drift_no_update_delete BEFORE UPDATE OR DELETE ON caverns.drift
    FOR EACH ROW EXECUTE FUNCTION caverns.append_only();
CREATE OR REPLACE TRIGGER drift_no_truncate BEFORE TRUNCATE ON caverns.drift
    FOR EACH STATEMENT EXECUTE FUNCTION caverns.append_only();
"""
_DRIFT_EXISTS_SQL = "SELECT to_regclass('caverns.drift') IS NOT NULL"
_OWN_CHANGESET = Changeset(
    _OWN_CHANGESET_NAME,
    [
        Step("ledger", _LEDGER_DDL_SQL, check=_LEDGER_EXISTS_SQL),
        Step("fingerprint", _FINGERPRINT_DDL_SQL, check=FINGERPRINT_EXISTS_SQL),
        Step("epochs", _EPOCHS_DDL_SQL, check=EPOCHS_EXISTS_SQL),
        Step("privileges", _PRIVILEGES_DDL_SQL, check=_PRIVILEGES_REVOKED_SQL),
        Step("fingerprint_server", _FINGERPRINT_SERVER_DDL_SQL, check=FINGERPRINT_SERVER_SQL),
        Step("acknowledgement", _ACKNOWLEDGEMENT_DDL_SQL, check=_ACKNOWLEDGEMENT_EXISTS_SQL),
        Step("drift", _DRIFT_DDL_SQL, check=_DRIFT_EXISTS_SQL),
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
# Every invalid index of the database: what a failed `CREATE INDEX CONCURRENTLY` leaves behind.
_INVALID_INDEXES_SQL = (
    "SELECT n.nspname::text || '.' || c.relname::text FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT x.indisvalid ORDER BY 1"
)
_INSERT_ACKNOWLEDGEMENT_SQL = "INSERT INTO caverns.acknowledgement (version, risk, findings) VALUES (%s, %s, %s::jsonb)"
_INSERT_DRIFT_SQL = (
    "INSERT INTO caverns.drift (version, fingerprint_version, schemas, differences) VALUES (%s, %s, %s, %s::jsonb)"
)

_ON_DRIFT_REFUSE = "refuse"
_ON_DRIFT_WARN = "warn"
_ON_DRIFT = (_ON_DRIFT_REFUSE, _ON_DRIFT_WARN)


def _validate_on_drift(value: object) -> None:
    """*value* must be one of `_ON_DRIFT`; a `ChangesetError` names it otherwise."""
    if value not in _ON_DRIFT:
        choices = " or ".join(repr(choice) for choice in _ON_DRIFT)
        raise ChangesetError(f"on_drift must be {choices}, not {reprlib.repr(value)}")


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
    return round((time.monotonic() - started) * MILLISECONDS)


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


def _guard(
    changeset: Changeset, plan: Sequence[tuple[StepStatus, str]], acknowledge: frozenset[str]
) -> dict[str, Classification]:
    """Murozond's guard over the steps of *plan* that run SQL; the classifications of the acknowledged ones.

    Refuses before anything is written: `ChangesetError` for an *acknowledge* label the changeset lacks, whose step
    does not run in this apply or is safe (checked in that order, the first sorted label reported), then
    `GuardRefused` for a step that is not safe and not named in *acknowledge*.
    """
    running = {status.step.label: status for status, outcome in plan if outcome == _OUTCOME_APPLIED}
    missing = sorted(acknowledge - {step.label for step in changeset.steps})
    if missing:
        raise ChangesetError(f"changeset {changeset.name!r} has no step {missing[0]!r} to acknowledge")
    idle = sorted(acknowledge - running.keys())
    if idle:
        raise ChangesetError(f"step {idle[0]!r} does not run in this apply: nothing to acknowledge")
    classified = {label: classify(status.step.sql) for label, status in running.items()}
    safe = sorted(label for label in acknowledge if classified[label].risk is Risk.SAFE)
    if safe:
        raise ChangesetError(f"step {safe[0]!r} is safe: nothing to acknowledge")
    refused = [label for label, found in classified.items() if found.risk is not Risk.SAFE and label not in acknowledge]
    if refused:
        raise GuardRefused([running[label] for label in refused], [classified[label] for label in refused])
    return {label: classified[label] for label in acknowledge}


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


@dataclass(frozen=True)
class _Drift:
    """What differs between the latest recorded fingerprint (at ledger version *fingerprint_version*) and the live
    database, compared over *schemas*; never empty."""

    fingerprint_version: int
    schemas: list[str]
    differences: tuple[Difference, ...]


def _record_failure(
    connection: Connection,
    changeset: Changeset,
    step: Step,
    error: Exception,
    duration_ms: int,
    *,
    consumer: bool,
    lock_timeout: float | None,
    acknowledgement: Classification | None = None,
    drift: _Drift | None = None,
) -> StepFailed:
    """Record a failed step in a transaction of its own; the error to raise.

    The wait for Caverns' lock has no timeout: the run has a failure to record, and losing the row would leave
    the ledger silent about it. *lock_timeout* bounds the waits after it.

    With *consumer* the transaction also stores the fingerprint of the database as the steps before the failed one
    left it, with an *acknowledgement* (the step ran acknowledged) the acknowledgement row and with a *drift* (the
    run's first row) the drift record. Raises `CavernsError` from *error* when the failure cannot be recorded:
    the ledger does not exist (the failed step was its own) or writing the row fails (a read-only
    session). The transaction is rolled back either way, so no fingerprint is recorded then.
    """
    try:
        with transaction(connection) as cursor:
            lock(cursor, lock_timeout=lock_timeout, unbounded_wait=True)
            if not _ledger_exists(cursor):
                raise CavernsError("the ledger does not exist")
            entry = _record_row(
                cursor,
                changeset,
                step,
                _OUTCOME_FAILED,
                error=_recorded_error(error),
                duration_ms=duration_ms,
                acknowledgement=acknowledgement,
                with_fingerprint=consumer,
                drift=drift,
            )
    except Exception as record_error:
        raise CavernsError(
            f"step {step.label!r} of changeset {changeset.name!r} failed and could not be recorded: {record_error}"
        ) from error
    _log.warning("step %s of changeset %s failed: ledger version %d", step.label, changeset.name, entry.version)
    return StepFailed(step.label, entry.version)


def _record_row(
    cursor: Cursor,
    changeset: Changeset,
    step: Step,
    outcome: str,
    *,
    acknowledgement: Classification | None,
    with_fingerprint: bool,
    drift: _Drift | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> LedgerEntry:
    """The one place a step's ledger row is written, with its acknowledgement, the drift of the run's first row
    and, when asked, the fingerprint."""
    entry = _insert(cursor, changeset.name, step, outcome, error=error, duration_ms=duration_ms)
    if acknowledgement is not None:
        _record_acknowledgement(cursor, entry, acknowledgement)
    if drift is not None:
        _record_drift(cursor, entry, drift)
    if with_fingerprint:
        _record_fingerprint(cursor, entry)
    return entry


def _record_fingerprint(cursor: Cursor, entry: LedgerEntry) -> None:
    """Store the fingerprint of the watched schemas, as this transaction sees them, at *entry*'s version."""
    record(cursor, entry.version, capture(cursor, watched_schemas(cursor)))


def _record_acknowledgement(cursor: Cursor, entry: LedgerEntry, classification: Classification) -> None:
    """Store what the caller acknowledged for *entry*'s step: its risk and findings, at *entry*'s version."""
    findings = [
        {"risk": finding.risk.value, "statement": finding.statement, "reason": finding.reason}
        for finding in classification.findings
    ]
    cursor.execute(_INSERT_ACKNOWLEDGEMENT_SQL, (entry.version, classification.risk.value, json.dumps(findings)))


def _record_drift(cursor: Cursor, entry: LedgerEntry, drift: _Drift) -> None:
    """Store *drift*, the differences a run proceeded past, at *entry*'s version (the run's first row)."""
    differences = [{"kind": d.kind, "name": d.name, "change": d.change} for d in drift.differences]
    cursor.execute(
        _INSERT_DRIFT_SQL, (entry.version, drift.fingerprint_version, drift.schemas, json.dumps(differences))
    )


def _find_drift(connection: Connection) -> _Drift | None:
    """The differences between the latest recorded fingerprint and the live database; None when there is no
    fingerprint or no difference. Read-only.

    Compares the watched schemas plus those the fingerprint covers, so a schema dropped since reads as removed.
    """
    with read_only(connection) as cursor:
        found = latest_fingerprint(cursor)
        if found is None:
            return None
        version, recorded = found
        compared = compared_schemas(cursor, recorded)
        differences = compare(restrict(recorded.document, compared), read_document(cursor, compared))
    return _Drift(version, compared, tuple(differences)) if differences else None


def _check_drift(connection: Connection, changeset: Changeset, on_drift: str) -> _Drift | None:
    """The drift a run proceeds past, each difference logged as a warning; `DriftRefused` unless *on_drift* is warn."""
    drift = _find_drift(connection)
    if drift is None:
        return None
    if on_drift == _ON_DRIFT_REFUSE:
        raise DriftRefused(drift.fingerprint_version, drift.differences)
    for difference in drift.differences:
        _log.warning(
            "drift before applying changeset %s: %s %s %s",
            changeset.name,
            difference.change,
            difference.kind,
            difference.name,
        )
    return drift


def _execute_step(
    connection: Connection,
    changeset: Changeset,
    cursor: Cursor,
    step: Step,
    *,
    consumer: bool,
    lock_timeout: float | None,
    acknowledgement: Classification | None,
    drift: _Drift | None,
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
            consumer=consumer,
            lock_timeout=lock_timeout,
            acknowledgement=acknowledgement,
            drift=drift,
        ) from error
    return _elapsed_ms(started)


def _invalid_indexes(connection: Connection) -> list[str]:
    """Every invalid index of the database (a failed `CREATE INDEX CONCURRENTLY` leaves one), `schema.index`."""
    with read_only(connection) as cursor:
        cursor.execute(_INVALID_INDEXES_SQL)
        return [row[0] for row in cursor.fetchall()]


def _warn_of_invalid_indexes(connection: Connection, changeset: Changeset, step: Step) -> None:
    """Warn of the invalid indexes a step that failed outside a transaction may have left; never raises."""
    try:
        invalid = _invalid_indexes(connection)
    except Exception:
        return  # the StepFailed matters more than the warning
    if invalid:
        _log.warning(
            "step %s of changeset %s failed outside a transaction; invalid indexes in the database: %s; "
            "drop each with DROP INDEX CONCURRENTLY before reapplying",
            step.label,
            changeset.name,
            ", ".join(invalid),
        )


def _execute_step_outside_transaction(
    connection: Connection,
    changeset: Changeset,
    step: Step,
    *,
    consumer: bool,
    lock_timeout: float | None,
    acknowledgement: Classification | None,
    drift: _Drift | None,
) -> int:
    """Run the step's one statement with autocommit on; its duration in milliseconds.

    Nothing is rolled back on a failure: a `failed` row is recorded, the invalid indexes of the database are
    warned of and `StepFailed` is raised; when that row cannot be written, `CavernsError` is raised from the
    database error. The caller holds Caverns' session lock.
    """
    started = time.monotonic()
    try:
        with autocommit(connection, lock_timeout=lock_timeout) as cursor:
            cursor.execute(step.sql)
    except Exception as error:
        failure = _record_failure(
            connection,
            changeset,
            step,
            error,
            _elapsed_ms(started),
            consumer=consumer,
            lock_timeout=lock_timeout,
            acknowledgement=acknowledgement,
            drift=drift,
        )
        _warn_of_invalid_indexes(connection, changeset, step)
        raise failure from error
    return _elapsed_ms(started)


def _still_to_write(cursor: Cursor, changeset: Changeset, status: StepStatus) -> bool:
    """Read the ledger again, under the lock: False when another runner applied the step since preflight.

    A status that changed since preflight raises `PreflightRefused`; an `applied` one means the work is done.
    """
    current = _recheck(changeset, status, _read_entries(cursor, changeset.name))
    if current.status is Status.APPLIED:
        return False
    if current.status is not status.status:
        raise PreflightRefused([current])
    return True


def _warn_acknowledged(changeset: Changeset, step: Step, acknowledgement: Classification | None) -> None:
    if acknowledgement is not None:
        _log.warning(
            "step %s of changeset %s runs acknowledged as %s", step.label, changeset.name, acknowledgement.risk
        )


def _write_step_outside_transaction(
    connection: Connection,
    changeset: Changeset,
    status: StepStatus,
    *,
    consumer: bool,
    first: bool,
    last: bool,
    lock_timeout: float | None,
    acknowledgement: Classification | None,
    drift: _Drift | None,
) -> LedgerEntry | None:
    """Run a non-transactional step under Caverns' session lock, then record it; None when applied elsewhere.

    The statement runs in autocommit between two transactions: the ledger read and the row write. The wait for
    the session lock follows *first* as in `_write_step`; the row's own lock is taken at once (this session holds
    the session lock) and without a timeout. A crash between the statement and its row leaves the effect without
    a row.
    """
    step = status.step
    committed = f"step {step.label!r} of changeset {changeset.name!r} was recorded"
    with session_lock(connection, committed=committed, lock_timeout=lock_timeout, unbounded_wait=not first):
        with transaction(connection) as cursor:
            if not _still_to_write(cursor, changeset, status):
                return None
        _warn_acknowledged(changeset, step, acknowledgement)
        duration = _execute_step_outside_transaction(
            connection,
            changeset,
            step,
            consumer=consumer,
            lock_timeout=lock_timeout,
            acknowledgement=acknowledgement,
            drift=drift,
        )
        with transaction(connection) as cursor:
            lock(cursor, lock_timeout=lock_timeout, unbounded_wait=True)
            return _record_row(
                cursor,
                changeset,
                step,
                _OUTCOME_APPLIED,
                duration_ms=duration,
                acknowledgement=acknowledgement,
                with_fingerprint=consumer and last,
                drift=drift,
            )


def _write_step(
    connection: Connection,
    changeset: Changeset,
    status: StepStatus,
    outcome: str,
    *,
    consumer: bool,
    first: bool,
    last: bool,
    lock_timeout: float | None,
    acknowledgement: Classification | None = None,
    drift: _Drift | None = None,
) -> LedgerEntry | None:
    """Run or accept one step in one transaction; None when another runner applied it first.

    Until the run has written a row (*first*), the wait for Caverns' lock is bounded by *lock_timeout* (a timeout
    raises `CavernsError` with nothing written); afterwards a step waits for it without a timeout, so the run is
    not stopped between its rows and its fingerprint. The step's own lock waits are bounded in every case.

    The ledger is read again under the lock: a status that changed since preflight (an `applied`
    one means the work is done) must not be acted on. With *consumer* a failure of the step
    records a fingerprint with its `failed` row, and, when the step is the *last* of the run, so does
    its own row. A step run with an *acknowledgement* (its Murozond classification) writes the acknowledgement
    row in the same transaction as its `applied` or `failed` row; a step another runner applied first writes none.
    The *drift* the run proceeded past is recorded in the same transaction as the row of the run's first written
    step, whatever that row is (`applied`, `accepted` or `failed`); a step another runner applied first writes none.

    A non-transactional step that runs (not an accepted one) takes `_write_step_outside_transaction`: its
    statement runs outside any transaction and its row follows in a transaction of its own.
    """
    step = status.step
    if outcome == _OUTCOME_APPLIED and not step.transactional:
        return _write_step_outside_transaction(
            connection,
            changeset,
            status,
            consumer=consumer,
            first=first,
            last=last,
            lock_timeout=lock_timeout,
            acknowledgement=acknowledgement,
            drift=drift,
        )
    with transaction(connection) as cursor:
        lock(cursor, lock_timeout=lock_timeout, unbounded_wait=not first)
        if not _still_to_write(cursor, changeset, status):
            return None
        duration = None
        if outcome == _OUTCOME_APPLIED:
            _warn_acknowledged(changeset, step, acknowledgement)
            duration = _execute_step(
                connection,
                changeset,
                cursor,
                step,
                consumer=consumer,
                lock_timeout=lock_timeout,
                acknowledgement=acknowledgement,
                drift=drift,
            )
        return _record_row(
            cursor,
            changeset,
            step,
            outcome,
            duration_ms=duration,
            acknowledgement=acknowledgement,
            with_fingerprint=consumer and last,
            drift=drift,
        )


def _apply_statuses(
    connection: Connection,
    changeset: Changeset,
    statuses: Sequence[StepStatus],
    reapply: frozenset[str],
    accept: frozenset[str],
    *,
    consumer: bool,
    acknowledge: frozenset[str] = frozenset(),
    on_drift: str = _ON_DRIFT_REFUSE,
    lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT,
) -> list[LedgerEntry]:
    """Walk the planned steps in order, each in its own transaction; the rows written.

    *consumer* says the changeset is a consumer's, never Caverns' own: it governs the guard, the drift check and
    the fingerprint. Such a run is guarded (`_guard`, after the status refusal and before anything is written;
    *acknowledge* names the risky steps it may run); one that has steps to write then checks drift (`_check_drift`:
    refused, or per *on_drift* recorded with the first row it writes) and stores one fingerprint, in the
    transaction of its last ledger row.
    """
    plan = _plan(changeset, statuses, reapply, accept)
    acknowledgements = _guard(changeset, plan, acknowledge) if consumer else {}
    drift = _check_drift(connection, changeset, on_drift) if consumer and plan else None
    written: list[LedgerEntry] = []
    for position, (status, outcome) in enumerate(plan, start=1):
        entry = _write_step(
            connection,
            changeset,
            status,
            outcome,
            consumer=consumer,
            first=not written,
            last=position == len(plan),
            lock_timeout=lock_timeout,
            acknowledgement=acknowledgements.get(status.step.label),
            drift=drift,
        )
        if entry is not None:
            drift = None  # recorded with the run's first row
            _log.info(
                "%s step %s of changeset %s: ledger version %d", outcome, entry.label, entry.changeset, entry.version
            )
            written.append(entry)
    return written


def _require_owned(cursor: Cursor) -> None:
    """Refuse (`CavernsError`) the own schema or a relation in it that the current role neither owns nor inherits.

    A role that is a member without inheriting the owner's privileges is refused too, as it cannot change what the
    owner owns.
    """
    cursor.execute(_FOREIGN_OWNER_SQL)
    row = cursor.fetchone()
    if row is not None:
        description, owner, current = row
        raise CavernsError(
            f"{description} is owned by role {owner}, whose privileges role {current} does not inherit: "
            f"run install as {owner} or a member that inherits its privileges"
        )


def install(connection: Connection) -> list[LedgerEntry]:
    """Create the ledger by applying Caverns' own changeset; the rows written ([] when installed).

    Refuses (`CavernsError`, nothing written) a standby or a read-only session, and a `caverns` schema or a
    relation in it owned by a role the current role is not a member of that inherits its privileges.
    """
    check_connection(connection)
    check_writable(connection)
    with read_only(connection) as cursor:
        _require_owned(cursor)
        statuses = _statuses(cursor, _OWN_CHANGESET)
    # An effect without its row (a schema-only restore) is recorded as accepted: re-running would only repeat it.
    unknown = frozenset(s.step.label for s in statuses if s.status is Status.UNKNOWN)
    for label in sorted(unknown):
        _log.warning("own step %s is present without a ledger row: recording it as accepted", label)
    return _apply_statuses(connection, _OWN_CHANGESET, statuses, frozenset(), unknown, consumer=False)


def apply(
    connection: Connection,
    changeset: Changeset,
    *,
    reapply: Collection[str] = (),
    accept: Collection[str] = (),
    acknowledge: Collection[str] = (),
    on_drift: str = _ON_DRIFT_REFUSE,
    lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT,
) -> list[LedgerEntry]:
    """Apply the steps of *changeset* that are pending, one transaction and one ledger row each.

    A step already applied is skipped. A `conflict`, `unknown` or `before_baseline` step refuses the
    whole run (`PreflightRefused`, nothing written) unless named in *reapply* (conflict or before_baseline:
    run the SQL, record a new `applied` row) or *accept* (any of the three: record `accepted` without
    running). A step that fails is rolled back, recorded as `failed` and raised as `StepFailed`;
    later steps do not run. Returns the rows written, in order.

    Murozond guards the SQL that runs (a pending step, or one in *reapply*; an *accept*ed step runs none): a step
    that `murozond.classify` does not call safe refuses the run (`GuardRefused`, a `PreflightRefused`, nothing
    written) unless its label is in *acknowledge*; an acknowledged step is logged as a warning and recorded in
    `caverns.acknowledgement`, in the transaction of its `applied` or `failed` row. Caverns' own changeset is not
    guarded. The refusals, all before anything is written, come in this order: the status refusal, an *acknowledge*
    label the changeset lacks, whose step does not run in this apply or is safe (`ChangesetError`), the guard, drift.

    A run that writes ledger rows stores one fingerprint of the watched schemas, in the transaction of its
    last row (a failed step's `failed` row included). Before its first step a run with steps to write compares the
    live watched schemas (and those the fingerprint covers) with the latest recorded fingerprint, in a read-only
    transaction; no fingerprint, no check. With *on_drift* `"refuse"` (the default) a difference refuses the run
    (`DriftRefused`, nothing written); with `"warn"` it logs a warning per difference and the run proceeds, and
    the drift is recorded in `caverns.drift`, in the transaction of the run's first written row (`applied`,
    `accepted` or `failed`; a step another runner applied first writes none, so the next row carries it). Drift
    made between the check and the first step is absorbed by that step's fingerprint, unrecorded. Any other
    *on_drift* is a `ChangesetError`.

    *lock_timeout* is the seconds each step's transaction waits for a lock its SQL needs, and the run's for
    Caverns' advisory lock until the run has written a row (None keeps the session's own `lock_timeout`; a number
    must be positive, 1 ms or more). The first wait for Caverns' lock that times out raises `CavernsError`, nothing
    written; one inside a step fails the step like any other error (`failed` row `55P03: ...`, `StepFailed`). Once
    the run has written a row, its later waits for Caverns' lock have no timeout, so that a run is never stopped
    between its rows and its fingerprint; the failure record's wait is never bounded, even before any row.

    A step that is not `transactional` (its SQL starts with `-- caverns:no-transaction`) runs outside a
    transaction, with autocommit on, under Caverns' session advisory lock (a direct connection or a session-mode
    pooler is required) and *lock_timeout* as the session's lock timeout for its statement; its row, acknowledgement
    and fingerprint follow in a transaction. A failure cannot be rolled back: it leaves the statement's partial
    effect (an invalid index), records a `failed` row, raises `StepFailed` and logs a warning naming every invalid
    index of the database, to drop with `DROP INDEX CONCURRENTLY` before reapplying. A crash between the statement
    and its row leaves the effect without a row: give such a step a check.

    A standby or a read-only session is refused with `CavernsError`, nothing written.
    """
    validate_lock_timeout(lock_timeout)
    _validate_on_drift(on_drift)
    reapply_names, accept_names = names("reapply", reapply), names("accept", accept)
    acknowledge_names = names("acknowledge", acknowledge)
    statuses = preflight(connection, changeset)
    check_writable(connection)
    return _apply_statuses(
        connection,
        changeset,
        statuses,
        reapply_names,
        accept_names,
        consumer=True,
        acknowledge=acknowledge_names,
        on_drift=on_drift,
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
    when the changeset already has ledger rows or a named schema does not exist, and with `CavernsError` on a
    standby or a read-only session.
    """
    check_connection(connection)
    check_writable(connection)
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
