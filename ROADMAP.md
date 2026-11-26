# Caverns of Time — Roadmap

The planned scope of the next release. `RELEASE_NOTES.md` records what shipped; this file records
what is planned. A release's section leaves this file when the release is tagged. Owner decisions are dated.

Approved by the owner on 2026-11-22.
Amended by the owner on 2026-11-25 after two reviews against the code of Phases 0–3 (decisions R1–R31 below).

## v0.1 — A recorded timeline for PostgreSQL

Goal: a generic Python library and CLI that gives a PostgreSQL database a recorded past — which schema
changes were applied and when, what opted-in tables looked like at a past run, and whether anything
changed outside the timeline. The first consumer is not the design target.

### Components

| Name | Lore | Role |
|---|---|---|
| **Nozdormu** | Aspect of Time, keeper of the one true timeline | Migration ledger: ordered, checksummed, append-only record of every applied step; preflight and apply. |
| **Chromie** | The guide who takes travellers to past timelines | CLI and query API: `log`, `status`, `drift`, `epochs`, `as-of`, `diff`, `delete-epoch`, `show`, `timewalk`, and the write commands `install`, `apply`, `baseline`. |
| **Epochs** | The sands of the hourglass | Run-level snapshots of opted-in tables, each tagged with the ledger version it was taken at. |
| **Infinite** | The Infinite Dragonflight, who alter the timeline | Drift detector: live schema versus what the ledger says it should be. |
| **Murozond** | Nozdormu's corrupted future | Destructive-change guard: flags DROPs, column drops and data deletion in a changeset before apply. |
| **Timewalking** | Replaying the past | Rebuilds the schema at a past ledger version in a scratch database, from the baseline's schema dump and the archived SQL of each step. |

### Scope rules (owner decisions, 2026-11-22)

- **Generic core (D1).** No consumer's name, schema, table or convention enters the package. A consumer
  integrates through an adapter that lives in the consumer's repository. Extended by the owner,
  2026-11-23: no consuming project is named anywhere in the repository.
- **Run-level snapshots (D2).** History is captured as Epochs: whole-table snapshots taken on demand.
  No row-level triggers in v0.1.
- **Wrap, not replace (D3).** Caverns defines its own `Step` (label, SQL or file, optional check) and
  accepts a changeset from a Python list or a directory of files. A consumer's existing runner keeps its
  flow and calls Nozdormu for apply and record. The ledger starts at adoption with one baseline entry.
- **Real database in tests (D4).** Database behaviour is tested against a real PostgreSQL, never a mock:
  a throwaway service container in CI, a local instance in development. The done gate requires the
  database tests to have run; a skipped database test fails the gate.
- **PostgreSQL 18.** CI tests against PostgreSQL 18. No version matrix until a second project adopts the
  tool.
- **Never a production database.** No test, workflow or development session connects to a consumer's
  live database. Applying to a live database is always the consumer owner's own run.
- **Delivery.** One pull request per phase, several commits each. Each phase lists what the owner
  confirms before its code is written.

### Review amendments (owner decisions, 2026-11-25)

| # | Item | Decision |
|---|---|---|
| R1 | Step archive | The ledger stores a step's checksum, never its SQL, so a replay has nothing to run. A new own table archives the SQL of each applied step, keyed by checksum, append-only, written by `apply`; the ledger is unchanged (the pattern of C6). A step applied before the archive exists replays only from a supplied changeset whose checksum matches. Phase 6. |
| R2 | Baseline schema dump | The fingerprint cannot regenerate a schema (no column order, sequences, types, triggers or partition bounds). The consumer supplies a schema dump as a file; Caverns stores it with the baseline and never runs `pg_dump` itself. Phase 6. |
| R3 | Scratch database | Timewalking refuses a scratch target that is the source database or is not empty. Who creates the scratch database is a Phase 6 sign-off item. |
| R4 | Drift is recorded | Today `apply` logs drift as a warning and then records a fingerprint that absorbs it, leaving no trace. The differences found before an apply are recorded in a new append-only own table. An `on_drift` choice of refuse (*proposed default*, R25) or warn on `apply` is a Phase 5 sign-off item. Phase 5. |
| R5 | Murozond sign-off widened | The acknowledgement parameter on `apply` and `preflight`, the `chromie status` output and exit code for a destructive or unknown step, the classifier approach without a SQL parser (no runtime dependency) and whether an acknowledgement is recorded join the Phase 5 sign-off. |
| R6 | Phase split | Old Phase 4 becomes Murozond and Timewalking, one pull request each (Phases 5 and 6 after R13); adoption and release follow the write commands. |
| R7 | Write commands | New Phase 7: `chromie install`, `chromie apply DIRECTORY` and `chromie baseline`, so a project with a directory of SQL files adopts without writing Python. Amends C2 ("all read-only") and E6; D3 stands: the library API stays the integration point for a consumer with its own runner. `snapshot`, `track` and `untrack` commands are a Phase 7 sign-off item. |
| R8 | `chromie show VERSION` | Prints the archived SQL a ledger row ran. Phase 6. |
| R9 | Release readiness | Phase 8 gains a commit: the `py.typed` marker, classifiers, a release workflow, CI on every supported Python version (not the PostgreSQL matrix, which stays deferred), the database privileges Caverns needs, a known-limitations section and a stability statement for the JSON output. |
| R10 | Non-transactional statements | Superseded by R24: steps that cannot run in a transaction join v0.1. |
| R11 | Timeouts | Superseded by R20: `apply` sets a default `lock_timeout`. |
| R12 | Later list | Epochs extras, wider fingerprint coverage with `drift --accept`, and ledger tamper evidence join Later (listed there; R26 pulls part of the coverage into v0.1). |
| R13 | Hardening phase | A second review (migration tooling, PostgreSQL operations, library engineering) found hazards in what Phases 1–3 built. New Phase 4 fixes them before new components are added; the later phases shift by one. |
| R14 | Step before the baseline | A step added at or before the baseline's step is today treated as applied and never runs. Preflight reports it with its own status and `apply` refuses until it is acknowledged. Amends N3. Phase 4. |
| R15 | Recorded error | A `failed` row keeps the driver's whole message for good, and a failing statement can quote a secret literal. The row keeps the error code and the primary message only; the docs state that step SQL must not carry secrets. Phase 4. |
| R16 | Row-level security | A snapshot of a table under row-level security copies only the rows its role sees and records success. Refused unless the role bypasses row security (*proposed*; recording the fact per table is the alternative). Phase 4. |
| R17 | Session lock release | The snapshot's session advisory lock ignores a failed unlock, which a transaction-mode connection pooler can cause. The result is checked and raised; a session-mode pooler is a documented requirement. Phase 4. |
| R18 | Fingerprint settings | The fingerprint is read with `search_path` pinned but not the output settings, so a definition can read differently between sessions or minor versions. It is read under the pinned settings of E8 and records the server version. Phase 4. |
| R19 | Own-step checksums | Only the `ledger` step's checksum is pinned by a test. Every own step's checksum is pinned, so an accidental edit cannot make `install` refuse after a release. Phase 4. |
| R20 | Lock timeout | `apply` and `snapshot` set a default `lock_timeout` for their own lock and for each step, overridable by a parameter. Supersedes R11. Phase 4. |
| R21 | Privilege model | `install` accepts an existing `caverns` schema of any owner and revokes nothing. It checks the owner, revokes access from `PUBLIC` and documents the grants a runner role needs. The docs state that the append-only triggers guard against mistakes, not against the schema's owner or a superuser. Phase 4. |
| R22 | Standby and replication | A write on a standby is refused with a Caverns error, not a driver error. Logical replication of the `caverns` schema is unsupported and listed under known limitations. Phase 4. |
| R23 | Lock hazards | Murozond also flags statements that take long locks or rewrite a table (`CREATE INDEX` without `CONCURRENTLY`, `ADD CONSTRAINT` without `NOT VALID`, `ALTER COLUMN TYPE`, `SET NOT NULL`). The list is a Phase 5 sign-off item. |
| R24 | Non-transactional steps | A flag on `Step` runs a step outside a transaction and records it. Supersedes R10. Phase 5. |
| R25 | Drift default | `on_drift` refuses by default (*proposed*, confirmed at the Phase 5 sign-off), with an explicit way to proceed. Amends R4. |
| R26 | Fingerprint coverage | Triggers, row-level-security flags and policies, and enum labels join the fingerprint before the drift record is built. Amends C4. Phase 5. Grants, sequences, other types, extensions, storage options and publications stay Later. |
| R27 | Snapshot size | A snapshot estimates the size of what it copies and checks the key size before copying; the cost of its long transaction is documented. Batching stays Later. Phase 4. |
| R28 | Search path of steps | A step runs under the caller's `search_path`. The adoption guide requires schema-qualified steps; pinning a declared schema in `apply` is a Phase 4 sign-off item. |
| R29 | Release practice | Phase 8's release readiness (R9) also covers: a CI job that builds the wheel and sdist, checks their contents and runs `chromie` from a clean install; a stability and deprecation policy naming the public surface; trusted publishing from a protected environment, the tag equal to the version, a rehearsal on the test index; the supported Python versions in CI and a statement of the tested PostgreSQL version; a coverage threshold; format and file-hygiene checks; a `NullHandler` on the library logger; current licence metadata and project links; a dependency-update cooldown and dependency review on pull requests; a code of conduct and an issue template; a `schema_version` key in the JSON output. `chromie --version` lands in Phase 7. |
| R30 | The name | The project name and the codenames, which are import paths, come from a game franchise, and a rename after publishing is a breaking change. Before anything is published the owner checks the name's availability on the package index and its trademark exposure and confirms or changes it: a Phase 8 sign-off item, taken early. |
| R31 | Later and not planned | A run identifier tying one apply's rows together, property-based tests for the checksum and the classifier, and batched snapshots join Later. Not planned: expand-and-contract orchestration, declarative schema diff, OpenTelemetry hooks, shell completion, mutation testing, a separate SBOM. |

### Status

| Phase | Status | Pull request |
|---|---|---|
| 0 — Development process | Merged | [#1](https://github.com/Deluha/caverns-of-time/pull/1) |
| 1 — Nozdormu | Merged | [#2](https://github.com/Deluha/caverns-of-time/pull/2) |
| 2 — Chromie and Infinite | Merged | [#3](https://github.com/Deluha/caverns-of-time/pull/3) |
| 3 — Epochs | Merged | [#4](https://github.com/Deluha/caverns-of-time/pull/4) |
| 4 — Hardening | In review | [#7](https://github.com/Deluha/caverns-of-time/pull/7) |
| 5 — Murozond | Not started | |
| 6 — Timewalking | Not started | |
| 7 — Chromie write commands | Not started | |
| 8 — First consumer adoption and release | Not started | |

### Running a phase

One development session per phase, in order. A phase starts only when the previous phase's pull request
is merged. Kick-off prompt: "Implement Phase N of `ROADMAP.md` following its Running a phase section."

1. **Read.** This file, `CLAUDE.md` and the development skill (both exist from Phase 0 on).
2. **Sign-off first.** Present the phase's sign-off items to the owner as concrete proposals (table
   layouts, signatures, CLI output samples) and wait for confirmation before writing code. Record each
   confirmed decision under the phase, dated, in the phase's first commit.
3. **Implement.** One commit per table row, in order, each passing the verify command.
4. **Review.** The `reviewer` agent reviews the whole phase diff before the pull request opens; its
   findings are fixed in further commits and reviewed again.
5. **Deliver.** One pull request for the phase; the phase's Status row names it ("In review"). The next
   phase's first commit sets the previous row to "Merged". Each phase adds its entry to one
   "v0.1.0 — in development" section at the top of `RELEASE_NOTES.md`; Phase 8 turns it into the release
   entry and removes the v0.1 section from this file.
6. **Stay in scope.** The scope rules bind every phase. A phase that finds an agreed item infeasible, or
   needs something not listed here, stops and asks the owner; it never narrows or widens scope itself.

### Phase 0 — Development process

Ports the guidance machinery of an existing project. No product code.

| Commit | Content |
|---|---|
| 1 | `CLAUDE.md`: operating principles, short-form hard rules with anchors, the ask-first list — rewritten for this repository. |
| 2 | Agents (`implementer`, `reader`, `reviewer`, `scribe`), hooks (session start, post-edit check) and `.claude/settings.json` with the same allow / ask / deny shape. |
| 3 | Development skill as a router with `references/` (architecture and codenames, conventions, style, recipes, QA and CI, workflows), plus the generic skills `verify`, `review-diff`, `db-change`, `guidance-change`. |
| 4 | Package skeleton (`pyproject.toml`, empty `caverns_of_time` package, `tests/`), the verify command, and the conventions, guidance-integrity and docs-sync checkers with their tests. |
| 5 | Pre-commit, gitleaks, `.editorconfig`, and the CI workflows: pre-commit, docs sync, tests with a PostgreSQL 18 service container and the single "CI gate" check. |
| 6 | Local test database: session-start hook and devcontainer start a PostgreSQL instance and export the test connection string. |
| 7 | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `RELEASE_NOTES.md`. |

Not ported: the source project's domain rules, domain references and domain workflows, and the hard rules
that only make sense there (R1, R5, R8, R9).

Sign-off: the hard rules and ask-first list of the new `CLAUDE.md`; which source-project script conventions
carry over (the `#%%` markers, the icon logging helpers); the package name; the licence; how development
sessions get PostgreSQL 18.

Sign-off decisions (owner, 2026-11-22: S1–S5 confirmed as proposed):

| # | Item | Decision |
|---|---|---|
| S1 | Package and layout | Distribution `caverns-of-time`, import package `caverns_of_time`, flat at the repository root; one module per component (`nozdormu`, `epochs`, `infinite`, `murozond`, `timewalking`); `chromie` the only CLI entry; dev tooling in `dev_tools/`, not shipped; verify command `python dev_tools/verify.py`; Python 3.11 or newer. |
| S2 | Hard rules and ask-first list | Nine hard rules: generic core, one canonical helper, safe SQL, no unasked files, append-only ledger, own changes through the ledger, docs move with the code, real database tests, one module per component. The four operating principles of the source project unchanged. Ask-first: ledger schema and checksum, public API, destructive behaviour on a consumer database, Murozond's list and guard relaxations, runtime dependencies and PostgreSQL versions, the guidance machinery and CI. Text: `CLAUDE.md`. |
| S3 | Source-project conventions | Dropped: `#%%` markers and the no-module-docstring rule. Kept: icon status helpers for CLI output only (library code uses `logging`), codenames, actions pinned by commit SHA, the single `CI gate` check, gitleaks, pre-commit, the path-rule and eval mechanisms, the `settings.json` shape and agent tiers. Changed: mypy is strict and gating. |
| S4 | Licence | MIT, unchanged. |
| S5 | PostgreSQL 18 in development | The official `postgres:18` Docker image, started by the cloud session-start hook and by `python dev_tools/database.py start`; the same image in the dev container and CI. No fallback to another version: if the instance cannot start, the database tests fail. |

### Phase 1 — Nozdormu

| Commit | Content |
|---|---|
| 1 | `Step` and changeset model; loaders for a Python list and a directory of files; checksums. |
| 2 | Ledger schema and its self-installation (the ledger's own tables are its first recorded step). |
| 3 | Preflight: ledger first, then the step's check; statuses applied / pending / unknown / conflict. |
| 4 | Apply: one transaction per step, ledger row on success and on failure, refusal of a step whose label was applied with a different checksum. |
| 5 | Baseline: record an existing database as the ledger's starting entry with a schema fingerprint. |
| 6 | Docs and release-notes entry. |

Sign-off: ledger schema name and columns; the `Step` signature; the connection contract (a DB-API
connection, so both psycopg2 and psycopg 3 consumers work — *proposed*); what a checksum covers; how a
deliberately edited step is re-applied.

Sign-off decisions (owner, 2026-11-22: N1–N8 confirmed as recommended; N9 confirmed after the phase review; N10 confirmed after the pull-request review):

| # | Item | Decision |
|---|---|---|
| N1 | Ledger schema | Fixed schema `caverns`, table `caverns.ledger`: `version` (identity, the ledger version), `changeset`, `label`, `checksum` (64 hex), `outcome` (`applied` / `failed` / `baseline` / `accepted`), `fingerprint` (required on `baseline`), `error` (exactly on `failed`), `duration_ms`, `recorded_at`, `recorded_by`, `caverns_version`; index on (changeset, label, version); triggers reject UPDATE, DELETE and TRUNCATE. Created by step `ledger` of Caverns' own changeset `caverns` (name reserved). |
| N2 | `Step` and `Changeset` | Frozen `Step(label, sql, check=None)`, `Step.from_file(path, *, label=None, check=None)`, `Step.checksum`; a check is SQL returning one boolean, true when the effect is present. `Changeset(name, steps)` and `Changeset.from_directory(name, path)`: each `*.sql` a step in filename order, `<stem>.check.sql` its check, no recursion. Labels non-empty, one line, no surrounding whitespace, unique; empty SQL or changeset refused. |
| N3 | Public functions | `install(conn)`, `preflight(conn, changeset)`, `apply(conn, changeset, *, reapply=(), accept=())`, `baseline(conn, changeset, *, through=None, schemas=None)`. Statuses: applied (latest non-failed row matches, or at or before the baseline's step), conflict (latest row differs), pending (no row; no check or check false), unknown (no row; check true). Apply refuses the whole run up front on an unacknowledged conflict or unknown; one transaction per step under an advisory lock; a failure is rolled back, recorded as a `failed` row and raised. Exceptions in one module, `errors`. |
| N4 | Connection contract | Any DB-API 2.0 connection with `%s` parameters (psycopg 3, psycopg2); no driver imported, no runtime dependency; a connection with autocommit on is refused; Caverns commits and rolls back itself. `psycopg2-binary` joins the `dev` extra and the database tests run on both drivers. Statements that cannot run in a transaction are out of scope for v0.1. Identifier quoting without a driver module is done server-side (E5). |
| N5 | Checksum | SHA-256 hex of the step's SQL as UTF-8, line endings normalised to `\n`, leading and trailing whitespace of the whole text trimmed; label, check and changeset name not covered. |
| N6 | Edited step | Preferred: a new step. `reapply` runs the edited SQL and records a new `applied` row; `accept` records the new checksum as `accepted` without running (also for an unknown step). Naming a step that is not a conflict or unknown is an error. |
| N7 | Baseline | One `baseline` row for the `through` step (default the last), with its checksum and a fingerprint; nothing executed; refused when the changeset already has rows. Phase 1 fingerprint: SHA-256 over a canonical document of tables and columns in the given schemas (default all but `pg_*`, `information_schema`, `caverns`); Phase 2 widens it and moves it to Infinite. |
| N8 | Layout | One module `nozdormu` plus the shared `errors` module; split into a sub-package behind a facade only past ~800 lines. |
| N9 | Review amendment | Public names `Connection`, `LedgerEntry`, `Status`, `StepStatus` and the exceptions `CavernsError`, `ChangesetError`, `LedgerNotInstalled`, `PreflightRefused` (`statuses`), `StepFailed` (`label`, `version`), `BaselineRefused`. Returns: `install` and `apply` the rows written (`[]` when nothing to do), `preflight` one `StepStatus` per step, `baseline` its row. Refused: a bare string for `reapply`, `accept` or `schemas`, a label in both `reapply` and `accept`, an empty `schemas` (`ChangesetError`); a named schema that does not exist (`BaselineRefused`); a connection with uncommitted writes (`CavernsError`, nothing rolled back). The checksum (N5) trims ASCII whitespace only: space, tab, LF, CR, FF, VT. |
| N10 | Pull-request review amendment | Refused with `ChangesetError`: an empty check; a `<x>.check.sql` without its `<x>.sql`; a label in `reapply` or `accept` that the changeset lacks; `None` for `reapply` or `accept`; a `baseline` row whose step the changeset no longer has (the way out is a new changeset name); a check that returns a second result set where the driver reports it (psycopg 3): a check is exactly one statement. `PreflightRefused`: acknowledging a step that is not a conflict or unknown; a step whose ledger status changed under the lock, even mid-run after earlier steps committed. A failure that cannot be recorded raises `CavernsError` from the step's error. A connection whose transaction has failed or that is lost is refused (`CavernsError`); an open transaction that only read is rolled back, ending its `SET LOCAL`, uncommitted `SET` and transaction-level locks. `install` records its own step as `accepted` when its effect is present without a row (a schema-only restore). The baseline fingerprint is read with `search_path` pinned to `pg_catalog`. |

### Phase 2 — Chromie and Infinite

| Commit | Content |
|---|---|
| 1 | Chromie CLI skeleton and connection configuration. |
| 2 | `chromie log` and `chromie status`. |
| 3 | Schema fingerprint: tables, columns, indexes, constraints, views and functions read from the system catalogs. |
| 4 | Infinite: fingerprint recorded after each apply; `chromie drift` reports differences and ledger/check disagreements. |
| 5 | Docs and release-notes entry. |

Sign-off: command names and output layout; which object kinds the fingerprint covers; which schemas are
watched by default; exit codes for use in CI.

Sign-off decisions (owner, 2026-11-22: C1–C9 confirmed as recommended; C10 confirmed during implementation; C11 confirmed after the phase review):

| # | Item | Decision |
|---|---|---|
| C1 | Driver and connection | Optional extra `caverns-of-time[cli]` = `psycopg[binary]>=3.2,<4`, imported by `chromie` only when it runs (without it: exit 2 naming the extra); the library keeps no runtime dependency (N4). Connection string: `--dsn`, else `CAVERNS_DSN`, else exit 2; no fallback to libpq defaults. |
| C2 | Commands | Entry point `chromie = "caverns_of_time.chromie:main"`; all read-only; every command takes `--dsn` and `--format text\|json` (default `text`). `chromie log [--changeset NAME] [--limit N]`: ledger rows newest first, `--limit` default 20, 0 for all. `chromie status DIRECTORY [--changeset NAME]`: preflight of a directory changeset, name defaulting to the directory's name. `chromie drift [DIRECTORY] [--changeset NAME] [--schema NAME]`: drift report. `--changeset` and `--limit` join the flag table. |
| C3 | Exit codes | 0 success (`log` rows or an empty ledger; `status` all applied; `drift` none). 1 a finding (`status`: a step pending, conflict or unknown; `drift`: any difference or disagreement). 2 usage or environment (bad arguments, connection, ledger not installed, bad changeset, no fingerprint recorded). |
| C4 | Fingerprint kinds | Tables (`r`, `p`, `f`), columns (name, type, not null, default, identity, generated, collation), indexes (`pg_get_indexdef`), constraints (`pg_get_constraintdef`), views and materialized views (`pg_get_viewdef`), functions and procedures (identity arguments, `pg_get_functiondef`); read with `search_path` pinned to `pg_catalog`; extension-owned objects excluded. Not covered: triggers, sequences, types, grants, ownership, comments, extensions. SHA-256 of the canonical JSON document; the baseline hash takes the widened definition (Phase 1 unreleased). |
| C5 | Watched schemas | Set by `baseline(schemas=...)` (default every schema but `pg_*`, `information_schema`, `caverns`) and inherited by each later fingerprint; without a baseline, the default. `apply`'s signature is unchanged. `drift --schema` narrows the report only. |
| C6 | Fingerprint storage | New own step `fingerprint`: `caverns.fingerprint` (`version` bigint primary key referencing `caverns.ledger`, `schemas` text[], `document` jsonb, `hash` 64 hex, `recorded_at` `clock_timestamp()`), append-only by the same triggers as the ledger; the ledger is unchanged. `apply` writes one row in the transaction of its last ledger row, `baseline` one with its row (the ledger's `fingerprint` keeps the same hash). Before running, `apply` compares the live schema with the latest fingerprint and logs each difference as a warning; no refusal. No `drift --accept` in v0.1. |
| C7 | Library API | `infinite.fingerprint(conn, *, schemas=None) -> Fingerprint` (`schemas`, `document`, `hash`); `infinite.drift(conn, changeset=None) -> Drift` (`fingerprint_version`, `differences` of `Difference(kind, name, change)` with change `added` / `removed` / `changed`, `disagreements` of `StepStatus`); `nozdormu.history(conn, *, changeset=None, limit=None) -> list[LedgerEntry]`; exception `NoFingerprint`. The fingerprint code moves from `nozdormu` to `infinite`. |
| C8 | Disagreements | A step whose ledger status is `applied` but whose check is now false; `unknown` stays with `status`. Only with a changeset. |
| C9 | Commits | As the table above; the own step `fingerprint` lands in commit 4. The guidance evals run before the pull request opens. |
| C10 | Implementation amendment | `infinite.drift(conn, changeset=None, *, schemas=None)`: `schemas` narrows the report to watched schemas (an unwatched one is a `ChangesetError`); `Drift` also carries `schemas`. The fingerprint's constraints are check, foreign-key, primary-key, unique and exclusion (`contype` `c`, `f`, `p`, `u`, `x`): PostgreSQL 18's NOT NULL constraints (the column's `not_null`) and constraint triggers are left out. Shared private modules `_connection` (connection plumbing) and `_fingerprint` (the fingerprint machinery) keep Nozdormu and Infinite free of a mutual import; `infinite` is the public facade. |
| C11 | Review amendment | Watched schemas (C5, made exact): the schemas of the latest `baseline` row's fingerprint; without a baseline, the default, read again at every fingerprint, so a schema created later is watched. A baseline without `schemas` takes the default. Drift and the pre-apply warning compare the watched schemas plus those the latest fingerprint covers (drift narrowed by `schemas`) with that fingerprint: a watched schema the fingerprint lacks shows its objects as added, a covered schema since dropped as removed; the fingerprint apply records covers the watched schemas. The connection string is required, but libpq fills what it omits from the standard `PG*` variables, as every libpq client does (documented, not refused). `chromie` exits 2 on any error that is not a finding, a driver or OS error included. |

### Phase 3 — Epochs

| Commit | Content |
|---|---|
| 1 | Opt-in registry of snapshotted tables and the epoch catalog (name, time, ledger version). |
| 2 | Snapshot: copy the opted-in tables into the epoch tables under one epoch. |
| 3 | `chromie as-of <epoch> <table>` and the query API. |
| 4 | `chromie diff <epoch> <epoch>`: rows added, removed and changed, by a declared key. |
| 5 | Retention: list and delete epochs. |
| 6 | Docs and release-notes entry. |

Sign-off: storage layout (one history table per source table, keyed by epoch — *proposed*); behaviour
when a table's columns changed between two epochs; retention defaults; whether deletion of an epoch is
itself recorded in the ledger.

Sign-off decisions (owner, 2026-11-23: E1–E7 confirmed as recommended; E8 confirmed after the phase review):

| # | Item | Decision |
|---|---|---|
| E1 | Storage layout | Not one history table per source table: one shared jsonb history, no DDL at run time. New own step `epochs` (third of `caverns`) creates, in schema `caverns`: `epoch_table` (the opt-in registry, mutable: `schema_name`, `table_name`, `key` text[] non-empty, `registered_at`, `registered_by`; primary key (`schema_name`, `table_name`)); `epoch` (the catalog: `epoch` bigint identity primary key, `name` unique, `ledger_version` referencing `caverns.ledger`, `taken_at`, `taken_by`, `caverns_version`); `epoch_source` (one row per table per epoch: `epoch`, `schema_name`, `table_name`, `key` text[], `columns` jsonb `[{"name", "type"}]` in column order, `row_count`; primary key (`epoch`, `schema_name`, `table_name`)); `epoch_row` (`epoch`, `schema_name`, `table_name`, `key` jsonb, `row` jsonb = `to_jsonb` of the source row; primary key (`epoch`, `schema_name`, `table_name`, `key`); a deferred foreign key to `epoch_source`); `epoch_deletion` (tombstones: `epoch` primary key referencing `epoch`, `deleted_at`, `deleted_by`, `caverns_version`). Triggers: `epoch`, `epoch_source` and `epoch_deletion` reject UPDATE, DELETE and TRUNCATE; `epoch_row` rejects UPDATE and TRUNCATE, and DELETE unless the epoch has a tombstone. |
| E2 | Columns changed between epochs | `as_of` shows an epoch's own columns. `diff` reports each column added, removed or of another type, and compares rows on the columns both epochs have. A table whose key differs between the two epochs is refused (`EpochRefused`, naming it); a table in one epoch only is reported as added or removed, its rows not listed. |
| E3 | Retention defaults | Everything is kept: nothing is pruned, a snapshot never deletes. Deletion is explicit, by name. No keep-last-N in v0.1. |
| E4 | Deletion and the ledger | Not a ledger row (the ledger is the schema timeline that preflight and Timewalking read): a tombstone row in `caverns.epoch_deletion`, written in the transaction that deletes the epoch's `epoch_row` rows. The catalog row stays; listings show the deletion time. |
| E5 | Library API | Module `epochs`: `register(conn, schema, table, *, key=None) -> TrackedTable` (key defaults to the primary key, refused without either; the same key again is a no-op, another key replaces it with a warning); `unregister(conn, schema, table)` (history kept); `tracked(conn) -> list[TrackedTable]`; `snapshot(conn, name) -> Epoch` (every registered table in one REPEATABLE READ transaction under the ledger's advisory lock, taken before the snapshot; refused without tables, for a missing table or key column, a key with duplicates or nulls, a used name); `catalog(conn, *, limit=None) -> list[Epoch]` (newest first, deleted ones included); `as_of(conn, epoch, schema, table, *, limit=None) -> TableAt`; `diff(conn, old, new, *, tables=None) -> EpochDiff`; `delete(conn, names) -> list[Epoch]`. Epochs are named by `name`, required, following the label rules and unique for good. Exceptions `EpochNotFound`, `EpochRefused`; bad arguments `ChangesetError`. Identifiers are composed by PostgreSQL (`format('%I.%I')`), never in Python. |
| E6 | CLI | `chromie epochs [--limit N]`; `chromie as-of EPOCH SCHEMA.TABLE [--limit N]` (both `--limit` default 20, 0 all); `chromie diff OLD NEW [--table SCHEMA.TABLE ...]`; `chromie delete-epoch EPOCH ... [--dry-run] [--yes]`, the first command that writes (amends C2): it asks for confirmation (default no) and requires `--yes` unattended. Exit codes: 0 success; `diff` 1 when anything differs; 2 usage or environment (an epoch or table not found, a deleted epoch, a refusal). `--table` joins the flag table. No snapshot command: the consumer's runner calls `snapshot`. |
| E7 | Commits | As the table above; the own step `epochs` lands in commit 1 with all five tables. |
| E8 | Review amendment | A snapshot prints its values under pinned settings, transaction-local: `TimeZone` `UTC`, `IntervalStyle` `postgres`, `extra_float_digits` `1`, `bytea_output` `hex`, `DateStyle` `ISO, MDY`, `lc_monetary` `C`, so what `epoch_row.row` stores does not depend on the caller's session. `delete(conn, names, *, dry_run=False)`: with `dry_run` the same checks and errors, returns the epochs it would delete and writes nothing; `chromie delete-epoch` uses it for `--dry-run` and its confirmation. A key column whose type differs between two epochs is a key that differs (`diff` refuses the table). A primary key's `INCLUDE` columns are not key columns. A declined confirmation exits 0. The guidance's "history tables" wording becomes "the epoch tables". |

### Phase 4 — Hardening

Fixes what the second review found in Phases 1–3 (R13–R22, R27, R28). No new component.

| Commit | Content |
|---|---|
| 1 | Every own step's checksum pinned by a test (R19). |
| 2 | A step at or before the baseline's step without a row: its own preflight status, refused by apply until acknowledged (R14). |
| 3 | A `failed` row records the error code and primary message only (R15). |
| 4 | Default `lock_timeout` in apply and snapshot, with a parameter (R20). |
| 5 | Privilege model: owner check and revoked public access at install (R21). |
| 6 | A standby is refused with a Caverns error; the session lock's release is checked (R22, R17). |
| 7 | Fingerprint read under pinned output settings; server version recorded (R18). |
| 8 | Snapshot: row-level security refusal, size estimate and key-size check (R16, R27). |
| 9 | Docs and release-notes entry. |

Sign-off: the new status's name and how it is acknowledged; what a `failed` row keeps; the default timeout's
value (5 seconds — *proposed*) and the parameter on `apply` and `snapshot`; the owner check, the grants and
what `install` does with a schema of another owner, and whether these arrive as a new own step; whether the
fingerprint change alters recorded hashes; refusal or record for row-level security; the snapshot size
limits; whether `apply` pins a declared schema (R28).

Sign-off decisions (owner, 2026-11-26: H1–H10 confirmed as proposed):

| # | Item | Decision |
|---|---|---|
| H1 | Step before the baseline (R14) | `baseline` writes one `baseline` row per step from the first through `through`, in one transaction, each with its step's checksum and the same fingerprint hash; the `caverns.fingerprint` row is keyed at the last (amends N7). New status `before_baseline` (`Status.BEFORE_BASELINE`): a step at or before the latest baseline row's step without a row of its own; its check is not run. Acknowledged by `reapply` (run, record `applied`) or `accept` (record `accepted`); `chromie status` shows it and exits 1. An edited pre-baseline step now reads as `conflict`. |
| H2 | Recorded error (R15) | A `failed` row's `error` is `<SQLSTATE>: <primary message>`, read without a driver import (psycopg 3 `sqlstate`, psycopg2 `pgcode`, both `diag.message_primary`); an error without them keeps the first line of its text. `StepFailed` is still raised from the whole error. The README states that step SQL must not carry secrets. |
| H3 | Lock timeout (R20) | `apply(..., lock_timeout=5.0)` and `epochs.snapshot(..., lock_timeout=5.0)`: seconds, a positive number; `None` keeps the session's setting. Set by `set_config('lock_timeout', %s, true)` in each transaction before Caverns' lock, so it bounds the lock and each step. A timeout on Caverns' lock raises `CavernsError`; one in a step is a `failed` row and `StepFailed`. `install`, `baseline`, `register`, `unregister` and `delete` take the fixed 5 seconds, without a parameter. Amended by the owner after the phase review, 2026-11-26: only a run's first wait for Caverns' lock is bounded; once the run has written a row, its later waits for Caverns' lock (the failure record's included) are not, so a run never stops between its rows and its fingerprint; every step's own lock waits stay bounded. |
| H4 | Privilege model (R21) | `install` refuses (`CavernsError`) an existing `caverns` schema or own table whose owner the current role is not a member of. New own step `privileges` (fourth of `caverns`): `REVOKE ALL` from `PUBLIC` on the schema, its tables, sequences and functions; its check is that `PUBLIC` holds no privilege on any of them. Amended by the owner, 2026-11-26: no default-privilege revoke (a per-schema one cannot revoke a global default, a global one would change the consumer role's defaults everywhere); a test asserts after `install` that `PUBLIC` cannot execute any `caverns` function, so a later own step that adds one revokes it itself. The README documents the grants a runner role needs and that the append-only triggers guard against mistakes, not the schema's owner or a superuser. |
| H5 | Standby and lock release (R22, R17) | Every write (`install`, `apply`, `baseline`, `register`, `unregister`, `snapshot`, `delete` without `dry_run`) first reads `pg_is_in_recovery()` and `transaction_read_only`: a standby or a read-only session is refused with `CavernsError`. The read-only case is tested on the real database; the standby case shares its code path (no replica in tests). A `false` from `pg_advisory_unlock` or an error releasing the session lock raises `CavernsError` saying the epoch was committed and that a session-mode pooler or a direct connection is required (replaces the warning). Logical replication of `caverns` joins the known limitations. |
| H6 | Fingerprint settings (R18) | The fingerprint is read under E8's settings plus `quote_all_identifiers` `off` and `standard_conforming_strings` `on`. New own step `fingerprint_server`: `caverns.fingerprint` gains `server_version integer` (`server_version_num`, nullable, outside the hash); `Fingerprint` gains `server_version`. The document format is unchanged, so a hash recorded under default settings stays equal; nothing is released. Drift output unchanged. |
| H7 | Row-level security (R16) | Refused, not recorded: before copying, a table with row-level security is refused (`EpochRefused`, naming table and role) unless the role is a superuser, has `BYPASSRLS` or owns the table without `FORCE`; `row_security` is pinned `off` in the snapshot transaction as a backstop. |
| H8 | Snapshot size (R27) | The total `pg_table_size` of the registered tables is logged before copying; `snapshot(..., max_bytes=1 GiB)` refuses (`EpochRefused`) above it, `None` lifts the limit. A table whose largest key plus its schema and table names exceeds the btree entry limit (from `block_size`; 2,704 bytes on 8 kB pages) is refused before copying. The README documents the cost of the long REPEATABLE READ transaction. |
| H9 | Search path of steps (R28) | Not pinned in v0.1: steps and checks run under the caller's `search_path`; the README requires schema-qualified steps (the adoption guide repeats it in Phase 8). |
| H10 | Commits | As the table above; commit 1 pins every own step's checksum, and the steps of commits 5 and 7 add their pins. No new exception class. |

### Phase 5 — Murozond

| Commit | Content |
|---|---|
| 1 | Murozond: classify each step's SQL as safe, destructive, lock hazard or unknown (R23). |
| 2 | Murozond wired into preflight and apply: a destructive, lock-hazard or unknown step needs an explicit acknowledgement. |
| 3 | `chromie status` shows each step's classification. |
| 4 | Steps that cannot run in a transaction: a flag on `Step` (R24). |
| 5 | Fingerprint covers triggers, row-level-security flags and policies, and enum labels (R26). |
| 6 | Drift record (R4): the differences found before an apply are stored in a new own table; `on_drift`. |
| 7 | Docs and release-notes entry. |

Sign-off: the list of statements treated as destructive and the list treated as lock hazards; how SQL inside
function bodies and `DO` blocks is classified; the classifier approach without a SQL parser; the
acknowledgement parameter on `apply` and `preflight` and whether an acknowledgement is recorded; the
`chromie status` output and exit code; the `Step` flag's name, how a failure is recorded without a rollback
and what is said about an invalid index left behind; the new fingerprint kinds; the drift record's layout and
the `on_drift` default (refuse — *proposed* — or warn).

### Phase 6 — Timewalking

| Commit | Content |
|---|---|
| 1 | Step archive (R1): a new own table holding the SQL of each applied step, written by apply. |
| 2 | Baseline schema dump (R2): supplied as a file, stored with the baseline. |
| 3 | Timewalking: replay from the baseline's dump through the archived steps to a chosen version in a scratch database. |
| 4 | `chromie timewalk <version>` and a comparison of the rebuilt schema with a recorded fingerprint. |
| 5 | `chromie show <version>`: the archived SQL of a ledger row. |
| 6 | Docs and release-notes entry. |

Sign-off: the archive table's layout and whether a step's check is archived; how the dump is supplied and
stored, and what a baseline taken before this phase does; who creates the scratch database and how it is
named to Timewalking (a second connection string — *proposed*); the refusals of R3; how a step with no
archived SQL is replayed; the `timewalk` and `show` output and exit codes; how archived SQL that carries a
secret is handled (R15).

### Phase 7 — Chromie write commands

| Commit | Content |
|---|---|
| 1 | `chromie install`. |
| 2 | `chromie apply DIRECTORY`: preflight, Murozond's acknowledgements, apply. |
| 3 | `chromie baseline`. |
| 4 | `chromie --version`. |
| 5 | Docs and release-notes entry. |

Sign-off: command names, flags and output; confirmation and `--yes` behaviour (as `delete-epoch`, E6); how
`reapply`, `accept` and Murozond's acknowledgements are given on the command line; a `--dry-run` for
`apply`; exit codes; whether `snapshot`, `track` and `untrack` commands join.

### Phase 8 — First consumer adoption and release

| Commit | Content |
|---|---|
| 1 | Adoption guide: installing the package, writing an adapter, taking the baseline, the database privileges Caverns needs, schema-qualified steps (R28), the session-mode pooler requirement (R17), the connection string in the environment rather than on the command line. |
| 2 | End-to-end test: a sample consumer project with a tuple-style changeset list, run through an adapter. |
| 3 | Release readiness (R9, R29): packaging and typing marker, built-distribution check in CI, stability policy, release workflow, supported versions, coverage and format gates, community files, known limitations, JSON output version. |
| 4 | v0.1.0 release: version, release notes, this section removed. |

The consumer side is a separate pull request in the consumer's own repository, under that repository's own
rules.

Sign-off: how the first consumer installs the package (git dependency or a package index); who takes the
baseline on the live database and when; whether the first consumer uses Epochs at all; which Python versions
are supported; where the release is published; the project name (R30); the stability policy's terms.

### Later (not in v0.1)

- Row-level history by trigger for slowly changing tables.
- A PostgreSQL version matrix.
- Rollback (down) steps.
- Epochs: snapshot of a subset of tables, a key filter on `as-of`, export and restore of an epoch,
  keep-last-N retention, batched snapshots.
- Fingerprint coverage of grants, sequences, other types, extensions, storage options and publications;
  `drift --accept`.
- Ledger tamper evidence: a hash chain over the rows and a `chromie verify`.
- A run identifier tying one apply's ledger rows, fingerprint and drift record together.
- Property-based tests for the checksum and the classifier.

Not planned: expand-and-contract orchestration, declarative schema diff, OpenTelemetry hooks, shell
completion, mutation testing, a separate SBOM (R31).
