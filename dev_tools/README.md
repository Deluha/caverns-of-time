# dev_tools

## Purpose & scope

Development tooling: the one verify command, the three checkers and the test-database command. Standard
library only, so each script runs on a bare Python. Not part of the distribution.

## Files & entry points

| File | Command | Does |
|---|---|---|
| `verify.py` | `python dev_tools/verify.py [--base <ref>]` | Runs every QA step and prints one pass/fail table; requires the test database |
| `check_conventions.py` | `python dev_tools/check_conventions.py` | Convention rules C1–C9 |
| `check_guidance_integrity.py` | `python dev_tools/check_guidance_integrity.py` | Guidance pointers, anchors, `CLAUDE.md` budget, skill routing, rule files, command skills |
| `check_docs_sync.py` | `python dev_tools/check_docs_sync.py --base <ref>` or `--staged <commit-msg-file>` | Fails a code change in a documented area without its doc |
| `database.py` | `python dev_tools/database.py start \| stop \| status \| dsn` | The local PostgreSQL 18 test instance (a Docker container on 127.0.0.1) |

## How it works

- `verify.py` runs conventions, guidance integrity, `ruff check .`, `mypy`, `pytest -q` and docs-sync in
  that order, never stopping at the first failure. For the pytest step it exports `CAVERNS_REQUIRE_DB=1`
  and, when `CAVERNS_TEST_DSN` is unset, the local default from `database.py`.
- The checkers are pure functions over explicit inputs; each `main()` wires the real repository. What
  each one parses: `.claude/skills/caverns-of-time-development/references/checkers.md`.
- `database.py` holds the image tag, port and throwaway password of the test instance in one place.
  `start` is idempotent and waits until the server accepts TCP connections.

## Interfaces & contracts

| Script | Exit codes |
|---|---|
| `verify.py` | 0 every step passed · 1 a step failed · 2 git failure |
| `check_conventions.py` | 0 clean · 1 violations · 2 git failure |
| `check_guidance_integrity.py` | 0 clean · 1 violations |
| `check_docs_sync.py` | 0 in sync or exempt · 1 a doc is missing · 2 git failure |
| `database.py` | 0 done · 1 the instance could not start or is not running |

`check_docs_sync.git` and `GitError` are shared by `verify.py` and `check_conventions.py`.

## Extending

A new convention rule is a new `check_*` function in `check_conventions.py`, added to `run_all`, with a
test and a row in the checkers reference. A new documented area is a row in `AREAS` of
`check_docs_sync.py`. Changing a checker is a guidance change: follow the changing-guidance workflow.

## See also

`tests/README.md` · `.claude/skills/caverns-of-time-development/references/qa-and-ci.md`
