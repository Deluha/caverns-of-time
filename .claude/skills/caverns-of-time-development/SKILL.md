---
name: caverns-of-time-development
description: >-
  Development guide for the Caverns of Time repository (a PostgreSQL timeline keeper:
  Nozdormu ledger, Chromie CLI, Epochs snapshots, Infinite drift detection, Murozond guard,
  Timewalking rebuilds). Use for ANY code, SQL, workflow, test or doc change anywhere in this
  repository, even when conventions are not mentioned, and for running a roadmap phase.
  It routes each task to its single authoritative file.
---

# Caverns of Time — routing table

`CLAUDE.md` holds the principles, hard rules and ask-first gates; this table names the ONE file
that holds each task's detail. Read it before editing; when a workflow matches, follow it step by
step. Grep the `## ` heading you need and read that section, not the whole file.

| Task / question | Read |
|---|---|
| Conventions: hard rules R1–R9 full forms, simplicity audit, error handling, SQL patterns | `references/conventions.md` |
| Style: docstrings and comments, console messages (`#messages`), CLI flags (`#cli`) | `references/style.md` |
| Architecture: module map, components and codenames, the consumer boundary, packaging, tech stack | `references/architecture.md` |
| QA: hook gates, the verify command, the test database, the GitHub workflows, README and dependency policy | `references/qa-and-ci.md` |
| Checker mechanics: what each guard parses, scopes, exemptions (read before editing a checker or a guarded doc line) | `references/checkers.md` |
| Grep-before-coding commands, Implementation Decision format, orchestration (roles and agents, small-edit threshold, brief, review loop), lessons loop (`#lessons`) | `references/recipes.md` |
| Add a component or a module to the package | `references/workflows/adding-a-component.md` |
| Change one of Caverns' own database objects (ledger, epoch catalog, history tables) | `references/workflows/changing-own-schema.md` |
| Change guidance: `CLAUDE.md`, the skill, an agent, a rule, a hook, `.claude/settings.json`, CI or the pre-commit config | `references/workflows/changing-guidance.md` |
| Run a roadmap phase (sign-off, commits, review, delivery); planned scope and status | `ROADMAP.md` |
| Test suite inventory, the database fixtures, the local test instance | `tests/README.md` |
| Dev tooling: verify, the checkers, the test-database command | `dev_tools/README.md` |
| Package contents and public surface | `caverns_of_time/README.md` |
| GitHub Actions (triggers, groups) | the `.github/workflows/` file header, then the QA row's `#workflows` table |
| Release history / versioning | `RELEASE_NOTES.md` |
| Guidance evals: routing / ask-first prompts to run by hand before merging a change to `CLAUDE.md`, the skill or `.claude/rules/` | `evals/evals.json` (how: the recipes row's `#lessons` section) |
