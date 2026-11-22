# caverns_of_time

## Purpose & scope

The importable package. It is generic: nothing here names a consumer or assumes a consumer's schema
(hard rule 1). It holds the version, the shared exceptions and, so far, one component: Nozdormu, the
migration ledger.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | `__version__`, read from the installed distribution's metadata |
| `errors.py` | The exception types, all in one module |
| `nozdormu.py` | Nozdormu: the `Step` and `Changeset` model, the checksum, the loaders, the connection contract, the ledger, `install`, `preflight`, `apply` and `baseline` |

## How it works

Each component arrives as one module in its roadmap phase (`ROADMAP.md`); Nozdormu is the first.
The flow is install, then preflight, then apply; baseline adopts a database that already has the effects.

**Nozdormu, the model.** A `Step` is a label, SQL and an optional check; a `Changeset` is a name and an
ordered list of steps. Both are frozen and validated when built. A changeset comes from a Python list or
from a directory (`Changeset.from_directory`): every `*.sql` file of that directory, not its
sub-directories, is a step in file-name order, labelled with the file name minus `.sql`;
`<label>.check.sql` is the check of the step `<label>.sql`. Other files are ignored. A step is also
readable from one file (`Step.from_file`).

**Nozdormu, the ledger.** Every recorded change is a row of `caverns.ledger`. Caverns creates it itself:
`install(connection)` applies Caverns' own changeset (named `caverns`, one step `ledger`) and records that
step as the ledger's first row, in the same transaction as the DDL. Installing again writes nothing and
returns `[]`. Until the ledger exists, reading it behaves as an empty ledger; that is how the own
changeset bootstraps.

**Nozdormu, preflight.** `preflight(connection, changeset)` reports one `StepStatus` (step, status and the
ledger row that decided it, if any) per step in changeset order and writes nothing: it runs in one
read-only transaction that is always rolled back. It decides the ledger first, then the check:

| Status | When |
|---|---|
| `applied` | the latest non-failed row matches, or the step is before the baseline's step, or is the baseline's step with an unchanged checksum |
| `conflict` | the latest row of the step that is not `failed` has another checksum (the SQL was edited) |
| `pending` | no such row, and the step has no check or its check is false |
| `unknown` | no such row, but the check is true: the effect is present and nothing recorded it |

A `failed` row never decides a status. A step's check runs only when the ledger is silent about the step.
A check is exactly one SELECT statement: psycopg 3 refuses a second result set (`ChangesetError`), psycopg2
cannot report one, so there the rule is documented rather than detected.

A step inserted before the baseline's step counts as applied and never runs: add new steps after it. A
`baseline` row whose step the changeset no longer has makes preflight and apply refuse (`ChangesetError`);
because the ledger is append-only, the way out is a new changeset name. Keep the baseline's step in the changeset.

**Nozdormu, apply.** `apply(connection, changeset, *, reapply=(), accept=())` runs preflight, then walks the
steps in changeset order: `applied` is skipped, `pending` runs. Each write transaction starts at READ
COMMITTED whatever the session's isolation level, so that what is read under the lock is current. Each step
is its own transaction: take the ledger's advisory lock, read the ledger again (a step another runner
applied meanwhile is skipped; one whose status changed otherwise refuses the run), execute the step's SQL
as written, insert the ledger row and commit, so the step's effect and its row stand or fall together. A
step that raises is rolled back, then recorded as a `failed` row in a transaction of its own, and
`StepFailed` is raised from the database error; later steps do not run, and the step is `pending` again.
If the failure cannot be recorded, nothing is recorded and a `CavernsError` is raised instead, from the
step's own error. That happens when the failed step was Caverns' own `ledger` step (`install` as a role
that cannot create the schema), so there is no ledger to record into, or when the `failed` row cannot be
written (a read-only session). A connection lost during a step raises the driver's error and records
nothing. The rows written are returned and logged.
`install` takes the same path for Caverns' own changeset; when the ledger table exists without its row (a
schema-only restore) it records its own step as `accepted` without running it, and logs a warning.

A `conflict` or `unknown` step refuses the whole run before anything is executed or written
(`PreflightRefused`, listing the steps) unless the caller acknowledges it:

| Argument | Allowed for | Effect |
|---|---|---|
| `reapply` | `conflict` | run the edited SQL, record a new `applied` row; the old row stays |
| `accept` | `conflict`, `unknown` | record an `accepted` row with the current checksum, run nothing |

Naming a step in any other status is a `PreflightRefused`; naming a label the changeset lacks, or one
label in both, is a `ChangesetError`. Preferred practice: add a new step rather than edit one that has
been applied. Statements that cannot run inside a transaction (`CREATE INDEX CONCURRENTLY`) are
unsupported in v0.1: such a step fails and is recorded as a failed step.

**Nozdormu, baseline.** `baseline(connection, changeset, *, through=None, schemas=None)` adopts a database
that already holds a changeset's effects. It writes one `baseline` row (label: the `through` step, by
default the last; that step's checksum; a fingerprint) and executes nothing; from then on every step up to
and including `through` is `applied`, the later ones follow the normal rules. It runs in one transaction
under the advisory lock and is refused (`BaselineRefused`) when the changeset already has any ledger row.
The fingerprint is the SHA-256 of a canonical JSON document of the tables (kinds `r` and `p`) and their
columns (name, type, not-null, default) in the given schemas, read from the system catalogs with `search_path`
pinned to `pg_catalog` (type and default expressions print relative to it); without
`schemas` it covers every schema except `pg_*`, `information_schema` and `caverns`. A named schema that does
not exist is refused. Phase 2 widens the fingerprint.

## Interfaces & contracts

- `caverns_of_time.__version__`: the version from `pyproject.toml`; `0+unknown` in a checkout that was
  never installed.
- `Step(label, sql, check=None)`: the label is a non-empty single line without surrounding whitespace;
  the SQL is not blank. A check is SQL returning exactly one row with one boolean column, true when the
  step's effect is already present. `Step.from_file(path, *, label=None, check=None)` reads UTF-8 text.
- `Step.checksum`: the SHA-256 hex of the SQL as UTF-8 after line endings are normalised to `\n` and the
  whole text's leading and trailing ASCII whitespace (space, tab, LF, CR, FF, VT) is trimmed; any other
  character, a non-breaking space included, is kept. It covers the SQL only, not the label, the
  check or the changeset name, so a comment change inside the SQL changes it and a re-indent of the
  whole file does not.
- `Changeset(name, steps)`: the name follows the label rules; at least one step; labels are unique. The
  name `caverns` is Caverns' own.
- An invalid step, changeset or argument raises `ChangesetError`, naming the offending value; other refusals
  have their own errors, listed with each function below.
- The connection: any DB-API 2.0 connection with `%s` parameters. The `Connection` protocol names what is
  used (`cursor()`, `commit()`, `rollback()`, a readable `autocommit`; a cursor's `execute`, `fetchone`,
  `fetchall`, `close`), so psycopg 3 and psycopg2 connections both fit and the package imports no driver.
  Caverns commits and rolls back itself: pass a connection with **autocommit off**.
  Every public function first refuses, with a `CavernsError` that names the fix, a connection with
  autocommit on, and a connection whose open transaction has written something (checked with
  `pg_current_xact_id_if_assigned()`); the caller's work is not rolled back, so it can commit it and call
  again. A connection whose transaction has already failed, or that is lost, is refused the same way
  (a `CavernsError` from the driver's error), and also left untouched: roll it back or reconnect, then call again. A transaction that only read is accepted and ended with a
  rollback, which also ends anything transaction-scoped the caller set in it (`SET LOCAL`, an uncommitted
  `SET`, transaction-level locks). A psycopg 3 connection inside `with conn.transaction():` is unsupported,
  because Caverns must commit and roll back itself. A step's SQL and check run exactly as written, with no parameters, so a
  `%` in them is literal.
- `install(connection) -> list[LedgerEntry]`: the rows written, `[]` when the ledger is already installed;
  one `accepted` row when the table exists without its row.
- The ledger, `caverns.ledger`, one row per recorded event:

  | Column | Holds |
  |---|---|
  | `version` | identity; the ledger version, the order of events |
  | `changeset`, `label` | the step the row is about |
  | `checksum` | the step's checksum (64 lower-case hex digits) |
  | `outcome` | `applied`, `failed`, `baseline` or `accepted` |
  | `fingerprint` | required on `baseline`, else null |
  | `error` | the database error, present exactly on `failed` |
  | `duration_ms` | how long the step ran, when it ran |
  | `recorded_at`, `recorded_by` | `clock_timestamp()` and `current_user` at insert |
  | `caverns_version` | the package version that wrote the row |

  An index on (`changeset`, `label`, `version`) serves the reads. The ledger is append-only: triggers
  reject UPDATE, DELETE and TRUNCATE (hard rule 5); a correction is a new row. `LedgerEntry` carries these
  columns in this order.
- `preflight(connection, changeset) -> list[StepStatus]` and `Status` (`applied`, `pending`, `unknown`,
  `conflict`). A check that does not return exactly one row with one boolean column, or raises a database
  error, is a `ChangesetError` naming the step; so is a `baseline` row at a step the changeset lacks.
  `LedgerNotInstalled` is raised when Caverns' own changeset is not fully applied (call `install` first).
  `PreflightRefused` carries `statuses`; `StepFailed` carries `label` and `version`.
  The changeset name `caverns` is refused with a `ChangesetError`: the public functions take a consumer's
  changeset only.
- `apply(connection, changeset, *, reapply=(), accept=()) -> list[LedgerEntry]`: the rows written, in order
  (`[]` when nothing is to do). A bare string for `reapply` or `accept`, or a label in both, is a `ChangesetError`.
  Raises `PreflightRefused` (attribute `statuses`, the offending steps), `StepFailed` (attributes `label`
  and `version`, the failed row's ledger version; its `__cause__` is the driver's error), a plain
  `CavernsError` (its `__cause__` is the step's error) when a failed step cannot be recorded, and the
  errors of `preflight`.
- `baseline(connection, changeset, *, through=None, schemas=None) -> LedgerEntry`: the row written. Raises
  `BaselineRefused` (the changeset has rows; a named schema that does not exist) and `ChangesetError` (a
  `through` the changeset lacks, a bare string for `schemas`, an empty `schemas`).
- The public names are `Connection`, `LedgerEntry`, `Status`, `StepStatus`, `Step`, `Changeset` and the
  functions above; the exceptions, all in `caverns_of_time.errors`, are `CavernsError` (the base),
  `ChangesetError`, `LedgerNotInstalled`, `PreflightRefused`, `StepFailed` and `BaselineRefused`.
- The own changeset is never edited once released: a change to the ledger is a new own step (hard rule 6).
  The ledger's DDL is idempotent, and `caverns` is a reserved changeset name.
- No runtime dependency. Adding one is an ask-first decision.

## Extending

Follow the adding-a-component workflow of the development skill
(`.claude/skills/caverns-of-time-development/references/workflows/adding-a-component.md`).

## See also

`ROADMAP.md` · `.claude/skills/caverns-of-time-development/references/architecture.md`
