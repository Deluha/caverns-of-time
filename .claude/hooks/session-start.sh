#!/bin/bash
# SessionStart hook: Claude Code cloud sessions get the dev tools (pytest, ruff, mypy,
# pre-commit), the git hooks and the PostgreSQL 18 test instance. Local sessions are
# left alone (the dev container does the same on create). CAVERNS_SKIP_DEV_SETUP=1 skips it.
# A failure to start the database never blocks the session: verify then fails its
# pytest step, which is the intended signal (hard rule 8).
set -euo pipefail
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ] || [ "${CAVERNS_SKIP_DEV_SETUP:-}" = "1" ]; then exit 0; fi
cd "${CLAUDE_PROJECT_DIR:-.}"
# Reinstall only when a file that decides what gets installed changed since the last success.
hashes="$(python3 -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/.caverns-session-start.sha256"
if ! sha256sum --check --status "$hashes" 2>/dev/null; then
  rm -f "$hashes"
  python3 -m pip install --quiet --editable ".[dev]" >&2
  sha256sum .claude/hooks/session-start.sh pyproject.toml > "$hashes" || rm -f "$hashes"
fi
# A fresh clone on a cached container has the packages but not the git hooks.
if [ ! -f .git/hooks/pre-commit ]; then python3 -m pre_commit install --allow-missing-config >&2 || true; fi
# The test database: the Docker daemon is not running in a fresh cloud container.
if ! docker info >/dev/null 2>&1; then
  (nohup dockerd >/tmp/caverns-dockerd.log 2>&1 &)
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
python3 dev_tools/database.py start >&2 || true
# Later commands of the session read the connection string from the environment.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export CAVERNS_TEST_DSN=$(python3 dev_tools/database.py dsn)" >> "$CLAUDE_ENV_FILE"
fi
