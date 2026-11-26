# caverns_of_time

## Purpose & scope

The importable package. It is generic: nothing here names a consumer or assumes a consumer's schema
(hard rule 1). It holds the version, the shared exceptions, the console helpers and, so far, four
components: Nozdormu, the migration ledger, Chromie, the command line, Infinite, the schema fingerprint, and
Epochs, the snapshots of opted-in tables (their registry, the snapshot, the catalog, a table as of an epoch, the
difference of two epochs and their deletion).

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | `__version__`, read from the installed distribution's metadata |
| `errors.py` | The exception types, all in one module |
| `_connection.py` | Shared private plumbing, not a component: the `Connection` and `Cursor` protocols, the connection guard and the writable check (`check_connection`, `check_writable`), the transaction helpers, the advisory lock every Caverns write takes (`ADVISORY_LOCK_KEY`, `lock`; `session_lock` for a REPEATABLE READ `transaction`, whose failed release is a `CavernsError`) and its lock timeout (`DEFAULT_LOCK_TIMEOUT`, `set_lock_timeout`), the SQLSTATE reader `sqlstate`, the name, limit and lock timeout validation (`validate_name`, `validate_limit`, `validate_lock_timeout`), the pinned search path (`pin_search_path`), the one-statement check runner and the collection-of-names guard |
| `_fingerprint.py` | Shared private fingerprint machinery, not a component: `Fingerprint`, the catalog queries, the document and its hash, the default, watched and missing schemas, `Difference`, `restrict` and `compare`, and the storage of fingerprints in `caverns.fingerprint` (`record`, `latest`) |
| `console.py` | The console helpers: status lines with their icons, tables and JSON, the confirmation prompt (`confirm`, `interactive`); the only module that prints or reads |
| `chromie.py` | Chromie: the `chromie` command's parsing, connection configuration and dispatch (command flow only) |
| `nozdormu.py` | Nozdormu: the `Step` and `Changeset` model, the checksum, the loaders, the ledger, `install`, `preflight`, `history`, `apply` and `baseline`; it re-exports `Connection`, whose contract lives in `_connection.py` |
| `infinite.py` | Infinite: `fingerprint` and the `Fingerprint` it returns, `drift` and the `Drift` and `Difference` it returns |
| `epochs.py` | Epochs: the registry of tracked tables (`register`, `unregister`, `tracked`, `TrackedTable`) in `caverns.epoch_table`, `snapshot`, `catalog`, `as_of`, `diff`, `delete` and the `Epoch`, `EpochTable`, `Column`, `TableAt` and `EpochDiff` (with `TableDiff`, `ColumnChange`, `RowChange`) they return |

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
`install(connection)` applies Caverns' own changeset (named `caverns`, four steps: `ledger`, then `fingerprint`,
the table of recorded fingerprints, then `epochs`, the epoch tables, then `privileges`, below) and records each step as a ledger row, in the same transaction as its DDL.
Installing again writes nothing and returns `[]`. Until the ledger exists, reading it behaves as an empty
ledger; that is how the own changeset bootstraps. A database installed before the `fingerprint`, the `epochs` or
the `privileges` step existed lacks its row: call `install()` again after upgrading, which applies each missing step
as a pending step; until then `preflight`, `apply` and `baseline` raise `LedgerNotInstalled` (`history` needs only
the ledger table and keeps working), and so do the epoch functions until `epochs` is applied. `drift` raises it before
the `fingerprint` step; before the `epochs` step it does so only given a changeset (its `preflight` runs first):
without one it checks only `caverns.fingerprint`. `install` records no fingerprint.

**Nozdormu, privileges.** The own step `privileges` revokes every privilege of `PUBLIC` on the schema `caverns`,
on its tables, its sequences and its functions (`REVOKE ALL ... FROM PUBLIC`); its check is that `PUBLIC` holds none
of them. A function is executable by `PUBLIC` unless revoked, so a later own step that adds a function revokes it
itself (a test asserts that `PUBLIC` can execute no function of `caverns` after `install`); Caverns sets no default
privileges. Only the schema's owner and the roles it grants to can use the ledger afterwards. Before applying
anything `install` also refuses, with a `CavernsError` and nothing written, a `caverns` schema, or a relation in it,
owned by a role the current role is not a member of (`schema caverns is owned by role alice, of which role bob is
not a member: run install as alice or a member of it`); a schema owned by a role the current role is a member of
is accepted. The grants a role needs to run `apply`, `baseline`, `register`, `unregister`, `snapshot` and
`delete` (it need not own anything, and an identity sequence needs no grant of its own):

```sql
GRANT USAGE ON SCHEMA caverns TO runner;
GRANT SELECT, INSERT ON caverns.ledger, caverns.fingerprint, caverns.epoch, caverns.epoch_source,
    caverns.epoch_row, caverns.epoch_deletion TO runner;
GRANT SELECT, INSERT, UPDATE, DELETE ON caverns.epoch_table TO runner;  -- the mutable registry
GRANT DELETE ON caverns.epoch_row TO runner;                           -- `delete` removes a deleted epoch's rows
```

Reading (`preflight`, `history`, `drift`, `catalog`, `as_of`, `diff`) needs `USAGE` and `SELECT` only. `install`
runs as the schema's owner or a member of the owner's role. The append-only triggers guard against mistakes, not
against the schema's owner or a superuser, who can drop or disable them. **Upgrade:** run `install()` once after
upgrading, as the owner; it applies `privileges`, after which a role that used the schema only through `PUBLIC` needs
the grants above.

**Nozdormu, preflight.** `preflight(connection, changeset)` reports one `StepStatus` (step, status and the
ledger row that decided it, if any) per step in changeset order and writes nothing: it runs in one
read-only transaction that is always rolled back. It decides the ledger first, then the check:

| Status | When |
|---|---|
| `applied` | the latest non-failed row (a `baseline` row included) has the step's checksum |
| `conflict` | the latest row of the step that is not `failed` has another checksum (the SQL was edited) |
| `pending` | no such row, and the step has no check or its check is false |
| `unknown` | no such row, but the check is true: the effect is present and nothing recorded it |
| `before_baseline` | no such row, and the step sits at or before the baseline's step: it was added after the baseline was taken, so nothing says whether the database has its effect; its check is not run |

A `failed` row never decides a status. A step's check runs only when the ledger is silent about the step.
A check is exactly one statement: psycopg 3 refuses a second result set (`ChangesetError`), psycopg2
cannot report one, so there the rule is documented rather than detected.

A step inserted before the baseline's step is `before_baseline`, not applied: `apply` refuses the run until the
step is acknowledged (below); add new steps after the baseline's step to avoid it. A
`baseline` row whose step the changeset no longer has makes preflight and apply refuse (`ChangesetError`);
because the ledger is append-only, the way out is a new changeset name. Keep the baseline's step in the changeset.

**Nozdormu, apply.** `apply(connection, changeset, *, reapply=(), accept=(), lock_timeout=5.0)` runs preflight, then walks the
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

*Lock timeout.* `lock_timeout` is the seconds one step's transaction waits for any lock: before it takes Caverns'
advisory lock the transaction sets `lock_timeout` to it (transaction-local, so the session's own setting is
untouched afterwards), which bounds Caverns' lock and every lock the step's SQL then waits for. It is `5.0` by
default; a positive finite number (rounded to milliseconds, at least 1 ms, at most 2147483.647 s), else
`ChangesetError`; `None` leaves the session's own `lock_timeout` in force. A timeout on Caverns' lock (another
Caverns run holds it) raises `CavernsError` naming the lock, with the transaction rolled back and nothing
written; a timeout inside a step fails the step like any other error: rolled back, a `failed` row whose error is
`55P03: <message>` and `StepFailed`, the step `pending` again. `install` and `baseline` always use the default, and
so do `register`, `unregister` and `delete`; they take no parameter.

A run that writes ledger rows stores one fingerprint in `caverns.fingerprint` (below), in the transaction of
its last ledger row: the last planned step's transaction when that step writes a row, or, when a step fails,
the transaction that records its `failed` row (the database is then as the steps before it left it). If that
failure cannot be recorded, no fingerprint is. A run that writes nothing stores nothing, and neither does
`install`. The fingerprint covers the watched schemas (below). Two kinds of run write rows and still store no
fingerprint of their own: one interrupted after earlier steps committed (a lost connection, or a mid-run
`PreflightRefused` because another runner changed a step), and one whose last planned step another runner
applied first. The next `drift` then shows the committed steps' changes in the watched schemas as drift until the next fingerprint.
Before its first step, and only when the run has a step to write, `apply` compares the live database with the
latest recorded fingerprint (the watched schemas plus those the fingerprint covers, as `drift` does) in a
read-only transaction and logs one warning per difference through the
`caverns_of_time.nozdormu` logger (`drift before applying changeset <name>: <change> <kind> <name>`); it never
refuses, and says nothing when no fingerprint is recorded. The warning only reaches logs: in CI run
`chromie drift` before `apply` to fail the build on drift.
`install` takes the same path for Caverns' own changeset; when the ledger table exists without its row (a
schema-only restore) it records its own step as `accepted` without running it, and logs a warning.

A `conflict`, `unknown` or `before_baseline` step refuses the whole run before anything is executed or written
(`PreflightRefused`, listing the steps) unless the caller acknowledges it:

| Argument | Allowed for | Effect |
|---|---|---|
| `reapply` | `conflict`, `before_baseline` | run the step's SQL, record a new `applied` row; an old row stays |
| `accept` | `conflict`, `unknown`, `before_baseline` | record an `accepted` row with the current checksum, run nothing |

Naming a step in any other status is a `PreflightRefused`; naming a label the changeset lacks, or one
label in both, is a `ChangesetError`. Preferred practice: add a new step rather than edit one that has
been applied. Statements that cannot run inside a transaction (`CREATE INDEX CONCURRENTLY`) are
unsupported in v0.1: such a step fails and is recorded as a failed step.

**Nozdormu, baseline.** `baseline(connection, changeset, *, through=None, schemas=None)` adopts a database
that already holds a changeset's effects. It writes one `baseline` row per step from the first through the `through` step (default the last), in
changeset order, each with its own step's checksum and the same fingerprint hash, and executes nothing; from
then on every step up to and including `through` is `applied`, the later ones follow the normal rules, and a
step added before `through` later is `before_baseline`. It runs in one transaction under the advisory lock and is refused (`BaselineRefused`) when the changeset already has any ledger row.
The fingerprint is the hash of Infinite's fingerprint (below) of the given schemas; without `schemas` it
covers every schema except `pg_*`, `information_schema` and `caverns`, never an earlier baseline's set. A
named schema that does not exist is refused. The baseline also stores the fingerprint in
`caverns.fingerprint`, in the same transaction, once, at the version of the last baseline row (the `through`
step's), which makes its schemas the watched schemas.

**Infinite, the fingerprint.** `infinite.fingerprint(connection, *, schemas=None)` reads the shape of a
database's schemas from the system catalogs, in one read-only transaction that writes nothing and is always
rolled back, with `search_path` pinned to `pg_catalog` so that type, default and definition texts print the same
whatever the session's path. The document is keyed by schema, then by kind, then by object name; every schema
named appears with all six kinds, empty when it holds none, so an empty or dropped schema is visible:

| Kind | Key | Value |
|---|---|---|
| `table` | `<table>` | `{"kind": "r" \| "p" \| "f"}`: ordinary, partitioned or foreign |
| `column` | `<table>.<column>` | `{"type", "not_null", "default", "generated", "identity", "collation"}`: `format_type`; a boolean; the default expression or null; the generation expression or null; `"always"`, `"by default"` or null; the collation name when it differs from the type's default, else null. Columns of tables only |
| `index` | `<index>` | `pg_get_indexdef` |
| `constraint` | `<table>.<constraint>` | `pg_get_constraintdef`; table constraints of the kinds check, foreign key, primary key, unique and exclusion (a `NOT NULL` is the column's `not_null`) |
| `view` | `<view>` | `{"kind": "v" \| "m", "definition": pg_get_viewdef}`: view or materialized view; a view's shape is its definition |
| `function` | `<name>(<identity arguments>)` | `pg_get_functiondef`; functions and procedures |

An object that belongs to an extension is left out (an extension's table takes its indexes and constraints with
it). Not covered: triggers, sequences, types, grants, ownership, comments and extensions themselves. The hash is the
SHA-256 of the document's canonical JSON (sorted keys, no whitespace). Without `schemas` the fingerprint covers
the watched schemas (below); the result's `schemas` are sorted and unique.

**Watched schemas.** The schemas that `apply` fingerprints, `infinite.fingerprint` reads by default and
`drift` compares are those of the fingerprint recorded with the latest `baseline` ledger row, of any
changeset: the watched set is database-wide, and a later baseline of another changeset replaces it. Without
a baseline they are every schema except `pg_*`, `information_schema` and `caverns`, read again each time, so a
schema created later is watched from then on. A `baseline` without `schemas` takes that default, never the
set before it. `apply`'s signature has no schema argument. The fingerprint `apply` records covers the watched
schemas, the database as it now is; `drift` and the pre-apply warning compare the watched schemas plus those of
the latest fingerprint (below), so a schema dropped since shows as removed, with or without a baseline.

**Infinite, drift.** `infinite.drift(connection, changeset=None, *, schemas=None)` compares the live schemas with
the latest row of `caverns.fingerprint` and writes nothing. It reads in read-only transactions: with a
`changeset`, `preflight` runs first in its own, then one transaction reads the fingerprint, runs the checks and
reads the catalogs. The reference is always the latest fingerprint, whatever baseline it came from. The compared
schemas are the watched schemas plus those of the latest fingerprint, narrowed by `schemas` (one that is in
neither is a `ChangesetError`; a dropped one is accepted and shows its objects as removed); the
recorded document is restricted to them and the live document of them is read. Each object only in the live
one is `added`, only in the recorded one `removed`, and in both with another value `changed`, one
`Difference(kind, name, change)` each, sorted by schema, kind and key; `name` is `<schema>.<key>`, for example
`public.users.nickname` or `public.touch_updated()`. A watched schema the fingerprint lacks (created since)
shows all its objects as added; a dropped one, as removed, with or without a baseline. With a `changeset`, a step the ledger records as
`applied` whose check is now false is a disagreement between the ledger and the database, returned as its
`StepStatus`; a step without a check, or `unknown`, never is. The checks run before the catalogs are read, with
the session's `search_path`.

**The fingerprint table.** `caverns.fingerprint`, created by the own step `fingerprint`, holds one row per
apply run that writes ledger rows and per baseline:

| Column | Holds |
|---|---|
| `version` | primary key; the ledger version of the run's last row (a foreign key to `caverns.ledger`) |
| `schemas` | the schemas read (`text[]`, sorted) |
| `document` | the fingerprint's document (`jsonb`) |
| `hash` | its SHA-256 (64 lower-case hex digits) |
| `recorded_at` | `clock_timestamp()` at insert |

It is append-only like the ledger: triggers reject UPDATE, DELETE and TRUNCATE. Truncating the ledger is also
refused, by the foreign key, unless it cascades, and then by the ledger's trigger.

**Epochs, the registry.** Epochs are snapshots of opted-in tables, taken under a name and tagged with the ledger
version; the opt-in registry is `caverns.epoch_table`. `register(connection, schema, table, *,
key=None)` opts a table in, `unregister(connection, schema, table)` opts it out and `tracked(connection)` lists the
registry. A table is named by its exact PostgreSQL schema and table name: case-sensitive, never quoted or
folded by Caverns (PostgreSQL does the quoting), so `Odd Table` is that name and `odd table` another. The key is
the column list that identifies a row: by default the table's primary key in index order (its key columns only:
the `INCLUDE` columns of `PRIMARY KEY (a) INCLUDE (b)` are not part of the key, so it is `(a)`), else the columns
passed, in the order passed. `register` runs in one transaction under the advisory lock and refuses a table
that does not exist or is not a table (a view is not one), a key column the table lacks (a dropped column
included) and, without a `key`, a table with no primary key. Registering the same key again writes nothing and
returns the stored row; another key replaces the stored one (the row's time and user are the change's) and logs a
warning through the `caverns_of_time.epochs` logger. `unregister` deletes the registry row only: the epochs
already taken keep the table's rows. The registry is mutable configuration, so it has no trigger.

**Epochs, snapshot.** `snapshot(connection, name, *, lock_timeout=5.0)` copies every registered table into one new epoch: a row of
`caverns.epoch` (the name, the latest ledger version, the time, the user, the Caverns version), a row of
`epoch_source` per table (its key, its `columns` as a JSON list of name and type in column order, its row count) and
a row of `epoch_row` per source row (the key as a JSON object of the key columns' values, the row as `to_jsonb`
of it). It takes Caverns' advisory lock at session level and commits, then opens a REPEATABLE READ transaction, so
the snapshot is taken after the lock is held (a transaction-level lock statement would take the snapshot before it
waits) and all tables are read in one consistent view; the lock is released when the call ends. The epoch name
follows the label rules and is unique for good: a deleted epoch keeps its name. A refusal rolls everything back,
so nothing is written: no registered table, a name taken, a registered table or key column that no longer exists,
a key with duplicates or with nulls (nulls are checked first, so two rows with a null key report nulls, not duplicates;
only a key column's own null counts: an array key holding a null element is a valid key). The copy of a table is one statement PostgreSQL composes (hard rule 3):
`format` quotes the schema and table names and the key as literals from a template bound as a value, never built in
Python. A value's type is not in the row: JSON holds numbers, strings, booleans, nulls, arrays and objects, so
read a value's type from the epoch's `columns` (`Column.type`, printed by `format_type` with the search path
pinned to `pg_catalog`, so a user type is schema-qualified). The values are printed under settings the snapshot
pins for its transaction, so an epoch does not depend on the session that took it: `TimeZone` `UTC` (a timestamptz
ends `+00:00`), `IntervalStyle` `postgres`, `extra_float_digits` 1, `bytea_output` `hex`, `DateStyle` `ISO, MDY` and
`lc_monetary` `C`; the caller's own settings are untouched afterwards. If releasing the advisory lock fails
after the epoch is committed (an error, or `pg_advisory_unlock` returns false because the session did not hold the
lock), `snapshot` raises a `CavernsError` instead of returning: "epoch 'nightly' is taken, but releasing Caverns'
advisory lock failed (<reason>): Caverns needs a direct connection or a session-mode pooler, not a transaction-mode
one". The epoch is committed and stands. The session-level lock needs a connection that stays one session: connect
directly or through a session-mode pooler. `lock_timeout` (as for `apply`: seconds, default `5.0`, `None` keeps the
session's own) bounds the wait for Caverns' advisory lock, where a timeout raises `CavernsError`, and, set at the
start of the REPEATABLE READ transaction, the wait for each source table's lock: a timeout there rolls the copy
back and raises the driver's own error (SQLSTATE `55P03`), untranslated. `catalog(connection, *, limit=None)` lists the epochs
newest first, deleted ones included, each with its tables.

**Epochs, as of.** `as_of(connection, epoch, schema, table, *, limit=None)` reads one table of an epoch back: the
epoch, the table's entry in it and its rows ordered by the key columns in their declared order (an integer key
sorts as a number, so 2 comes before 10), at most `limit`. The columns are the epoch's own, in the order they had
then, whatever the live table has become; each row is a dict of the values as JSON holds them (their SQL types are
the epoch's `Column.type`). It reads in one read-only transaction and writes nothing. A deleted epoch is refused, and so is a table
the epoch does not hold (one registered after it was taken included).

**Epochs, diff.** `diff(connection, old, new, *, tables=None)` compares two epochs, by default every table either one
holds, else the `(schema, table)` pairs of `tables`. A table in one epoch only is reported as added (only in the new
epoch) or removed (only in the old), its rows not listed. A table in both is compared on the columns both epochs
have: its rows are matched by the declared key and listed as added or removed (their keys) or changed (the key and
the old and new values of the columns that differ, in column order); values are compared as JSON holds them, so a
column whose type changed counts as changed in a row only when its JSON value differs (`int` to `bigint` does not,
`text` `'7'` to `int` 7 does). A column added, removed or of another type (its two types as `format_type` prints
them) is reported as a column change, and a row that differs only in an added or a removed column is not a changed
row. A table whose key differs between the epochs, in a column name or in a key column's type, is refused
(`EpochRefused`, naming the table and both keys with their types, `(id integer)` and `(id text)`): leave it out with
`tables`. A table that does not differ is left out of the result. It reads in one read-only transaction, writes
nothing and ignores the live tables; a deleted epoch is refused. Rows come back ordered by the key columns in their
declared order, as in `as_of`.

**Epochs, retention.** Nothing is pruned automatically: every epoch is kept until it is deleted by name, and a
snapshot never deletes. `delete(connection, names)` deletes the epochs called *names*: for each it writes a tombstone
in `caverns.epoch_deletion` (the time, the user, the Caverns version), then deletes the epoch's `epoch_row` rows,
which the table's trigger allows only after the tombstone. The catalog row and the `epoch_source` rows stay, so
`catalog` and `chromie epochs` still list the epoch with its deletion time and its table sizes, and the name stays
taken for good (`snapshot` refuses it; `as_of` and `diff` refuse the epoch). It is all or nothing: one transaction
under the advisory lock resolves every name first, so an unknown or an already deleted name deletes nothing.
`delete(connection, names, dry_run=True)` validates and resolves the names the same way (same errors) in one
read-only transaction without the lock and returns the epochs that would be deleted, newest first, with `deleted_at`
None: nothing is written. A deletion is a tombstone, not a ledger row: the ledger is the schema timeline that `preflight` and Timewalking read.

**The epoch tables.** Created by the own step `epochs`, all in schema `caverns`:

| Table | Holds | Rule |
|---|---|---|
| `epoch_table` | the registry: `schema_name`, `table_name`, `key` (`text[]`, not empty), `registered_at`, `registered_by`; primary key (`schema_name`, `table_name`) | mutable, no trigger |
| `epoch` | the catalog: `epoch` (identity), `name` (unique), `ledger_version` (a foreign key to `caverns.ledger`), `taken_at`, `taken_by`, `caverns_version` | triggers reject UPDATE, DELETE and TRUNCATE |
| `epoch_source` | one row per table per epoch: `epoch`, `schema_name`, `table_name`, `key` (`text[]`), `columns` (`jsonb`), `row_count`; primary key (`epoch`, `schema_name`, `table_name`) | triggers reject UPDATE, DELETE and TRUNCATE |
| `epoch_row` | one row per source row: `epoch`, `schema_name`, `table_name`, `key` (`jsonb`), `row` (`jsonb`); primary key (`epoch`, `schema_name`, `table_name`, `key`); a deferred foreign key to `epoch_source` | triggers reject UPDATE and TRUNCATE, and DELETE unless the epoch has a tombstone |
| `epoch_deletion` | the tombstones: `epoch` (primary key, a foreign key to `epoch`), `deleted_at`, `deleted_by`, `caverns_version` | triggers reject UPDATE, DELETE and TRUNCATE |

Truncating `epoch` or `epoch_source` is also refused by the foreign keys unless it cascades, and then by the
triggers. `snapshot` writes the catalog, the sources and the rows; `delete` writes the tombstones and deletes the
rows of the epochs it deletes: nothing else updates or deletes in these tables.

**Chromie, the command line.** `chromie` is installed with the `cli` extra
(`pip install 'caverns-of-time[cli]'`), which brings its driver, psycopg 3; the library itself imports
none. The connection string is required, from `--dsn`, else the `CAVERNS_DSN` environment variable: with neither
the command exits 2. libpq fills what the string omits from the standard `PG*` variables, as every libpq
client does, so name the host and database in the string. Every command takes
`--format text|json` (default `text`). Results go to stdout, warnings and errors to stderr. Exit codes:
0 success, 1 a finding the command exists to report, 2 a usage or environment error (bad arguments, no
connection string, a refused connection, a Caverns error); any other error, a driver or OS one included, is
also exit 2, never a finding. Every command only reads except `delete-epoch`, which deletes epochs' rows and asks
first.

`chromie log [--changeset NAME] [--limit N]` shows the ledger, newest first, as a table (`--limit` defaults to
20, `0` shows every row; `--format json` is a list of objects with every ledger column, `recorded_at` as ISO 8601
with its offset, `[]` when there are none). An empty result is an information line, exit 0:

```text
VERSION  CHANGESET  LABEL     OUTCOME  RECORDED_AT (UTC)    BY
      2  app        1_schema  applied  2026-11-22 17:06:45  postgres
      1  caverns    ledger    applied  2026-11-22 17:06:45  postgres
```

`chromie status DIRECTORY [--changeset NAME]` loads the directory as a changeset (named after the directory unless
`--changeset` says otherwise) and prints each step's status with the version of the ledger row that decided it
(`-` when none). Exit 0 when every step is `applied`, 1 when any is not (an error line lists them), 2 on a
usage error such as a missing directory or a ledger that is not installed. `--format json` prints
`{"changeset": ..., "steps": [{"label", "status", "version"}]}` with the same exit codes:

```text
LABEL     STATUS   VERSION
1_schema  applied  2
2_table   pending  -
❌ 1 of 2 steps of changeset app not applied: 2_table (pending).
```

`chromie as-of EPOCH SCHEMA.TABLE [--limit N]` shows a table as it was at an epoch (`epochs.as_of`). `SCHEMA.TABLE`
is split at the first dot, both parts non-empty (`s.a.b` is schema `s`, table `a.b`). The text output is an
information line (the table, the epoch, its ledger version, when it was taken in UTC, the row count and the key)
and the rows as a table in the epoch's column order, `-` for a null, a boolean, number, list or object as compact
JSON; `--limit` defaults to 20 and `0` shows every row, and when fewer rows are shown than the epoch holds a last line
says so. `--format json` prints `{"epoch", "ledger_version", "taken_at" (ISO 8601 with its offset), "schema",
"table", "key", "columns": [{"name", "type"}], "row_count", "rows": [{column: value}]}`. Exit 0; 2 for an unknown
or a deleted epoch, a table the epoch lacks, a ledger that is not installed or a malformed `SCHEMA.TABLE`:

```text
ℹ️ public.users at epoch run-42 (ledger version 9, taken 2026-11-22 17:06:45 UTC): 2 rows, key (id).
id  email            nickname
1   ann@example.org  -
2   bob@example.org  bobby
```

```text
ℹ️ Showing 20 of 25 rows — --limit 0 shows all.
```

`chromie diff OLD NEW [--table SCHEMA.TABLE ...]` compares two epochs row by row (`epochs.diff`). `--table` (repeatable,
split at the first dot like `as-of`) narrows the comparison to those tables; a named table neither epoch holds is an
error. The text output is an information line naming both epochs and their ledger versions, then one line per
difference: `+` added, `-` removed, `~` changed, then its kind (`table`, `column` or `row`) and its name, table by table
(the table, its columns, then its rows added, removed and changed). A row is named by its table and its key as
compact JSON values, a changed row followed by the columns that differ, a column of another type by both types. A last
line counts the lines and the tables. Exit 0 when nothing differs, 1 when anything does, 2 on a usage or environment
error: an unknown or a deleted epoch, a table neither epoch holds, a table whose key (a name or a type) differs between the epochs,
a ledger that is not installed or a malformed `--table`. `--format json` prints `{"old": {"epoch",
"ledger_version"}, "new": {...}, "tables": [{"schema", "table", "change", "key": [...], "columns": [{"name",
"change", "old_type", "new_type"}], "added": [{key}], "removed": [{key}], "changed": [{"key", "old", "new"}]}]}`
with the same exit codes and no status lines:

```text
ℹ️ Comparing epoch run-41 (ledger version 9) with run-42 (ledger version 9).
~  column  public.codes.label: text → character varying(5)
~  row     public.codes (code="x"): label
-  column  public.users.tags
+  column  public.users.age
+  row     public.users (id=3)
-  row     public.users (id=1)
~  row     public.users (id=2): nickname
❌ 7 differences in 2 tables.
```

With nothing found: `✅ No differences between epochs run-41 and run-42.`

`chromie epochs [--limit N]` lists the epochs, newest first, deleted ones included (`epochs.catalog`): the epoch
number, name, ledger version, when it was taken in UTC and by whom, the number of tables and rows it holds (a deleted
epoch keeps these counts, its rows are gone) and when it was deleted (`-` when it was not). `--limit` defaults to 20
and `0` shows every epoch. `--format json` prints a list, `[]` when there are none, of `{"epoch", "name",
"ledger_version", "taken_at" (ISO 8601 with its offset), "taken_by", "caverns_version", "deleted_at" (ISO 8601 or
null), "tables": [{"schema", "table", "key", "columns": [{"name", "type"}], "row_count"}]}`. No epoch is an
information line. Exit 0; 2 for a ledger that is not installed:

```text
EPOCH  NAME    LEDGER  TAKEN_AT (UTC)       BY        TABLES  ROWS  DELETED (UTC)
    3  run-42       9  2026-11-22 17:06:45  postgres       2  1204  -
    2  run-41       9  2026-11-21 17:06:45  postgres       2  1190  2026-11-22 18:00:00
    1  run-40       7  2026-11-20 17:06:45  postgres       1    12  -
```

`chromie delete-epoch EPOCH [EPOCH ...] [--dry-run] [--yes]` deletes the named epochs and their rows (`epochs.delete`;
the catalog rows and the names stay). An epoch that is unknown or already deleted is a usage error and nothing is
deleted: the plan, for `--dry-run` and the confirmation alike, comes from `epochs.delete(..., dry_run=True)`, so the
error is the library's. Without `--yes` it asks on stderr, `Delete 2 epochs (run-42, run-40) and their 1216 rows? [y/N]`, and
only `y` or `yes` goes on: any other answer, the end of input or Ctrl-C prints `ℹ️ Aborted — no changes were made.` and exits 0; when stdin is
not a terminal it refuses without `--yes` (exit 2), so an unattended run must pass `--yes`. `--dry-run` deletes
nothing and asks nothing: it prints one line per epoch, newest first, and `ℹ️ Dry run — nothing deleted.`. A deletion prints one
`✅ Deleted epoch NAME (N rows in M tables).` line per epoch, newest first. `--format json` prints the list of epochs
in the shape of `chromie epochs` (those that would be deleted with `--dry-run`, with `deleted_at` null; those deleted
otherwise, with `deleted_at` set) and no status lines. Exit 0; 2 for an unknown or an already deleted epoch, no
confirmation possible, a ledger that is not installed:

```text
ℹ️ Would delete epoch run-40 (12 rows in 1 table, taken 2026-11-20 17:06:45 UTC).
ℹ️ Dry run — nothing deleted.
```

`chromie drift [DIRECTORY] [--changeset NAME] [--schema NAME ...]` compares the live schema with the last
recorded fingerprint (`infinite.drift`). `DIRECTORY` is optional: given, its changeset's applied steps whose
check is now false are reported too (`--changeset` names it as for `status`; without a `DIRECTORY` it is a usage
error). `--schema` (repeatable) narrows the report to those watched schemas. Each difference is a line, `+`
added, `-` removed, `~` changed, then its kind and name; a disagreement is `~  step`. Exit 0 when nothing differs,
1 on drift (an error line counts them), 2 on a usage error: no fingerprint recorded yet, a ledger that is not
installed, an unwatched schema, a missing directory. `--format json` prints
`{"fingerprint_version", "schemas", "differences": [{"kind", "name", "change"}], "disagreements":
[{"changeset", "label", "version"}]}` with the same exit codes and no status lines:

```text
ℹ️ Comparing with the fingerprint at ledger version 4 (schemas: public).
+  column  public.users.nickname
~  step    app/2_table: recorded applied, check now false
❌ Drift: 2 differences.
```

With nothing found: `✅ No drift — the live schema matches the fingerprint at ledger version 4.`

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
- Every write (`install`, `apply`, `baseline`, `register`, `unregister`, `snapshot` and `delete` without `dry_run`)
  also refuses, with a `CavernsError` and before it writes anything, a database that is a standby
  (`pg_is_in_recovery()`: "the database is a standby (in recovery): Caverns writes only to a primary") and a
  session that cannot write (`transaction_read_only` is `on`, as when `default_transaction_read_only` is set:
  "the session is read-only (default_transaction_read_only is on): Caverns needs a session that can write"). The
  read-only functions (`preflight`, `history`, `drift`, `fingerprint`, `tracked`, `catalog`, `as_of`, `diff`
  and `delete` with `dry_run`) are not checked.
- `install(connection) -> list[LedgerEntry]`: the rows written, one per own step not yet applied, `[]` when
  Caverns is already installed; an `accepted` row for an own step whose table exists without its row. Raises
  `CavernsError` for a `caverns` schema or relation owned by a role the current role is not a member of.
- The ledger, `caverns.ledger`, one row per recorded event:

  | Column | Holds |
  |---|---|
  | `version` | identity; the ledger version, the order of events |
  | `changeset`, `label` | the step the row is about |
  | `checksum` | the step's checksum (64 lower-case hex digits) |
  | `outcome` | `applied`, `failed`, `baseline` or `accepted` |
  | `fingerprint` | required on `baseline`, else null |
  | `error` | on `failed` only: the SQLSTATE and the primary message (`<SQLSTATE>: <message>`), never the DETAIL, HINT or CONTEXT; an error without a SQLSTATE keeps the first line of its text. Step SQL must not carry secrets: a primary message can quote a literal of the failing statement. |
  | `duration_ms` | how long the step ran, when it ran |
  | `recorded_at`, `recorded_by` | `clock_timestamp()` and `current_user` at insert |
  | `caverns_version` | the package version that wrote the row |

  An index on (`changeset`, `label`, `version`) serves the reads. The ledger is append-only: triggers
  reject UPDATE, DELETE and TRUNCATE (hard rule 5); a correction is a new row. `LedgerEntry` carries these
  columns in this order.
- `preflight(connection, changeset) -> list[StepStatus]` and `Status` (`applied`, `pending`, `unknown`,
  `conflict`, `before_baseline`). A check that does not return exactly one row with one boolean column, or raises a database
  error, is a `ChangesetError` naming the step; so is a `baseline` row at a step the changeset lacks.
  `LedgerNotInstalled` is raised when Caverns' own changeset is not fully applied (call `install` first,
  also after an upgrade that added an own step).
  `PreflightRefused` carries `statuses`; `StepFailed` carries `label` and `version`.
  The changeset name `caverns` is refused with a `ChangesetError`: the public functions take a consumer's
  changeset only.
- `history(connection, *, changeset=None, limit=None) -> list[LedgerEntry]`: the ledger rows, newest first
  (by version); only *changeset*'s when given (Caverns' own `caverns` included: reading is allowed), at most
  *limit* rows (`None`: all). It runs in one read-only transaction and writes nothing. Raises
  `LedgerNotInstalled` when the ledger table does not exist, and `ChangesetError` for an invalid changeset name or a
  *limit* that is not an integer from 1 to 9223372036854775807, the bigint maximum (a `bool` is not one).
  `chromie log --limit` takes 0 to the same maximum.
- `apply(connection, changeset, *, reapply=(), accept=(), lock_timeout=5.0) -> list[LedgerEntry]`: the rows written, in order
  (`[]` when nothing is to do). A bare string for `reapply` or `accept`, a label in both, or a `lock_timeout` that is not
  `None` or a positive number (a `bool` is not one) is a `ChangesetError`; a wait for Caverns' lock that times out is a `CavernsError`.
  Raises `PreflightRefused` (attribute `statuses`, the offending steps), `StepFailed` (attributes `label`
  and `version`, the failed row's ledger version; its `__cause__` is the driver's error), a plain
  `CavernsError` (its `__cause__` is the step's error) when a failed step cannot be recorded, and the
  errors of `preflight`. A run that writes rows stores a fingerprint and may log drift warnings (see apply).
- `baseline(connection, changeset, *, through=None, schemas=None) -> LedgerEntry`: the `through` step's row (one
  row is written per step up to it). Raises
  `BaselineRefused` (the changeset has rows; a named schema that does not exist) and `ChangesetError` (a
  `through` the changeset lacks, a bare string for `schemas`, an empty `schemas`). It stores the fingerprint.
- `infinite.fingerprint(connection, *, schemas=None) -> Fingerprint`: `Fingerprint(schemas, document, hash)`, a
  frozen dataclass of the sorted, unique schema names (a tuple), the document and its SHA-256 hex; without
  `schemas`, the watched ones. It runs in one read-only transaction and writes nothing. Raises `ChangesetError` for a bare string, a name that is not a string
  or an empty collection for `schemas`, and for a named schema that does not exist; the connection is checked as
  for every public function.
- `infinite.drift(connection, changeset=None, *, schemas=None) -> Drift`: `Drift(fingerprint_version, schemas,
  differences, disagreements)`, a frozen dataclass of the ledger version of the fingerprint compared with, the
  schemas compared (a tuple), the `Difference(kind, name, change)` values (`kind` one of the six kinds, `change`
  `added`, `removed` or `changed`) and the `StepStatus` values of the disagreements, each a tuple. Read-only
  transactions, nothing written. Raises `LedgerNotInstalled` before
  `install()` (the fingerprint table does not exist; given a *changeset*, also before the `epochs` step, by its
  `preflight`), `NoFingerprint` when none is recorded, `ChangesetError`
  for a bad `schemas` or one that is not watched, and the errors of `preflight` for a `changeset`.
- `epochs.register(connection, schema, table, *, key=None) -> TrackedTable`: opts the table in and returns its
  registry row as stored. *schema* and *table* are non-empty strings, exact names; *key* is None (the primary key) or a
  non-empty sequence of distinct column names, never a bare string. Raises `ChangesetError` for a bad argument,
  `LedgerNotInstalled` when the epoch tables do not exist (call `install()`, also after an upgrade) and
  `EpochRefused` for a table that is missing or not a table, a key column it lacks, or no `key` without a primary key.
  The arguments are checked before the database is asked.
- `epochs.unregister(connection, schema, table)`: removes the registry row, past epochs untouched. Raises
  `ChangesetError`, `LedgerNotInstalled` and `EpochRefused` (the table is not registered).
- `epochs.tracked(connection) -> list[TrackedTable]`: the registry ordered by schema and table, in one read-only
  transaction that writes nothing. Raises `LedgerNotInstalled`.
- `epochs.snapshot(connection, name, *, lock_timeout=5.0) -> Epoch`: copies every registered table into a new epoch called *name* (a
  label-style name, unique for good). Raises `ChangesetError` for a bad name or `lock_timeout`, `CavernsError` when Caverns' lock times out, `LedgerNotInstalled` and `EpochRefused`
  (no registered table, a name taken, a registered table or key column that is gone, a key with duplicates or
  nulls); a refusal writes nothing. Logs `took epoch NAME at ledger version V: N rows in M tables`.
- `epochs.catalog(connection, *, limit=None) -> list[Epoch]`: the epochs newest first, deleted ones included, at
  most *limit* (None: all) in one read-only transaction that writes nothing. Raises `ChangesetError` for a *limit*
  that is not a positive integer up to 9223372036854775807 and `LedgerNotInstalled`.
- `epochs.delete(connection, names, *, dry_run=False) -> list[Epoch]`: deletes the epochs called *names* (a non-empty collection of valid
  names, never a bare string; duplicates collapse): a tombstone, then the epoch's `epoch_row` rows. The catalog and
  source rows stay and the name stays taken. One transaction under the advisory lock, every name resolved before
  anything is written: all or nothing. Returns the deleted epochs re-read in one query (`deleted_at` set), newest
  first. With *dry_run* the same validation and errors run in a read-only transaction without the lock, and the epochs
  that would be deleted are returned (`deleted_at` None) with nothing written. Raises
  `ChangesetError` for a bad *names*, `LedgerNotInstalled`, `EpochNotFound` (an unknown epoch) and `EpochRefused` (an
  already deleted epoch). Logs `deleted epoch NAME: N rows in M tables` per epoch.
- `epochs.as_of(connection, epoch, schema, table, *, limit=None) -> TableAt`: the table as it was at the epoch named
  *epoch*, rows ordered by key, at most *limit* (None: all) in one read-only transaction that writes nothing. Raises
  `ChangesetError` for a bad argument (a *limit* that is not a positive integer up to 9223372036854775807 included),
  `LedgerNotInstalled`, `EpochNotFound` (no such epoch, or the epoch lacks the table) and `EpochRefused` (a deleted
  epoch).
- `epochs.diff(connection, old, new, *, tables=None) -> EpochDiff`: the difference between the epochs named *old* and
  *new* (the same name is allowed: no difference), in one read-only transaction that writes nothing. *tables* is None
  (every table of either epoch) or a non-empty collection, never a bare string, of `(schema, table)` pairs of
  non-empty strings; duplicates collapse. Raises `ChangesetError` for a bad argument, `LedgerNotInstalled`,
  `EpochNotFound` (an unknown epoch, or a named table neither epoch has) and `EpochRefused` (a deleted epoch, or a
  table whose key, a column name or a key column's type, differs between the epochs).
- `EpochDiff(old, new, tables)`: a frozen dataclass of the two `Epoch` values and a tuple of the `TableDiff` values of
  the tables that differ, ordered by schema and table (empty: no difference). `TableDiff(schema, table, change, key,
  columns, added, removed, changed)`: `change` is `added` or `removed` (a table in one epoch only: `columns`, `added`,
  `removed` and `changed` empty) or `changed`; `key` the key columns; `columns` a tuple of `ColumnChange(name, change,
  old_type, new_type)` (`change` `added`, `removed` or `changed`; the type the column lacks is None); `added` and
  `removed` tuples of key dicts in declared key order; `changed` a tuple of `RowChange(key, old, new)`, the dicts of the
  columns that differ in column order, values as JSON holds them. All frozen.
- `TableAt(epoch, table, rows)`: a frozen dataclass of the `Epoch`, its `EpochTable` and a tuple of row dicts keyed
  by the epoch's column names in column order, values as JSON holds them.
- `Epoch(epoch, name, ledger_version, taken_at, taken_by, caverns_version, deleted_at, tables)`: a frozen dataclass;
  `deleted_at` is None until the epoch is deleted, `tables` a tuple of `EpochTable` ordered by schema and table.
  `EpochTable(schema, table, key, columns, row_count)`: `key` a tuple of column names, `columns` a tuple of
  `Column(name, type)` in column order. All frozen.
- `TrackedTable(schema, table, key, registered_at, registered_by)`: a frozen dataclass; `key` is a tuple of column names.
- The public names are `Connection`, `LedgerEntry`, `Status`, `StepStatus`, `Step`, `Changeset`, `Fingerprint`,
  `Difference`, `Drift`, `TrackedTable`, `Epoch`, `EpochTable`, `Column`, `TableAt`, `EpochDiff`, `TableDiff`, `ColumnChange`, `RowChange` and the functions above; the exceptions, all in `caverns_of_time.errors`, are
  `CavernsError` (the base), `ChangesetError`, `LedgerNotInstalled`, `PreflightRefused`, `StepFailed`,
  `BaselineRefused`, `NoFingerprint`, `EpochRefused` and `EpochNotFound`.
- The own changeset is never edited once released: a change to the ledger, the fingerprint table or the epoch
  tables is a new own step (hard rule 6). The DDL of each step is idempotent, and `caverns` is a reserved changeset name.
- No runtime dependency for the library; the CLI's driver (psycopg 3) is the optional `cli` extra. Adding
  a dependency is an ask-first decision.

## Known limitations

- Caverns writes only to a primary: a standby (in recovery) and a read-only session are refused.
- Logical replication of the `caverns` schema is unsupported: the ledger, fingerprint and epoch tables are not
  designed to be replicated, and a subscriber's copy is not a ledger Caverns can write.

## Extending

Follow the adding-a-component workflow of the development skill
(`.claude/skills/caverns-of-time-development/references/workflows/adding-a-component.md`).

## See also

`ROADMAP.md` · `.claude/skills/caverns-of-time-development/references/architecture.md`
