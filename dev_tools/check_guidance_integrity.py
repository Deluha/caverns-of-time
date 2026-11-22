"""Guidance-stack integrity guard (pre-commit hook, verify step and CI).

Checks: (1) every backticked repo path / bare `NAME.md` ref in CLAUDE.md, the
skill files, .claude/rules, command skills and project docs exists, and an
`#anchor` matches a target heading; (2) CLAUDE.md stays within its lean size
budget; (3) SKILL.md frontmatter + routing reference every references/ and
workflows/ file once; (4) a rule token (`R6`) heads a section in at most one
doc; (5) rule files: `paths:` frontmatter, pointer-only body; (6) command
skills: frontmatter + a pointer. Pure checks over explicit paths; main() wires
the real repo. Stdlib only.
Details: the skill's references/checkers.md#checker-guidance-integrity.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "caverns-of-time-development"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# Path-scoped rules are pointers into the skill; detail lives in the skill references.
RULE_MAX_BODY_LINES = 6

# Budget for the auto-loaded CLAUDE.md — deep detail belongs in skill references.
# Raising it is a deliberate, ask-first decision, not a convenience.
CLAUDE_MD_MAX_LINES = 80
CLAUDE_MD_MAX_BYTES = 8 * 1024

# Backticked repo-relative path, e.g. `dev_tools/verify.py`, with an
# optional `#anchor` suffix on .md targets. Requires at least one "/" so bare
# filenames are handled by BARE_DOC_RE instead; placeholder patterns ({pos},
# <filename>, *) are excluded below.
PATH_RE = re.compile(r"`(\.?[\w][\w./ -]*/[\w./ -]*\.(?:md|py|sql|sh|json|yml|toml))(?:#([\w-]+))?`")
# Bare doc filename (README.md, ROADMAP.md, CLAUDE.md, …):
# resolution order in resolve_bare_doc.
BARE_DOC_RE = re.compile(r"`((?:[A-Z][A-Z_]*|README)\.md)(?:#([\w-]+))?`")
PLACEHOLDER_CHARS = set("{}<>*…")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Rule / checklist tokens that must head a section in exactly one doc.
HOME_TOKEN_RE = re.compile(r"^(R\d{1,2}|C\d{1,2})$")

# Directories excluded from the suffix-resolution walk.
WALK_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", "build", "dist"}
# Agent git worktrees hold a second copy of every doc, so the walk skips them as a path pair.
WALK_SKIP_PATHS = {(".claude", "worktrees")}


def _walk_skipped(path: Path, repo_root: Path) -> bool:
    """Whether *path* sits in a skipped dir, judged RELATIVE to *repo_root* so a checker run from
    inside an agent worktree (that worktree is then the root) still walks its own files."""
    parts = path.relative_to(repo_root).parts
    return bool(WALK_SKIP & set(parts)) or parts[:2] in WALK_SKIP_PATHS


# Paths the docs legitimately reference but which are gitignored by design.
GITIGNORED_BY_DESIGN = {".claude/settings.local.json"}


def _rel(path: Path, repo_root: Path) -> str:
    return str(path.relative_to(repo_root)) if repo_root in path.parents else str(path)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def iter_repo_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Tracked-ish repo files, skipping noise dirs."""
    return [p for p in repo_root.rglob("*") if p.is_file() and not _walk_skipped(p, repo_root)]


def resolve_pointer(
    ref: str, doc: Path, repo_files: list[Path], repo_root: Path = REPO_ROOT, skill_dir: Path = SKILL_DIR
) -> Path | None:
    """Resolve a `/`-containing pointer to a real file, or None.

    A pointer resolves if it exists under any sensible base or as a unique path
    suffix of a real repo file (handles skill-root-relative, sibling-relative,
    and abbreviated `subdir/file.py` references alike)."""
    bases = [repo_root, doc.parent, skill_dir, skill_dir / "references"]
    for base in bases:
        if (base / ref).exists():
            return base / ref
    needle = "/" + ref
    matches = [f for f in repo_files if str(f).endswith(needle)]
    return matches[0] if len(matches) == 1 else None


def resolve_bare_doc(
    ref: str, doc: Path, repo_files: list[Path], repo_root: Path = REPO_ROOT, skill_dir: Path = SKILL_DIR
) -> Path | None:
    """Resolve a bare `NAME.md` ref: the referencing doc's dir, then the repo
    root, then the skill root (`SKILL.md`), then a unique repo-wide match by
    filename; None otherwise."""
    for base in (doc.parent, repo_root, skill_dir):
        if (base / ref).is_file():
            return base / ref
    matches = [f for f in repo_files if f.name == ref]
    return matches[0] if len(matches) == 1 else None


def slugify(heading: str) -> str:
    """GitHub-style heading slug: lowercase, drop punctuation except `-`/`_`,
    spaces → `-`."""
    cleaned = re.sub(r"[^\w -]", "", heading.lower())
    return cleaned.strip().replace(" ", "-")


def anchor_matches(anchor: str, headings: list[str]) -> bool:
    """`#token` satisfied by a heading whose slug equals it, starts with it as a
    `-`-joined prefix, or whose first token equals it (`## R6 own changes`
    satisfies `#r6`)."""
    anchor = anchor.lower()
    for heading in headings:
        slug = slugify(heading)
        first = heading.split()[0].lower() if heading.split() else ""
        if slug == anchor or slug.startswith(anchor + "-") or first == anchor:
            return True
    return False


def headings_of(text: str) -> list[str]:
    return [m.group(2) for m in HEADING_RE.finditer(text)]


def project_docs(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Permanent project docs outside the guidance stack: root README,
    RELEASE_NOTES, ROADMAP, CONTRIBUTING, SECURITY, every one-to-three-level `*/README.md`
    and the `.claude/agents/*.md` files."""
    docs = [
        repo_root / name for name in ("README.md", "RELEASE_NOTES.md", "ROADMAP.md", "CONTRIBUTING.md", "SECURITY.md")
    ]
    docs.append(repo_root / "tests" / "README.md")
    for pattern in ("*/README.md", "*/*/README.md", "*/*/*/README.md", ".claude/agents/*.md"):
        docs += sorted(p for p in repo_root.glob(pattern) if not _walk_skipped(p, repo_root))
    seen: set[Path] = set()
    out: list[Path] = []
    for d in docs:
        if d.exists() and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def guidance_docs(repo_root: Path = REPO_ROOT, skill_dir: Path = SKILL_DIR, claude_md: Path = CLAUDE_MD) -> list[Path]:
    """CLAUDE.md + every skill .md + `.claude/rules/*.md` + every other skill's
    SKILL.md."""
    return (
        [claude_md]
        + sorted(skill_dir.rglob("*.md"))
        + sorted((repo_root / ".claude" / "rules").glob("*.md"))
        + _command_skill_files(repo_root, skill_dir)
    )


def _command_skill_files(repo_root: Path, skill_dir: Path) -> list[Path]:
    """SKILL.md of every skill other than the main one."""
    return sorted(p for p in (repo_root / ".claude" / "skills").glob("*/SKILL.md") if skill_dir not in p.parents)


def check_doc_pointers(
    docs: list[Path], repo_files: list[Path], repo_root: Path = REPO_ROOT, skill_dir: Path = SKILL_DIR
) -> list[str]:
    """Every backticked path / bare doc ref in *docs* must exist; an anchored
    pointer must also match a heading of its target (check_anchors)."""
    violations: list[str] = []
    for doc in docs:
        if not doc.exists():
            violations.append(f"{_rel(doc, repo_root)}: file missing")
            continue
        text = doc.read_text(encoding="utf-8")
        name = _rel(doc, repo_root)
        for match in PATH_RE.finditer(text):
            ref, anchor = match.group(1).strip(), match.group(2)
            if PLACEHOLDER_CHARS & set(ref) or " " in ref:
                continue
            if ref in GITIGNORED_BY_DESIGN:
                continue
            target = resolve_pointer(ref, doc, repo_files, repo_root, skill_dir)
            line = _line_of(text, match.start())
            if target is None:
                violations.append(f"{name}:{line}: dead pointer `{ref}`")
            elif anchor:
                violations += check_anchors(name, line, ref, anchor, target)
        for match in BARE_DOC_RE.finditer(text):
            ref, anchor = match.group(1), match.group(2)
            target = resolve_bare_doc(ref, doc, repo_files, repo_root, skill_dir)
            line = _line_of(text, match.start())
            if target is None:
                violations.append(
                    f"{name}:{line}: bare doc ref `{ref}` resolves neither next to "
                    "the doc, at the repo root, nor uniquely repo-wide"
                )
            elif anchor:
                violations += check_anchors(name, line, ref, anchor, target)
    return violations


def check_anchors(doc_name: str, line: int, ref: str, anchor: str, target: Path) -> list[str]:
    """`ref#anchor` must match a heading of *target* (see anchor_matches)."""
    if target.suffix != ".md":
        return [f"{doc_name}:{line}: anchor `{ref}#{anchor}` on a non-markdown target"]
    headings = headings_of(target.read_text(encoding="utf-8"))
    if anchor_matches(anchor, headings):
        return []
    return [f"{doc_name}:{line}: dead anchor `{ref}#{anchor}` (no matching heading)"]


def check_claude_md_budget(
    claude_md: Path = CLAUDE_MD, max_lines: int | None = None, max_bytes: int | None = None
) -> list[str]:
    """CLAUDE.md is auto-loaded context — keep it within the lean budget."""
    max_lines = CLAUDE_MD_MAX_LINES if max_lines is None else max_lines
    max_bytes = CLAUDE_MD_MAX_BYTES if max_bytes is None else max_bytes
    if not claude_md.exists():
        return ["CLAUDE.md: file missing"]
    text = claude_md.read_text(encoding="utf-8")
    n_lines = len(text.splitlines())
    n_bytes = len(text.encode("utf-8"))
    violations: list[str] = []
    if n_lines > max_lines:
        violations.append(
            f"CLAUDE.md: {n_lines} lines exceeds the lean budget of {max_lines} — move detail into the skill references"
        )
    if n_bytes > max_bytes:
        violations.append(
            f"CLAUDE.md: {n_bytes} bytes exceeds the lean budget of {max_bytes} — move detail into the skill references"
        )
    return violations


def _missing_name_description(display: str, frontmatter: str) -> list[str]:
    """A violation per absent `name:` / `description:` key of *frontmatter*."""
    return [
        f"{display}: frontmatter missing `{field}`"
        for field in ("name:", "description:")
        if not re.search(rf"^{field}", frontmatter, re.MULTILINE)
    ]


def _skill_reference_files(skill_dir: Path) -> list[tuple[str, str]]:
    """(display-path, filename) for every references/*.md and workflows/*.md."""
    refs_dir = skill_dir / "references"
    files = sorted(refs_dir.glob("*.md")) + sorted(refs_dir.glob("workflows/*.md"))
    return [(str(f.relative_to(skill_dir)), f.name) for f in files]


def check_skill_structure(skill_dir: Path = SKILL_DIR) -> list[str]:
    """SKILL.md frontmatter is valid and routes to every reference / workflow."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ["SKILL.md: file missing"]
    text = skill_md.read_text(encoding="utf-8")
    violations: list[str] = []
    fm_match = FRONTMATTER_RE.match(text)
    if not fm_match:
        violations.append("SKILL.md: missing YAML frontmatter block")
    else:
        violations += _missing_name_description("SKILL.md", fm_match.group(1))
    for display, name in _skill_reference_files(skill_dir):
        if name not in text:
            violations.append(f"SKILL.md: `{display}` is not referenced from the routing table")
    return violations


def check_routing_unique(skill_dir: Path = SKILL_DIR) -> list[str]:
    """Each references/**.md filename appears in at most one SKILL.md table row
    (a row = a line starting with `|`) — one route per file."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return []
    rows = [line for line in skill_md.read_text(encoding="utf-8").splitlines() if line.lstrip().startswith("|")]
    violations: list[str] = []
    for display, name in _skill_reference_files(skill_dir):
        n = sum(1 for row in rows if name in row)
        if n > 1:
            violations.append(f"SKILL.md: `{display}` is routed from {n} table rows (expected 1)")
    return violations


def check_single_home(docs: list[Path], repo_root: Path = REPO_ROOT) -> list[str]:
    """A rule / checker token (`R6`, `C3`) heads a `##`/`###` section in at
    most one doc — every fact lives in exactly one file."""
    homes: dict[str, list[str]] = {}
    for doc in docs:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for m in HEADING_RE.finditer(text):
            if len(m.group(1)) not in (2, 3):
                continue
            tokens = m.group(2).split()
            if tokens and HOME_TOKEN_RE.match(tokens[0]):
                homes.setdefault(tokens[0], []).append(f"{_rel(doc, repo_root)}:{_line_of(text, m.start())}")
    return [
        f"token `{tok}` has {len(where)} homes: {', '.join(where)}"
        for tok, where in sorted(homes.items())
        if len(where) > 1
    ]


def check_rule_files(repo_root: Path = REPO_ROOT, max_body_lines: int = RULE_MAX_BODY_LINES) -> list[str]:
    """Each `.claude/rules/*.md` has `paths:` frontmatter and a pointer-only body:
    at most *max_body_lines* non-blank lines, with at least one pointer."""
    violations: list[str] = []
    for rule in sorted((repo_root / ".claude" / "rules").glob("*.md")):
        name = _rel(rule, repo_root)
        text = rule.read_text(encoding="utf-8")
        fm = FRONTMATTER_RE.match(text)
        if not fm or not re.search(r"^paths:", fm.group(1), re.MULTILINE):
            violations.append(f"{name}: missing YAML frontmatter with a `paths:` key")
            continue
        body = text[fm.end() :]
        n_lines = sum(1 for line in body.splitlines() if line.strip())
        if n_lines > max_body_lines:
            violations.append(
                f"{name}: body has {n_lines} non-blank lines (max {max_body_lines}) — "
                "pointer-only: move detail into the skill references"
            )
        if not PATH_RE.search(body):
            violations.append(f"{name}: body has no backticked pointer into the skill")
    return violations


def check_command_skills(repo_root: Path = REPO_ROOT, skill_dir: Path = SKILL_DIR) -> list[str]:
    """Each other skill's SKILL.md has `name:` + `description:` frontmatter and a
    body with at least one pointer (thin commands point at the single home)."""
    violations: list[str] = []
    for skill in _command_skill_files(repo_root, skill_dir):
        name = _rel(skill, repo_root)
        text = skill.read_text(encoding="utf-8")
        fm = FRONTMATTER_RE.match(text)
        if not fm:
            violations.append(f"{name}: missing YAML frontmatter block")
            continue
        violations += _missing_name_description(name, fm.group(1))
        if not PATH_RE.search(text[fm.end() :]):
            violations.append(f"{name}: body has no backticked pointer to the single home")
    return violations


def run_all(repo_root: Path = REPO_ROOT, skill_dir: Path = SKILL_DIR, claude_md: Path = CLAUDE_MD) -> list[str]:
    guidance = guidance_docs(repo_root, skill_dir, claude_md)
    docs = guidance + project_docs(repo_root)
    repo_files = iter_repo_files(repo_root)
    return (
        check_doc_pointers(docs, repo_files, repo_root, skill_dir)
        + check_claude_md_budget(claude_md)
        + check_skill_structure(skill_dir)
        + check_routing_unique(skill_dir)
        + check_single_home(docs, repo_root)
        + check_rule_files(repo_root)
        + check_command_skills(repo_root, skill_dir)
    )


def main() -> int:
    violations = run_all()
    if violations:
        print("❌ guidance-integrity violations:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            "\nSee .claude/skills/caverns-of-time-development/references/"
            "checkers.md#checker-guidance-integrity for the guidance-stack rules."
        )
        return 1
    print(
        "✅ guidance stack intact (pointers + anchors, CLAUDE.md budget, "
        "skill routing, single-home tokens, rule files, command skills)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
