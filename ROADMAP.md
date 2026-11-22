# Caverns of Time — Roadmap

The planned scope of the next release. `RELEASE_NOTES.md` records what shipped; this file records
what is planned. A release's section leaves this file when the release is tagged. Owner decisions are dated.

Status of this file: **draft for owner sign-off** (2026-11-22). No product code exists yet.

## v0.1 — A recorded timeline for PostgreSQL

Goal: a generic Python library and CLI that gives a PostgreSQL database a recorded past — which schema
changes were applied and when, what opted-in tables looked like at a past run, and whether anything
changed outside the timeline. Project Acme is the first consumer, not the design target.

### Components

| Name | Lore | Role |
|---|---|---|
| **Nozdormu** | Aspect of Time, keeper of the one true timeline | Migration ledger: ordered, checksummed, append-only record of every applied step; preflight and apply. |
| **Chromie** | The guide who takes travellers to past timelines | CLI and query API: `log`, `status`, `as-of`, `diff`. |
| **Epochs** | The sands of the hourglass | Run-level snapshots of opted-in tables, each tagged with the ledger version it was taken at. |
| **Infinite** | The Infinite Dragonflight, who alter the timeline | Drift detector: live schema versus what the ledger says it should be. |
| **Murozond** | Nozdormu's corrupted future | Destructive-change guard: flags DROPs, column drops and data deletion in a changeset before apply. |
| **Timewalking** | Replaying the past | Rebuilds the schema at a past ledger version in a scratch database. |

### Scope rules (owner decisions, 2026-11-22)

- **Generic core (D1).** No Acme name, schema, table or convention enters the package. A consumer
  integrates through an adapter that lives in the consumer's repository.
- **Run-level snapshots (D2).** History is captured as Epochs: whole-table snapshots taken on demand.
  No row-level triggers in v0.1.
- **Wrap, not replace (D3).** Caverns defines its own `Step` (label, SQL or file, optional check) and
  accepts a changeset from a Python list or a directory of files. A consumer's existing runner keeps its
  flow and calls Nozdormu for apply and record. The ledger starts at adoption with one baseline entry.
- **Real database in tests (D4).** Database behaviour is tested against a real PostgreSQL, never a mock:
  a throwaway service container in CI, a local instance in development. The done gate requires the
  database tests to have run; a skipped database test fails the gate.
- **PostgreSQL 18.** CI tests against PostgreSQL 18. No version matrix until a
  second project adopts the tool.
- **Never a production database.** No test, workflow or development session connects to a consumer's
  live database. Applying to a live database is always the consumer owner's own run.
- **Delivery.** One pull request per phase, several commits each. Each phase lists what the owner
  confirms before its code is written.

### Status

| Phase | Status | Pull request |
|---|---|---|
| 0 — Development process | Not started | |
| 1 — Nozdormu | Not started | |
| 2 — Chromie and Infinite | Not started | |
| 3 — Epochs | Not started | |
| 4 — Murozond and Timewalking | Not started | |
| 5 — Acme adoption and release | Not started | |

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
   "v0.1.0 — in development" section at the top of `RELEASE_NOTES.md`; Phase 5 turns it into the release
   entry and removes the v0.1 section from this file.
6. **Stay in scope.** The scope rules bind every phase. A phase that finds an agreed item infeasible, or
   needs something not listed here, stops and asks the owner; it never narrows or widens scope itself.

### Phase 0 — Development process

Ports the Project Acme guidance machinery. No product code.

| Commit | Content |
|---|---|
| 1 | `CLAUDE.md`: operating principles, short-form hard rules with anchors, the ask-first list — rewritten for this repository. |
| 2 | Agents (`implementer`, `reader`, `reviewer`, `scribe`), hooks (session start, post-edit check) and `.claude/settings.json` with the same allow / ask / deny shape. |
| 3 | Development skill as a router with `references/` (architecture and codenames, conventions, style, recipes, QA and CI, workflows), plus the generic skills `verify`, `review-diff`, `db-change`, `guidance-change`. |
| 4 | Package skeleton (`pyproject.toml`, empty `caverns_of_time` package, `tests/`), the verify command, and the conventions, guidance-integrity and docs-sync checkers with their tests. |
| 5 | Pre-commit, gitleaks, `.editorconfig`, and the CI workflows: pre-commit, docs sync, tests with a PostgreSQL 18 service container and the single "CI gate" check. |
| 6 | Local test database: session-start hook and devcontainer start a PostgreSQL instance and export the test connection string. |
| 7 | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `RELEASE_NOTES.md`. |

Not ported: Acme's domain rules, domain
references, domain workflows, and the hard rules that only make sense there (R1, R5, R8, R9).

Sign-off: the hard rules and ask-first list of the new `CLAUDE.md`; which Acme script conventions
carry over (the `#%%` markers, the icon logging helpers); the package name; the licence (the repository
is MIT today, Acme is proprietary); how development sessions get PostgreSQL 18 (the cloud container
ships 16 today).

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

### Phase 3 — Epochs

| Commit | Content |
|---|---|
| 1 | Opt-in registry of snapshotted tables and the epoch catalog (name, time, ledger version). |
| 2 | Snapshot: copy the opted-in tables into the history schema under one epoch. |
| 3 | `chromie as-of <epoch> <table>` and the query API. |
| 4 | `chromie diff <epoch> <epoch>`: rows added, removed and changed, by a declared key. |
| 5 | Retention: list and delete epochs. |
| 6 | Docs and release-notes entry. |

Sign-off: storage layout (one history table per source table, keyed by epoch — *proposed*); behaviour
when a table's columns changed between two epochs; retention defaults; whether deletion of an epoch is
itself recorded in the ledger.

### Phase 4 — Murozond and Timewalking

| Commit | Content |
|---|---|
| 1 | Murozond: classify each step's SQL as safe, destructive or unknown. |
| 2 | Murozond wired into preflight: a destructive or unknown step needs an explicit acknowledgement. |
| 3 | Timewalking: replay the ledger from the baseline to a chosen version in a scratch database. |
| 4 | `chromie timewalk <version>` and a comparison of the rebuilt schema with a recorded fingerprint. |
| 5 | Docs and release-notes entry. |

Sign-off: the list of statements treated as destructive; how SQL inside function bodies is classified;
what the baseline must store for a replay to be possible (a schema dump — *proposed*).

### Phase 5 — Acme adoption and release

| Commit | Content |
|---|---|
| 1 | Adoption guide: installing the package, writing an adapter, taking the baseline. |
| 2 | End-to-end test: a sample consumer project with a tuple-style changeset list, run through an adapter. |
| 3 | v0.1.0 release: version, release notes, this section removed. |

The Acme side is a separate pull request in the consumer's own repository, under that repository's own
rules: its changeset runner and its hard rule 6 are ask-first items there.

Sign-off: how Acme installs the package (git dependency or a package index); who takes the baseline
on the live database and when; whether Acme uses Epochs at all — it already has
a per-run snapshot table of its own, so Epochs would be for other tables or not used.

### Later (not in v0.1)

- Row-level history by trigger for slowly changing tables.
- A PostgreSQL version matrix.
- Rollback (down) steps.
