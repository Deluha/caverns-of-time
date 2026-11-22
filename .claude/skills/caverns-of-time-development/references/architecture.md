# Architecture

Module map, components, the consumer boundary, packaging and the tech stack.

## Module map

| Path | Holds |
|---|---|
| `caverns_of_time/` | The package: one module per component, behind an explicit public surface (`caverns_of_time/README.md`); exceptions live in one shared module, `caverns_of_time/errors.py`; `chromie.py` is the CLI entry, `console.py` the console helpers, and `_connection.py` and `_fingerprint.py` the shared private modules beside `errors` |
| `dev_tools/` | Verify command, checkers, the test-database command; stdlib only, never shipped (`dev_tools/README.md`) |
| `tests/` | The only test location; database fixtures in `tests/conftest.py` (`tests/README.md`) |
| `.claude/` | Guidance machinery: settings, agents, hooks, path rules, the development skill and the command skills |
| `.github/` | CI workflows, Dependabot, the PR template, code owners |
| `.devcontainer/` | Dev container with the same PostgreSQL image as CI |

## Components

The component names are the codenames; code, docs and messages use them consistently.

| Codename | Lore | Role | Phase |
|---|---|---|---|
| Nozdormu | Aspect of Time, keeper of the one true timeline | Migration ledger: ordered, checksummed, append-only record of every applied step; preflight and apply | 1 |
| Chromie | The guide who takes travellers to past timelines | The CLI and query API | 2 |
| Infinite | The Infinite Dragonflight, who alter the timeline | Drift detector: live schema versus what the ledger says | 2 |
| Epochs | The sands of the hourglass | Run-level snapshots of opted-in tables, tagged with the ledger version | 3 |
| Murozond | Nozdormu's corrupted future | Destructive-change guard on a changeset before apply | 4 |
| Timewalking | Replaying the past | Rebuild the schema at a past ledger version in a scratch database | 4 |

A component exists in the package only from its phase on (`ROADMAP.md`); until then nothing imports it
and no doc describes behaviour that is not built.

## Consumer boundary

- A consumer installs the package and calls its public surface; it never reaches into private modules.
- A consumer's own formats (a changeset tuple, a runner script, a naming scheme) are adapted in the
  consumer's repository. The package never learns them (R1).
- Caverns' own objects live in their own schema inside the consumer's database; everything else in
  that database belongs to the consumer and is changed only by the consumer's own steps.
- The first consumer is Project Acme; its adoption is a pull request in its own repository under
  its own rules.

## Packaging

- One distribution, `caverns-of-time`; one import package, `caverns_of_time`; the version is stated
  once, in `pyproject.toml`, and read at import.
- `dev_tools/` and `tests/` are not packaged.
- The `chromie` console script (`caverns_of_time.chromie:main`) comes with the optional `cli` extra (psycopg 3); the library imports no driver.
- Runtime dependencies are ask-first; the `dev` extra carries the tooling and the test suite's drivers (psycopg 3 and psycopg2).

## Tech stack

Python 3.11+ · PostgreSQL 18 · pytest · ruff · mypy (strict) · pre-commit · gitleaks · GitHub Actions ·
Docker for the local test instance.
