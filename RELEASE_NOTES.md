# Release Notes

What shipped, newest first. What is planned: `ROADMAP.md`.

## v0.1.0 — in development

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

No product code. The development process is ported from Project Acme and adapted to this
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
