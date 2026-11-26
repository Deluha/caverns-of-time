# Release Notes

What shipped, newest first. What is planned: `ROADMAP.md`.

## v0.1.0 — in development

### Phase 5 — Murozond

The destructive-change guard, and what a run does about drift:

- `murozond.classify(sql)` returns a `Classification`: the worst `Risk` (`destructive`, `lock_hazard`, `unknown` or
  `safe`) and a `Finding(risk, statement, reason)` per risky statement or `ALTER TABLE` action. Rules in brief:
  every `DROP`, `TRUNCATE`, `DELETE` and `ALTER TABLE ... DROP` is destructive; `CREATE INDEX` without
  `CONCURRENTLY`, `ADD CONSTRAINT` without `NOT VALID`, `ALTER COLUMN TYPE` and `SET NOT NULL` are lock hazards;
  a `COPY` of a `DELETE` or `MERGE` query is destructive and `COPY ... PROGRAM` is unknown; `DO`, `CALL`, `SELECT`
  and any statement it does not know are unknown. There is no SQL parser: a lexer reads keywords, and a finding's reason is fixed text, never quoted SQL.
- The guard on `apply`: `apply(..., acknowledge=())` refuses with `GuardRefused` (a `PreflightRefused`) every step that
  would run and is not safe unless its label is named. The refusals come in order, all before anything is written:
  status, an invalid `acknowledge`, the guard, drift. Caverns' own changeset is exempt; `preflight` is unchanged.
- `caverns.acknowledgement` (own step `acknowledgement`, append-only) records each acknowledged step that ran, with its
  risk and findings, never its SQL.
- `chromie status` gains a `RISK` column (`destructive (DROP TABLE)`), a warning listing the steps that would need an
  acknowledgement, and `risk` and `findings` keys in its JSON. Exit codes are unchanged.
- Non-transactional steps: a step whose SQL starts with `-- caverns:no-transaction` (`Step.transactional` is
  `False`) runs outside a transaction, for `CREATE INDEX CONCURRENTLY` and the like. It holds exactly one statement and
  takes Caverns' lock for the session, so it needs a direct or session-mode connection. A failure leaves its partial
  effect, and Caverns logs a warning naming the invalid indexes it may have left.
- The fingerprint reads four more kinds: triggers, row-level-security flags, policies and enum labels. A fingerprint
  recorded earlier is compared only on the kinds it holds, so it reports no drift for the new ones.
- `apply(..., on_drift="refuse")`: a difference between the live schema and the last fingerprint now refuses the run
  with `DriftRefused`, nothing written (a behaviour change: `apply` used to only warn). `on_drift="warn"` logs it,
  records it in the new append-only `caverns.drift` (own step `drift`) and proceeds.
- **Upgrade:** run `install()` once, as the schema's owner: it adds the own steps `acknowledgement` and `drift`; until
  then `preflight`, `apply` and `baseline` raise `LedgerNotInstalled`. Grant `SELECT, INSERT` on
  `caverns.acknowledgement` and `caverns.drift` to runner roles; a runner without the `caverns.drift` grant cannot
  record a failure in a warn run. A run that used to pass with logged drift now refuses until it is run once with
  `on_drift="warn"`. Step SQL using `SELECT` or `CREATE INDEX` without `CONCURRENTLY` now needs `acknowledge`, and so
  does any other step that is not safe.

### Phase 4 — Hardening

What a second review found in Phases 1–3, fixed before new components:

- A step added at or before the baseline's step has its own status, `before_baseline` (`Status.BEFORE_BASELINE`);
  `apply` refuses the run until `reapply` or `accept` acknowledges it, and `chromie status` shows it and exits 1.
  `baseline` now writes one `baseline` row per step up to `through`, each with its step's checksum.
- A `failed` row's `error` is the SQLSTATE and the primary message (`<SQLSTATE>: <message>`), no longer the whole
  error: step SQL must not carry secrets.
- `apply(..., lock_timeout=5.0)` and `epochs.snapshot(..., lock_timeout=5.0)`: a wait for a lock gives up after
  5 seconds; the run bounds its wait for Caverns' lock until it has written a row (a timeout is a `CavernsError`,
  nothing written), then its later waits for it have no timeout, so a run is never stopped between its rows and its
  fingerprint. The failure record's wait is never bounded, even before any row; every step's own lock waits are
  bounded. With `None`, every wait is left to the session's own `lock_timeout`.
- Privileges: `install` refuses a `caverns` schema or relation owned by a role whose privileges the current role does not
  inherit, and the new own step `privileges` revokes every privilege of `PUBLIC` on the schema, its tables, sequences and
  functions. The grants a runner role needs are documented; the append-only triggers guard against mistakes, not
  against the schema's owner or a superuser.
- Every write is refused with a `CavernsError` on a standby and in a read-only session. A failed release of the
  snapshot's session lock, or one that finds the lock not held (a transaction-mode pooler), raises a `CavernsError`
  (the epoch stands) instead of a warning.
- The fingerprint is read under pinned output and definition settings and records the server's version: the own
  step `fingerprint_server` adds `caverns.fingerprint.server_version` and `Fingerprint.server_version`. A hash
  recorded under default settings is unchanged.
- `epochs.snapshot` refuses, before copying a row, a table with row-level security the role does not bypass (a
  superuser, `BYPASSRLS`, or the owner without `FORCE`), estimates the registered tables' size, logs it and refuses
  above `max_bytes` (default 1 GiB, `None` for no limit), and refuses a key too large for an index entry (measured
  uncompressed, so a long but compressible key is refused too); the README states the cost of its long REPEATABLE
  READ transaction.
- Steps and checks run under the caller's `search_path`, which is not pinned: schema-qualify them.
- Known limitations: Caverns writes only to a primary, logical replication of the `caverns` schema is unsupported,
  and the snapshot's session lock needs a direct or session-mode connection.
- **Upgrade:** a database installed by Phase 3 code must run `install()` again, which adds the `privileges` and
  `fingerprint_server` steps; until then `preflight`, `apply` and `baseline` raise `LedgerNotInstalled`, and so does
  `drift`. A changeset baselined by earlier code has a single baseline row, so its earlier steps now read
  `before_baseline`: acknowledge them with `accept`. `apply` and `snapshot` now give up after 5 seconds waiting for a
  lock (pass `lock_timeout=None` for the old behaviour), and a snapshot above 1 GiB now needs `max_bytes`.

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
