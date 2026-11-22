# Contributing

## Setup

A new Codespace or Dev Container sets itself up (`.devcontainer/devcontainer.json`). A local clone needs
Python 3.11 or newer and Docker:

```bash
git clone https://github.com/Deluha/caverns-of-time.git
cd caverns-of-time
pip install -e ".[dev]"            # editable install with the tooling
pre-commit install                 # git hooks, all stages
python dev_tools/database.py start # the PostgreSQL 18 test instance
```

## QA gates (run before every commit)

Run `python dev_tools/verify.py` — it runs every verification step and prints one pass/fail table. The
test instance must be running: a database test that cannot connect fails the run. The verify list is
`.claude/skills/caverns-of-time-development/references/qa-and-ci.md#verify`. The same gates run in CI on
every PR.

## What each workflow and checker does

One line each; the contracts are in the pointers (`qa-and-ci.md` and `checkers.md` are under
`.claude/skills/caverns-of-time-development/references/`).

| Item | What it does |
|---|---|
| `ci-pre-commit` | Runs every pre-commit-stage hook on the whole tree, then a pinned, checksum-verified gitleaks scan of the PR's commits and the tree (`qa-and-ci.md#gates`) |
| `ci-docs-sync` | Fails a code change in a documented area without its doc, against the PR base (`checkers.md#checker-docs-sync`) |
| `ci-tests` | `pytest` with coverage against a PostgreSQL 18 service container, gating mypy and actionlint; ends in `CI gate` (`qa-and-ci.md#workflows`, which also lists the three checks to require on `main`) |
| `dev_tools/check_conventions.py` | The mechanical code conventions, rules C1–C9 (`checkers.md#checker-project-conventions`) |
| `dev_tools/check_guidance_integrity.py` | Guidance pointers, anchors, size budget, routing, rule files and command skills (`checkers.md#checker-guidance-integrity`) |
| `dev_tools/check_docs_sync.py` | The docs-sync guard of the commit-msg hook and `ci-docs-sync` (`checkers.md#checker-docs-sync`) |
| `dev_tools/verify.py` | The one command to run before a PR: every step above plus `ruff`, `mypy` and `pytest`, one pass/fail table (`qa-and-ci.md#verify`) |
| `dev_tools/database.py` | Starts and stops the local test instance (`qa-and-ci.md#test-database`) |
| `.claude/hooks/` | Claude Code session hooks: dev setup and test database at session start, lint after an edit (`qa-and-ci.md#gates`) |
| gitleaks (`.gitleaks.toml`) | Secret scan: staged changes locally, commit range and tree in CI; a finding is fixed, never allow-listed to get green |

## Branch and PR flow

1. Branch from `main` with a short descriptive name.
2. Keep changes surgical; one concern per PR. Roadmap work is one PR per phase (`ROADMAP.md`).
3. Open a PR using the template; fill every section.
4. CI must be green before review.

## Conventions

- Read `CLAUDE.md` first — it holds the binding hard rules (generic core, one canonical helper, safe
  SQL, no unasked files, the append-only ledger, own changes through the ledger, docs moving with the
  code, real database tests, one module per component). Their full forms are
  `.claude/skills/caverns-of-time-development/references/conventions.md`; task routing is the skill's
  `SKILL.md`.
- The ledger schema, the checksum definition, the public API and runtime dependencies are ask-first
  changes — confirm with the owner.

## Commit messages

- Imperative subject line, ≤ 72 chars (`Add ledger checksum`, not `Added…`).
- Body explains *why* when it is not obvious.
- `[skip-docs]` on a line of its own in the body exempts that commit's files from the docs-sync check;
  it is an escape hatch, not the default.
