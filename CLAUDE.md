# Caverns of Time — Development Guide

A generic Python library and CLI that gives a PostgreSQL database a recorded past: a migration ledger (Nozdormu),
a CLI (Chromie), run-level snapshots (Epochs), drift detection (Infinite), a destructive-change guard (Murozond) and
scratch rebuilds (Timewalking). Every fact lives in ONE file: this one holds the contract, rules and ask-first
gates; the skill (`.claude/skills/caverns-of-time-development/SKILL.md` — read it first) routes each task to its
authoritative file, module detail to that module's `README.md`. Planned scope and phase process: `ROADMAP.md`.

## Operating principles (NON-NEGOTIABLE)

1. **Flag uncertainties — ask, don't assume.** If intent, a design choice or a consumer-facing behaviour is unclear,
   ask before writing code. Validate every request against repository patterns first; if it conflicts with an
   established pattern, flag it — never silently comply. Unattended: take the most reasonable reading and record the
   assumption explicitly in the reply. The *Ask first* list below and workflow-file ⚑ marks are mandatory gates.
2. **The orchestrator designs and signs off; the writer implements; the reader reads; the reviewer reviews.** The
   session model owns ⚑ gates, design, briefs and the done gate (reads the diff, runs the verify). Repo edits →
   `implementer` (`effort: "high"` on the call for bug fixes, ledger and schema work, edge cases) — except a small
   edit, which the orchestrator makes itself: one file, ≤ ~20 changed lines, no ⚑ item, no new logic. Reads spanning
   more than three files → `reader`; diff review → `reviewer`; never `general-purpose` for edits, never a `model` on
   the call. Briefs, effort, reruns → `.claude/skills/caverns-of-time-development/references/recipes.md#orchestration`
3. **Be concise.** Lead with the outcome; checklists and tables over prose; no filler. Never suppress the required
   signals: a confidence label (High/Medium/Low) on architectural calls, a flag for pattern conflicts or spotted
   smells, for/against on non-trivial approaches, and a better approach when one exists.
4. **Modular — single source of truth.** One canonical definition per helper and constant; few surgical changes over
   sweeping refactors; every PR leaves touched files clearer, more concise and more modular than it found them.

**Workflow for any change:** understand the request (⚑ if ambiguous) → route through the skill and read the routed
file → grep the established pattern first (`.claude/skills/caverns-of-time-development/references/recipes.md#grep`)
→ validate against patterns, flag conflicts → implement → verify
(`.claude/skills/caverns-of-time-development/references/qa-and-ci.md#verify`) → update the changed area's docs →
simplicity audit (`.claude/skills/caverns-of-time-development/references/conventions.md#simplicity`) and the
`reviewer` agent on every non-trivial diff.

## Hard rules

Binding short form; the full form of rule N is the anchored section.

1. **Generic core** — no consumer's name, schema, table or convention inside the package; a consumer integrates
   through an adapter in its own repository → `.claude/skills/caverns-of-time-development/references/conventions.md#r1`
2. **One canonical helper** — shared helpers are defined once and imported, never re-implemented; library code logs
   through `logging`, CLI output goes through the console helpers, never raw `print`
   → `.claude/skills/caverns-of-time-development/references/conventions.md#r2`
3. **Safe SQL** — an identifier supplied by a consumer is quoted by PostgreSQL from a bound parameter, a value is
   bound as a parameter; never string formatting
   → `.claude/skills/caverns-of-time-development/references/conventions.md#r3`
4. **No files the user didn't ask for** — `tests/` is the only test location, no new markdown beyond the permanent
   docs → `.claude/skills/caverns-of-time-development/references/conventions.md#r4`
5. **The ledger is append-only** — no code path updates or deletes a ledger row
   → `.claude/skills/caverns-of-time-development/references/conventions.md#r5`
6. **Caverns records its own changes** — its own tables are created and changed through its own ledger
   → `.claude/skills/caverns-of-time-development/references/conventions.md#r6`
7. **Docs move with the code** — a documented area's change updates its `README.md` in the same PR (`[skip-docs]`:
   escape hatch) → `.claude/skills/caverns-of-time-development/references/conventions.md#r7`
8. **Real database tests** — database behaviour is tested on PostgreSQL 18, never a mock; a skipped database test
   fails the done gate; tests connect only to the local test instance, never a consumer's database
   → `.claude/skills/caverns-of-time-development/references/conventions.md#r8`
9. **One module per component, flow-only entry** — each component sits behind its own public facade; the `chromie`
   entry holds only command flow → `.claude/skills/caverns-of-time-development/references/conventions.md#r9`

## Ask first

⚑ Never change without explicit user confirmation (workflow files add task-specific ⚑ gates) — a wrong guess
invalidates a consumer's recorded history or breaks its integration:

- The ledger schema and the checksum definition; the on-disk layout of epochs and history tables.
- Public API: the `Step` signature, apply and preflight signatures, CLI commands, flags, output layout and exit codes.
- Anything destructive Caverns runs on a consumer database (deleting an epoch, dropping a history table).
- Murozond's destructive-statement list; any guard, check or threshold relaxation.
- Adding a runtime dependency; the supported PostgreSQL versions; the licence; the scope rules of `ROADMAP.md`.
- Connecting any test, workflow or session to a database other than the local test instance.
- The guidance machinery and CI: `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, `.claude/hooks/`,
  `.github/workflows/`, `.pre-commit-config.yaml`, `.gitleaks.toml` (path-gated by `permissions.ask`; changing one:
  `.claude/skills/caverns-of-time-development/references/workflows/changing-guidance.md`).
