# QA, CI & operational policy

The hook/CI guard rail, the verify command, the test database, the GitHub workflows, and the README and
dependency policies. What each checker parses (so you don't break a line shape it greps) is in
`references/checkers.md`. Test inventory: `tests/README.md`.

## Gates

Hooks live in `.pre-commit-config.yaml`; `pre-commit install` wires every stage (the cloud session-start
hook and the dev container do it); bypass one commit with `--no-verify`. All pre-commit-stage hooks also
run on every PR via `ci-pre-commit.yml` (`--all-files`), except `gitleaks`, which CI replaces with a
pinned range-and-tree scan.

| Hook id | Stage | Enforces | Runs locally when |
|---|---|---|---|
| `gitleaks` | pre-commit | Secret scan of the staged changes with `.gitleaks.toml` (`references/checkers.md#checker-secret-scan`) | any commit |
| `project-conventions` | pre-commit | Checker rules C1–C9 (`dev_tools/check_conventions.py`), whole-repo scan | every commit |
| `ruff` | pre-commit | `E`, `F`, `B`, `I`, `UP` (config in `pyproject.toml`); isolated venv | any `.py` |
| `guidance-integrity` | pre-commit | Doc pointers + anchors resolve, `CLAUDE.md` budget, skill routing, single-home tokens (`dev_tools/check_guidance_integrity.py`) | any `.py` / `.md` |
| `docs-sync` | **commit-msg** | Code change in a documented area also touches its doc (`dev_tools/check_docs_sync.py --staged`) | every commit |
| `pytest-fast` | pre-push | `pytest -q -x -m "not db"` with `CAVERNS_REQUIRE_DB` unset: the tests that need no database | every push |

Claude Code sessions add guards from `.claude/settings.json` (not git hooks — they act while editing):

| Guard | When | Does |
|---|---|---|
| `.claude/hooks/session-start.sh` | session start, cloud sessions only (`CAVERNS_SKIP_DEV_SETUP=1` skips it) | Installs the `dev` extra and the git hooks (skipped while the files that decide the install are unchanged), starts the Docker daemon and the test instance, exports `CAVERNS_TEST_DSN` |
| `.claude/hooks/post_edit_check.py` | after every `.py` edit | `ruff check` on the file + `dev_tools/check_conventions.py`; a failure is fed back to the model |
| `permissions.ask` | before an edit to the guidance machinery and CI: `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, `.claude/hooks/`, `.github/workflows/`, `.pre-commit-config.yaml`, `.gitleaks.toml` | Forces a confirmation prompt — the mechanical half of the CLAUDE.md "Ask first" list |
| `permissions.deny` | a `Read` of `.env*`, `*.pem`, `*.pfx`, `*.key`; the `Explore` agent | Keeps secret files out of the session context; `Explore` bypasses `reader` |

## verify

Run before committing (every workflow's Verification section points here): `python dev_tools/verify.py`.
It needs the dev install (`pip install -e ".[dev]"`) and the test instance running
(`python dev_tools/database.py start`). It never stops at the first failure and prints one pass/fail
table; `--base <ref>` changes the docs-sync base (default `origin/main`).

What it runs:

1. `python dev_tools/check_conventions.py`
2. `python dev_tools/check_guidance_integrity.py`
3. `ruff check .`
4. `mypy` — strict, over the package, `dev_tools/` and `.claude/hooks/`
5. `pytest -q` with the database **required**: the step exports `CAVERNS_REQUIRE_DB=1` and drops a
   caller's `PYTEST_ADDOPTS`, so a database test that cannot connect fails instead of skipping, and a
   run in which no database test passed fails as a whole (R8). A changed behaviour means updating its test
   deliberately in the same PR — never by loosening an assertion.
6. `python dev_tools/check_docs_sync.py --base origin/main` — the PR-level docs guard. It sees
   committed changes only; the commit-msg hook covers the commit being made.

A verify run without the test instance is a failed verify, not a partial pass. Delete scratch scripts
before the first commit.

## Test database

One PostgreSQL 18 instance, local to the machine running the tests, holding no data worth keeping.

| Where | How it starts | Connection string |
|---|---|---|
| Cloud session | `.claude/hooks/session-start.sh` starts the Docker daemon, then `dev_tools/database.py start` | exported as `CAVERNS_TEST_DSN`; verify falls back to the same local default |
| Local clone | `python dev_tools/database.py start` (Docker) | `python dev_tools/database.py dsn` |
| Dev container | a `db` service in `.devcontainer/docker-compose.yml`, sharing the app container's network | set in the compose file |
| CI | a `services: postgres` container in `ci-tests.yml` | set in the workflow |

- The image tag is stated in `dev_tools/database.py`; `tests/test_database.py` fails when the CI workflow
  or the dev container names a different one, and when the server is not major version 18.
- The fixtures refuse a connection string that can reach another machine: every `host` and `hostaddr`
  entry must be local, a host must be named, and a service entry or a set `PGHOSTADDR`, `PGSERVICE` or
  `PGSERVICEFILE` is refused (`tests/conftest.py`).
- There is no fallback to another PostgreSQL version: if the instance cannot start, the database tests
  fail and the work stops there.
- ⚑ Pointing any of this at another database, or changing the version, is ask-first.

## workflows

Three GitHub Actions workflows in `.github/workflows/`. Third-party actions are pinned by commit SHA;
every checkout sets `persist-credentials: false`; a run is cancelled by a newer push only on a pull
request, never on `main`. Header comments in each file are the detailed contract.

| File | Trigger | Runs |
|---|---|---|
| `ci-pre-commit.yml` | PR, push to `main` | `pre-commit run --all-files` (dev extra installed; skips the commit-msg `docs-sync` and pre-push `pytest-fast` hooks and the staged-only `gitleaks` hook); then the pinned, sha256-verified gitleaks scan of the PR's commits and the tree (`references/checkers.md#checker-secret-scan`) |
| `ci-docs-sync.yml` | PR | `check_docs_sync.py --base origin/<PR base> --actor <PR author>` (full history for the per-commit `[skip-docs]` attribution; the actor drives the Dependabot exemption) |
| `ci-tests.yml` | PR, push to `main` | `pytest -q` with coverage against a PostgreSQL 18 service container with the database required; gating `mypy`; gating `actionlint` (version pinned in `pyproject.toml`); `CI gate` (`needs` all three, `if: always()`, passes only when every need succeeded) stands for the whole workflow in branch protection |

**Required checks.** Branch protection on `main` requires three checks, one per workflow: `CI gate`,
`Run pre-commit hooks` and `Docs in sync with code`. `CI gate` covers only the jobs of `ci-tests.yml`; a
red pre-commit or docs-sync run blocks a merge only when its own check is required too.

No workflow holds a secret, and none connects to anything but its own service container.

## README policy

- Every code directory keeps a `README.md` with the uniform shape: **Purpose & scope · Files & entry
  points · How it works · Interfaces & contracts · Extending · See also**.
- READMEs carry local detail only — never the hard rules or CI mechanics.
- Root `README.md` = human overview; `RELEASE_NOTES.md` = the single changelog; `ROADMAP.md` = the planned
  next release (scope, phases, status, the phase process); its section leaves when the release is tagged;
  `tests/README.md` = test inventory.
- A README describes what is built. Planned behaviour belongs in `ROADMAP.md` until its phase ships.

## Dependency policy

`pyproject.toml` is the only place dependencies are declared. Runtime dependencies are ask-first and
arrive in the same PR as their first import. The `dev` extra carries the tooling (pytest, ruff, mypy,
pre-commit, actionlint) and the test suite's PostgreSQL driver. Pin minimally; pin exactly only where
a new release must arrive as a reviewed bump (actionlint, the gitleaks version in the workflow).
