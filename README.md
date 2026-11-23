# Caverns of Time

A recorded timeline for PostgreSQL. Caverns of Time is a generic Python library and CLI that will give a
database a past you can query: which schema changes were applied and when, what opted-in tables looked
like at an earlier run, and whether anything changed outside the timeline.

**Status: in development, nothing released.** Phase 0 (the development process), Phase 1 (Nozdormu),
Phase 2 (Chromie and Infinite) and Phase 3 (Epochs) are built; the CLI is installed with the `cli` extra
(`pip install 'caverns-of-time[cli]'`). `ROADMAP.md` holds the plan and its status.

## Components

Named after the keepers of time in Warcraft lore. Each arrives in its roadmap phase.

| Component | Role | Phase |
|---|---|---|
| **Nozdormu** | Migration ledger: ordered, checksummed, append-only record of every applied step | 1 |
| **Chromie** | CLI and query API | 2 |
| **Infinite** | Drift detector: live schema versus what the ledger says | 2 |
| **Epochs** | Run-level snapshots of opted-in tables | 3 |
| **Murozond** | Destructive-change guard on a changeset before apply | 4 |
| **Timewalking** | Rebuild the schema at a past ledger version in a scratch database | 4 |

## Install

For development (Python 3.11 or newer, Docker for the test database):

```bash
git clone https://github.com/Deluha/caverns-of-time.git
cd caverns-of-time
pip install -e ".[dev]"
pre-commit install
python dev_tools/database.py start
python dev_tools/verify.py
```

A Codespace or Dev Container sets all of this up itself (`.devcontainer/devcontainer.json`).

## Repository layout

| Path | Holds |
|---|---|
| `caverns_of_time/` | The package (`caverns_of_time/README.md`) |
| `dev_tools/` | The verify command, the checkers, the test-database command (`dev_tools/README.md`) |
| `tests/` | The test suite; database tests run against PostgreSQL 18 (`tests/README.md`) |
| `.claude/` | The Claude Code development process: guide, agents, hooks, skills |
| `.github/` | CI workflows |

## Development process

`CLAUDE.md` holds the operating principles, the nine hard rules and the ask-first list; the development
skill under `.claude/skills/` routes each task to its one authoritative file. `CONTRIBUTING.md` covers
setup, the QA gates and the PR flow. The process is ported from Project Blackwing, the first planned
consumer of this library.

## Release & versioning

The version is stated once, in `pyproject.toml`. `RELEASE_NOTES.md` records what shipped; `ROADMAP.md`
records what is planned and leaves a release's section when that release is tagged.

## Licence

MIT (`LICENSE`).
