"""Docs-sync guard: a code change in a documented area must touch that area's doc.

Fails a change that adds, modifies, renames or deletes code in a documented area
without touching that area's doc, CLAUDE.md, or the main skill's SKILL.md /
references/. A forcing-function reminder only: it cannot verify the prose.
Modes: --base <ref> (CI) or --staged <commit-msg-file> (local commit-msg hook).
[skip-docs] / [docs-ok] on a line of its own in a commit message is the escape
hatch (a message that merely mentions it does not count) and exempts only that
commit's own files; Dependabot's own PRs are exempt in the areas it can only
touch mechanically (--actor). A git failure exits 2, never green. Stdlib only.
Details: the skill's references/checkers.md#checker-docs-sync.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Escape tokens that exempt a changeset from the guard (see the module docstring).
SKIP_TOKENS = ("[skip-docs]", "[docs-ok]")

# File extensions that count as a "code change" per area; everything else is .py/.sql.
DEFAULT_SUFFIXES = (".py", ".sql")
AREA_SUFFIXES = {
    ".github/workflows": (".yml", ".yaml"),
    ".claude/agents": (".md",),
    ".claude/hooks": (".py", ".sh"),
}

# Docs that satisfy ANY area (project-wide guidance): CLAUDE.md, the main skill's
# SKILL.md and its references/ files. Other files under .claude/skills/ (evals,
# command skills) are not documentation.
CLAUDE_MD = "CLAUDE.md"
SKILL_DIR = ".claude/skills/caverns-of-time-development/"
SKILL_DOC_PREFIXES = (SKILL_DIR + "SKILL.md", SKILL_DIR + "references/")

# Dependabot bumps pinned action SHAs in `.github/workflows/` and never writes
# docs, so the guard would fail every one of its PRs forever. Exempt that bot in
# that area only; a human PR touching the same files is unaffected.
DEPENDABOT_ACTOR = "dependabot[bot]"
DEPENDABOT_EXEMPT_AREAS = frozenset({".github/workflows"})

# Touch-map: each documented code area and the doc that tracks it. Ordered
# most-specific-first: a changed file is attributed to the FIRST matching prefix only.
AREAS = [
    ("caverns_of_time", "caverns_of_time/", "caverns_of_time/README.md"),
    ("dev_tools", "dev_tools/", "dev_tools/README.md"),
    ("tests", "tests/", "tests/README.md"),
    (".github/workflows", ".github/workflows/", SKILL_DIR + "references/qa-and-ci.md"),
    (".claude/agents", ".claude/agents/", SKILL_DIR + "references/recipes.md"),
    (".claude/hooks", ".claude/hooks/", SKILL_DIR + "references/qa-and-ci.md"),
]


class GitError(RuntimeError):
    """A git command failed (unknown ref, no merge base, git missing)."""


def git(*args: str) -> str:
    """Run a git command at the repo root and return stripped stdout; raise GitError on failure."""
    try:
        out = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise GitError(f"git {' '.join(args)} failed: {e.stderr.strip()}") from e
    except FileNotFoundError as e:
        raise GitError(f"git {' '.join(args)} failed: {e}") from e


def _lines(out: str) -> set[str]:
    return {line.strip() for line in out.splitlines() if line.strip()}


def changed_files_vs_base(base: str) -> set[str]:
    """Files this branch changed relative to *base* (merge-base diff)."""
    return _lines(git("diff", "--name-only", "--diff-filter=ACMRD", f"{base}...HEAD"))


def changed_files_staged() -> set[str]:
    """Currently staged files (local commit-msg hook mode)."""
    return _lines(git("diff", "--cached", "--name-only", "--diff-filter=ACMRD"))


def parse_log(raw: str) -> list[tuple[str, set[str]]]:
    """Parse `git log --format=%x00%H%x00%B%x00 --name-only` into (message, files) per commit."""
    parts = raw.split("\x00")[1:]  # leading '' before the first commit
    return [(parts[i + 1], _lines(parts[i + 2])) for i in range(0, len(parts) - 2, 3)]


def commits_vs_base(base: str) -> list[tuple[str, set[str]]]:
    """(message, files) of every NON-merge commit in base..HEAD (files: ACMRD)."""
    raw = git(
        "-c", "core.quotepath=off", "log", "--no-merges", "--format=%x00%H%x00%B%x00",
        "--name-only", "--diff-filter=ACMRD", f"{base}..HEAD",
    )  # fmt: skip
    return parse_log(raw)


def skip_token(message: str) -> str | None:
    """First SKIP_TOKENS token that equals a stripped line of *message*, else None."""
    lines = {line.strip() for line in message.splitlines()}
    return next((t for t in SKIP_TOKENS if t in lines), None)


def token_exempt_files(commits: Sequence[tuple[str, set[str]]]) -> frozenset[str]:
    """Files touched ONLY by commits whose message carries a SKIP_TOKENS token."""
    skipped: set[str] = set()
    plain: set[str] = set()
    for message, files in commits:
        (skipped if skip_token(message) else plain).update(files)
    return frozenset(skipped - plain)


def area_of(path: str) -> str | None:
    """Label of the first AREAS prefix a code file falls under, or None."""
    for label, prefix, _doc in AREAS:
        if path.startswith(prefix):
            return label if path.endswith(AREA_SUFFIXES.get(label, DEFAULT_SUFFIXES)) else None
    return None


def _global_doc_touched(changed: set[str]) -> bool:
    return any(c == CLAUDE_MD or c.startswith(SKILL_DOC_PREFIXES) for c in changed)


def evaluate(
    changed: set[str], messages: str, actor: str = "", *, exempt: frozenset[str] = frozenset()
) -> tuple[int, list[str]]:
    """Pure classifier: apply the touch-map and return (exit_code, output_lines).

    *exempt* = code files to ignore when attributing areas (they sit in [skip-docs]
    commits); doc touches are still read from the full *changed*. Pass (0) when no
    documented code area changed, an escape token is in *messages* (--staged: one
    commit), every changed area also has a doc touched, or the only areas left are
    exempt (*exempt* files, or *actor*'s own areas). Fail (1) otherwise.
    """
    token = skip_token(messages)
    if token:
        return 0, [f"✅ docs-sync skipped via {token} on a line of its own in the commit message."]

    hits: dict[str, list[str]] = {}
    n_exempt = sum(1 for f in exempt if area_of(f))
    for f in changed - exempt:
        label = area_of(f)
        if label:
            hits.setdefault(label, []).append(f)

    bot_exempt = DEPENDABOT_EXEMPT_AREAS if actor == DEPENDABOT_ACTOR else frozenset()
    skipped = sorted(bot_exempt & hits.keys())
    for label in skipped:
        del hits[label]

    if not hits:
        if n_exempt:
            return 0, [f"✅ docs-sync: the remaining code changes sit in [skip-docs] commits ({n_exempt} file(s))."]
        note = f" ({', '.join(skipped)} exempt for {actor})" if skipped else ""
        return 0, [f"✅ docs-sync: no documented code areas changed{note}."]

    doc_for = {label: doc for label, _prefix, doc in AREAS}
    info = [f"ℹ️  docs-sync: {n_exempt} file(s) in [skip-docs] commits not checked."] if n_exempt else []
    if _global_doc_touched(changed):
        return 0, ["✅ docs-sync: CLAUDE.md / skill updated alongside code changes.", *info]
    violations = sorted((label, sorted(files)) for label, files in hits.items() if doc_for[label] not in changed)
    if not violations:
        return 0, ["✅ docs-sync: every changed area has a matching doc update.", *info]

    lines = [f"❌ docs-sync: {len(violations)} code area(s) changed without a doc update:\n"]
    for label, files in violations:
        shown = ", ".join(files[:5]) + (" …" if len(files) > 5 else "")
        lines.append(f"   [{label}] {shown}")
        lines.append(f"       → update {doc_for[label]}, or CLAUDE.md, or the main skill's SKILL.md / references/")
    lines.append(
        "\n   If this change genuinely needs no doc update, put [skip-docs] on its own line "
        "in the message of the commit that makes it."
    )
    return 1, lines + info


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Docs-sync guard for Caverns of Time changes.")
    parser.add_argument("--base", help="Base ref to diff against (CI mode), e.g. origin/main.")
    parser.add_argument("--staged", action="store_true", help="Use staged files (local commit-msg mode).")
    parser.add_argument("--actor", default="", help="PR author login (CI mode); exempts Dependabot's own areas.")
    parser.add_argument("message_file", nargs="?", help="Commit-message file (the commit-msg hook passes this).")
    args = parser.parse_args(argv)

    exempt: frozenset[str] = frozenset()
    try:
        if args.staged:
            changed = changed_files_staged()
            messages = Path(args.message_file).read_text(encoding="utf-8") if args.message_file else ""
        elif args.base:
            changed = changed_files_vs_base(args.base)
            messages = ""  # per-commit tokens only: no whole-PR short-circuit
            exempt = token_exempt_files(commits_vs_base(args.base)) & changed
        else:
            parser.error("provide --base <ref> (CI) or --staged <commit-msg-file> (local hook)")
    except GitError as e:
        print(f"❌ docs-sync: {e}")
        return 2

    code, lines = evaluate(changed, messages, args.actor, exempt=exempt)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
