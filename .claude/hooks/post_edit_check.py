"""PostToolUse hook for Edit/Write: lint the edited Python file.

After a Python file inside the repo is edited, runs ruff on it and the convention
checker; a failure exits 2 so Claude Code feeds the output back to the model. Any
unexpected error exits 0: the hook must never block editing. Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

REPO_ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def edited_python_file(raw: str) -> str | None:
    """The edited .py path inside the repo, else None."""
    try:
        path = json.loads(raw)["tool_input"]["file_path"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(path, str) or "\0" in path or not path.endswith(".py"):
        return None
    root, full = (os.path.realpath(p) for p in (REPO_ROOT, path))
    return path if full.startswith(root + os.sep) else None


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)


def failures(path: str) -> str:
    """Combined output of the first failing check, or ''."""
    ruff = run([sys.executable, "-m", "ruff", "check", "--force-exclude", path])
    if ruff.returncode != 0 and "No module named ruff" not in ruff.stderr:
        return ruff.stdout + ruff.stderr
    conventions = run([sys.executable, "dev_tools/check_conventions.py"])
    return conventions.stdout + conventions.stderr if conventions.returncode != 0 else ""


def main() -> int:
    try:
        path = edited_python_file(sys.stdin.read())
        report = failures(path) if path else ""
    except Exception:
        return 0
    if report:
        sys.stderr.write(report)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
