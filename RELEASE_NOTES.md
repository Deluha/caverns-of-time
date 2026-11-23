# Release Notes

What shipped, newest first. What is planned: `ROADMAP.md`.

## v0.1.0 — in development

### Phase 3 — Epochs

Run-level snapshots of opted-in tables:

- `epochs.register` / `unregister` / `tracked`: the opt-in registry, keyed by the primary key or a declared key.
- `epochs.snapshot(connection, name)`: copies every registered table into one named epoch, tagged with the
  ledger version, in one consistent REPEATABLE READ snapshot taken under Caverns' advisory lock; a key with
  duplicates or nulls refuses the whole snapshot. Rows are stored as jsonb with the epoch's own column list,
  their values printed under pinned settings (`TimeZone` UTC, `IntervalStyle` `postgres`, `extra_float_digits` 1,
  `bytea_output` hex, `DateStyle` `ISO, MDY`, `lc_monetary` `C`), so an epoch does not depend on the session that took it.
- `epochs.catalog`, `epochs.as_of` (a table as an epoch recorded it) and `epochs.diff` (columns added,
  removed or retyped; rows added, removed or changed by the key, compared on the columns both epochs have).
- Retention: nothing is pruned automatically; `epochs.delete` writes a tombstone and removes an epoch's rows,
  keeping its catalog row and its name. `epochs.delete(..., dry_run=True)` returns what would be deleted.
- `chromie epochs`, `chromie as-of EPOCH SCHEMA.TABLE`, `chromie diff OLD NEW [--table ...]` (exit 1 on a
  difference) and `chromie delete-epoch EPOCH ... [--dry-run] [--yes]`, the first command that writes.
- Caverns' own changeset gains the step `epochs`: `caverns.epoch_table`, `epoch`, `epoch_source`, `epoch_row`
  and `epoch_deletion`, append-only by trigger except the registry and the rows of a deleted epoch.
- New errors `EpochNotFound` and `EpochRefused`.
- **Upgrade:** a database installed by Phase 2 code must run `install()` again, which adds the `epochs` step;
  until then `preflight`, `apply` and `baseline` raise `LedgerNotInstalled`, and so does `drift` given a changeset
  (its `preflight` runs first); `drift` without one checks only `caverns.fingerprint`.

### Phase 2 — Chromie and Infinite

The command line and the drift detector:

- `chromie`, installed with the `cli` extra (psycopg 3; the library still has no runtime dependency).
  The connection string is `--dsn`, else `CAVERNS_DSN`; `--format text|json`; exit codes 0 success,
  1 a finding, 2 a usage or environment error.
- `chromie log` (the ledger, newest first, `--changeset`, `--limit`), `chromie status DIRECTORY` (each
  step's status) and `chromie drift [DIRECTORY]` (live schema against the last fingerprint, `--schema`).
- `nozdormu.history`: the ledger rows, newest first, optionally of one changeset and limited.
- Infinite: `infinite.fingerprint` reads six object kinds (tables, columns, indexes, constraints, views,
  functions) from the catalogs; `infinite.drift` returns a `Drift` of `Difference` values and, given a
  changeset, applied steps whose check is now false; `NoFingerprint` when none is recorded.
- Caverns' own changeset gains the step `fingerprint`, creating the append-only `caverns.fingerprint`.
  One fingerprint is stored per apply run that writes ledger rows and per baseline. `apply` logs a
  warning per difference before its first step when the live schema drifted from the last fingerprint.
  The watched schemas are those of the latest baseline's fingerprint, else every schema but the system's
  and Caverns' own, read again each time; a baseline without `schemas` takes the default.
- The baseline's fingerprint is redefined: the hash of Infinite's wider fingerprint.
- **Upgrade:** a database installed by Phase 1 code must run `install()` again, which adds the
  `fingerprint` step; until then `preflight`, `apply`, `baseline` and `drift` raise `LedgerNotInstalled`.

### Phase 1 — Nozdormu

The migration ledger, as a library (no CLI yet; that is Phase 2):

- `Step` and `Changeset` (from a list or a directory of `.sql` files, with optional `.check.sql` checks);
  a SHA-256 checksum over the normalised SQL.
- The ledger, `caverns.ledger`: append-only (triggers reject UPDATE, DELETE and TRUNCATE), created by
  Caverns' own changeset through `install(connection)` and recorded as its first row.
- `preflight`: every step is `applied`, `pending`, `unknown` or `conflict`, decided by the ledger first
  and then by the step's check, in a read-only transaction.
- `apply`: one transaction and one ledger row per step under an advisory lock; a failure is rolled back,
  recorded as a `failed` row and raised as `StepFailed`; a `conflict` or `unknown` step refuses the run
  unless acknowledged with `reapply` or `accept`.
- `baseline`: adopts an existing database with one `baseline` row and a schema fingerprint.
- Any DB-API 2.0 connection with autocommit off (psycopg 3 and psycopg2 are tested); the package imports no
  driver and has no runtime dependency. Errors are in `caverns_of_time.errors`.
- `psycopg2-binary` joins the `dev` extra; the database tests run once per driver.

### Phase 0 — Development process

No product code. The development process is ported from an existing project and adapted to this
repository:

- `CLAUDE.md`: four operating principles, nine hard rules, the ask-first list.
- Agents (`implementer`, `reader`, `reviewer`, `scribe`), the session-start and post-edit hooks,
  `.claude/settings.json`, three path rules.
- The development skill: a routing table over six references and three workflows, ten guidance evals,
  and the command skills `verify`, `review-diff`, `db-change`, `guidance-change`.
- Package skeleton (`caverns_of_time`, version only), `dev_tools/` with the verify command and the
  conventions, guidance-integrity and docs-sync checkers, and their tests.
- A PostgreSQL 18 test instance for every environment: Docker locally and in cloud sessions, a service
  container in CI, a compose service in the dev container. The verify command and CI require it.
- Pre-commit hooks, gitleaks, and three CI workflows (pre-commit, docs sync, tests), each with one check
  to require on `main`; mypy is strict and gating.
