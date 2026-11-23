# Conventions — the rulebook

Full forms of the hard rules R1–R9 (short forms: `CLAUDE.md`), the simplicity audit, error handling and
SQL patterns. Each rule names how it is enforced: a checker rule C1–C9
(`references/checkers.md#checker-project-conventions`), a test, or review.

## R1 generic core

The package serves any PostgreSQL project. Nothing inside it names a consumer, a consumer's schema or
table, or copies a consumer's convention. A consumer adapts its own formats to the Caverns API in an
adapter that lives in the consumer's repository; a sample consumer in `tests/` is a test fixture, not
an exception. Schema, table and column names Caverns works on always arrive as arguments or
configuration, never as constants.

No consuming project (its name, repository, schema or other detail) is named anywhere in this repository —
docs, guidance, tests and tooling included.

Enforced: C1 (known consumer names, every text file), review for the rest.

## R2 one canonical helper

A helper, a constant and a SQL statement are each defined once and imported. Before writing a helper,
grep for an existing one (`references/recipes.md#grep`). Library code reports through the standard
`logging` module so a consumer controls the output; it never prints. Command-line output goes through
the package's console helpers (one module, the icon vocabulary of `references/style.md#messages`).
The dev tools under `dev_tools/` are stdlib-only command-line scripts and print their own status lines.

Enforced: C2 (`print` in package code), review for duplication.

## R3 safe SQL

Caverns receives schema, table and column names from its caller and runs SQL built from them, so every
statement is composed safely:

- An identifier is quoted by PostgreSQL, never by Caverns' Python: the name is bound as a parameter and quoted
  with `format('%I.%I', …)` or `quote_ident(…)`, and the statement PostgreSQL composes is executed as returned.
  The library imports no driver, so there is no driver-side quoting to use (decision N4 in `ROADMAP.md`).
- A value is bound as a parameter.
- A statement is never built with an f-string, `%`, `+` or `.format()`.
- A consumer's own migration SQL (a step's payload) is executed as given, unmodified, with no
  parameters: it is the consumer's reviewed code, not input to compose.

Enforced: C3 (formatted string passed straight to `execute`), review for indirect cases.

## R4 no files the user didn't ask for

`tests/` is the only test location. No planning, progress, notes or decision markdown: the permanent
docs are the root `README.md`, `ROADMAP.md`, `RELEASE_NOTES.md`, `CONTRIBUTING.md`, `SECURITY.md`,
`CLAUDE.md`, one `README.md` per code directory and the files under `.claude/`. Scratch scripts and
notes go to the session scratchpad and are never committed.

Enforced: C4.

## R5 the ledger is append-only

A ledger row records something that happened; it is never updated and never deleted. A correction, a
failure, a re-apply and a retention action are new rows. No code path issues `UPDATE`, `DELETE` or
`TRUNCATE` against a ledger table, and no migration of Caverns' own schema rewrites existing rows.

Enforced: review, plus the ledger's triggers, tested by `test_the_ledger_is_append_only` in
`tests/test_nozdormu_ledger.py` (UPDATE, DELETE and TRUNCATE are rejected).

## R6 own changes go through the ledger

Caverns' own objects (the ledger, the fingerprint table, the epoch tables) are created and changed by steps
recorded in its own ledger, the same way a consumer's changes are. No ad-hoc DDL in application code,
no schema created as a side effect of an unrelated call. Workflow:
`references/workflows/changing-own-schema.md`.

Enforced: review, plus the workflow's test checklist.

## R7 docs move with the code

A change in a documented area updates that area's doc in the same PR: the area's `README.md`, or
`CLAUDE.md`, or the skill's `SKILL.md` / references. `[skip-docs]` on a line of its own in a commit
message exempts that commit's files; it is an escape hatch, not the default. Area map and mechanics:
`references/checkers.md#checker-docs-sync`.

Enforced: the docs-sync guard (commit-msg hook, verify, and CI where its check is required:
`references/qa-and-ci.md#workflows`).

## R8 real database tests

- Behaviour that touches the database is tested against PostgreSQL 18, never a mock or another engine.
- Tests reach the database only through the fixtures of `tests/conftest.py`; a test never opens its
  own connection or names a connection string.
- The connection string must point at this machine. No test, workflow or session connects to a
  consumer's database or any shared one.
- A test never skips itself. Without the test instance a database test skips with a notice under
  plain `pytest`, and fails under the verify command and in CI, so the done gate cannot pass untested.

Details: `tests/README.md`, `references/qa-and-ci.md#test-database`.

Enforced: C6, C9, the fixtures' local-only guard, and the required-database switch, under which a run
without a passed database test fails.

## R9 one module per component

Each component (`references/architecture.md#components`) is one module, or one sub-package behind a
facade that only re-exports, with an explicit public surface. Components call each other through that
surface. The `chromie` entry module parses the command line and holds command flow only; every
behaviour it offers is a call into a component, so the library and the CLI can never diverge.

Enforced: C7 (command-line parsing outside the entry module), review for the rest.

## Simplicity audit

Every PR leaves touched files clearer, more concise and more modular.

| Property | Rule |
|---|---|
| Clarity | Obvious names; small focused functions; no cleverness |
| Conciseness | No dead code or copy-paste duplication; a function > ~60 lines is split into helpers |
| Modularity | Shared logic is lifted to one home; never copy a helper between modules |
| Single source of truth | Table names, SQL statements, version numbers and limits each live in exactly one place — duplication is a bug |

**Audit (mandatory per PR):** before finalizing, audit every touched file plus files directly referenced
by the changes — Python, SQL and Markdown alike; fix each finding or explicitly justify leaving it:
1 dead imports · 2 unused variables/constants · 3 commented-out code (delete; git history keeps it) ·
4 duplicated logic → shared helper · 5 orphaned helpers · 6 over-long functions → named helpers ·
7 over-engineering (speculative parameters, unused hierarchies, premature abstraction) · 8 magic numbers
→ named constants · 9 patterns inconsistent with the codebase → align unless documented · 10 redundant
comments restating code → *why* comments or delete.

| Tier | Action | Examples |
|---|---|---|
| **High** | Just do it | Remove a dead import; delete a commented-out block; name a magic number |
| **Medium** | Do it, note it in the PR description | Rename for clarity; split an oversized function |
| **Low** | **Never unilaterally — ⚑ flag and ask first** | A public signature change; anything on the CLAUDE.md "Ask first" list |

Low-tier findings are never silently bundled into a PR. Surgical over sweeping: no multi-file refactor
when a targeted change suffices, no touching unrelated code in a PR; surface nearby smells separately.

## Error handling

- **Database:** catch the driver's specific exceptions, add context and **re-raise** — never swallow.
  A failed step rolls its transaction back and the failure is recorded before the error propagates.
  Never catch-and-continue around a statement whose failure would leave the ledger disagreeing with
  the database. Re-wrap with `raise … from e`.
- **Inputs:** validate early (a step without a label, an unknown epoch, an empty changeset) and fail
  loudly with a specific exception naming the offending value; never silently default.
- **Public errors:** the package raises its own exception types from one module, so a consumer can
  catch them without importing a driver.
- **Type hints** on every function; `mypy --strict` gates the package and the dev tools.

## SQL patterns

- One transaction per step; the ledger row and the step's effect commit together or not at all.
- Read the system catalogs (`pg_catalog`, `information_schema`) rather than parsing DDL text.
- Statements are idempotent where the engine allows it (`IF NOT EXISTS`, `ON CONFLICT`).
- Caverns' own objects live in their own schema, never in a consumer's.
