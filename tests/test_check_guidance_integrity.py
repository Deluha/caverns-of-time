"""Tests for dev_tools/check_guidance_integrity.py: each check on a miniature
CLAUDE.md / skill / docs tree under tmp_path (pass and fail cases)."""

from pathlib import Path

import pytest

from dev_tools import check_guidance_integrity as gi

SKILL_MD = """---
name: mini
description: mini skill
---
| Task | Read |
|---|---|
| Conventions | `references/conventions.md` |
| Add a thing | `references/workflows/add-thing.md` |
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path):
    """Minimal repo: CLAUDE.md, a skill with one reference + one workflow, a
    module README, an agent file and a code file to point at."""
    skill = tmp_path / ".claude" / "skills" / "mini"
    _write(tmp_path / "CLAUDE.md", "# Guide\n\nSee `dev_tools/tool.py` and `README.md`.\n")
    _write(skill / "SKILL.md", SKILL_MD)
    _write(
        skill / "references" / "conventions.md",
        "# Conventions\n\n## R6 own_changes\n\nbody\n\n### Status output & icons\n",
    )
    _write(skill / "references" / "workflows" / "add-thing.md", "# Add\n\nSee `GUIDE.md`.\n")
    _write(tmp_path / "README.md", "# Root\n")
    _write(tmp_path / "dev_tools" / "tool.py", "x = 1\n")
    _write(tmp_path / "dev_tools" / "README.md", "# dev_tools\n")
    _write(tmp_path / "docs_area" / "GUIDE.md", "# Routine\n")
    _write(tmp_path / ".claude" / "agents" / "bot.md", "# Bot\n\n`docs_area/GUIDE.md`\n")
    return tmp_path, skill


def _run(tree, **kw):
    root, skill = tree
    return gi.run_all(root, skill, root / "CLAUDE.md", **kw)


# ── whole tree ────────────────────────────────────────────────────────────────


def test_clean_tree_passes(tree):
    assert _run(tree) == []


def test_project_docs_include_new_locations(tree):
    root, _ = tree
    deep = _write(root / "a" / "b" / "c" / "README.md", "# deep\n")
    tests_doc = _write(root / "tests" / "README.md", "# tests\n")
    sec = _write(root / "SECURITY.md", "# sec\n")
    contrib = _write(root / "CONTRIBUTING.md", "# contrib\n")
    roadmap = _write(root / "ROADMAP.md", "# roadmap\n")
    docs = gi.project_docs(root)
    assert {deep, tests_doc, sec, contrib, roadmap, root / ".claude" / "agents" / "bot.md"} <= set(docs)
    assert len(docs) == len(set(docs))


# ── pointers, anchors, bare refs ──────────────────────────────────────────────


def test_dead_pointer_reports_file_and_line(tree):
    root, _ = tree
    _write(root / "CLAUDE.md", "# G\n\n`dev_tools/missing.py`\n")
    out = _run(tree)
    assert out == ["CLAUDE.md:3: dead pointer `dev_tools/missing.py`"]


def test_placeholder_and_gitignored_refs_are_skipped(tree):
    root, _ = tree
    _write(root / "CLAUDE.md", "# G\n\n`pkg_x/{pos}.py` `.claude/settings.local.json`\n")
    assert _run(tree) == []


@pytest.mark.parametrize("anchor", ["r6", "r6-own_changes", "status-output--icons", "status-output"])
def test_anchor_forms_resolve(tree, anchor):
    root, _ = tree
    _write(root / "CLAUDE.md", f"# G\n\n`references/conventions.md#{anchor}`\n")
    assert _run(tree) == []


def test_dead_anchor_fails_with_line(tree):
    root, _ = tree
    _write(root / "CLAUDE.md", "# G\n\n\n`references/conventions.md#r7`\n")
    out = _run(tree)
    assert len(out) == 1 and out[0].startswith("CLAUDE.md:4: dead anchor `references/conventions.md#r7`")


def test_slugify_is_github_style():
    assert gi.slugify("Status output & icons") == "status-output--icons"
    assert gi.slugify("R6 own_changes") == "r6-own_changes"


def test_bare_ref_resolves_next_to_doc_then_root(tree):
    root, skill = tree
    # README.md next to the referencing doc wins; CLAUDE.md falls back to the root.
    _write(root / "dev_tools" / "README.md", "# d\n\n`README.md` `CLAUDE.md`\n")
    assert _run(tree) == []


def test_walk_ignores_agent_worktrees_but_not_other_duplicates(tree):
    root, _ = tree
    _write(root / "CLAUDE.md", "# G\n\n`GUIDE.md`\n")
    _write(root / ".claude" / "worktrees" / "x" / "docs_area" / "GUIDE.md", "# Copy\n")
    assert _run(tree) == []
    _write(root / "worktrees" / "GUIDE.md", "# Elsewhere\n")
    assert any("GUIDE.md" in line for line in _run(tree))


def test_a_checker_rooted_inside_a_worktree_still_walks_its_own_files(tmp_path):
    root = tmp_path / ".claude" / "worktrees" / "agent1"
    _write(root / "docs_area" / "GUIDE.md", "# Routine\n")
    _write(root / "CLAUDE.md", "# G\n\n`GUIDE.md`\n")
    _write(root / "dev_tools" / "README.md", "# d\n\n`docs_area/GUIDE.md`\n")
    files = gi.iter_repo_files(root)
    assert root / "docs_area" / "GUIDE.md" in files
    assert (
        gi.resolve_bare_doc("GUIDE.md", root / "CLAUDE.md", files, root, root / "skill")
        == root / "docs_area" / "GUIDE.md"
    )
    assert (
        gi.check_doc_pointers([root / "CLAUDE.md", root / "dev_tools" / "README.md"], files, root, root / "skill") == []
    )
    assert root / "dev_tools" / "README.md" in gi.project_docs(root)


def test_project_docs_skip_a_worktree_readme(tree):
    root, _ = tree
    copy = _write(root / ".claude" / "worktrees" / "x" / "README.md", "# copy\n")
    nested = _write(root / ".claude" / "worktrees" / "x" / "dev_tools" / "README.md", "# copy\n")
    docs = gi.project_docs(root)
    assert copy not in docs and nested not in docs
    assert root / "dev_tools" / "README.md" in docs


def test_bare_ref_unresolvable_fails(tree):
    root, _ = tree
    _write(root / "CLAUDE.md", "# G\n\n`OPERATIONS.md`\n")
    out = _run(tree)
    assert out == [
        "CLAUDE.md:3: bare doc ref `OPERATIONS.md` resolves neither next to "
        "the doc, at the repo root, nor uniquely repo-wide"
    ]


def test_bare_ref_ignores_lowercase_and_placeholders(tree):
    root, _ = tree
    _write(root / "CLAUDE.md", "# G\n\n`notes.md` `<NAME>.md`\n")
    assert _run(tree) == []


def test_bare_ref_anchor_is_checked(tree):
    root, _ = tree
    _write(root / "CLAUDE.md", "# G\n\n`README.md#root` `README.md#nope`\n")
    out = _run(tree)
    assert out == ["CLAUDE.md:3: dead anchor `README.md#nope` (no matching heading)"]


# ── CLAUDE.md budget ──────────────────────────────────────────────────────────


def test_budget_constant_is_current():
    assert gi.CLAUDE_MD_MAX_LINES == 80 and gi.CLAUDE_MD_MAX_BYTES == 8 * 1024


def test_budget_over_lines_fails(tree):
    root, _ = tree
    out = gi.check_claude_md_budget(root / "CLAUDE.md", max_lines=1)
    assert len(out) == 1 and "exceeds the lean budget of 1" in out[0]
    assert gi.check_claude_md_budget(root / "CLAUDE.md", max_lines=50) == []


def test_budget_counts_terminated_lines_exactly(tmp_path):
    path = _write(tmp_path / "CLAUDE.md", "x\n" * 80)
    assert gi.check_claude_md_budget(path, max_lines=80) == []
    path = _write(tmp_path / "CLAUDE.md", "x\n" * 81)
    out = gi.check_claude_md_budget(path, max_lines=80)
    assert len(out) == 1 and "81 lines exceeds the lean budget of 80" in out[0]


def test_budget_over_bytes_fails(tree):
    root, _ = tree
    out = gi.check_claude_md_budget(root / "CLAUDE.md", max_bytes=4)
    assert len(out) == 1 and "bytes exceeds" in out[0]


# ── skill structure / routing ────────────────────────────────────────────────


def test_unreferenced_reference_and_workflow_fail(tree):
    _, skill = tree
    _write(skill / "references" / "extra.md", "# extra\n")
    _write(skill / "references" / "workflows" / "orphan.md", "# o\n")
    out = gi.check_skill_structure(skill)
    assert out == [
        "SKILL.md: `references/extra.md` is not referenced from the routing table",
        "SKILL.md: `references/workflows/orphan.md` is not referenced from the routing table",
    ]


def test_missing_frontmatter_fields_fail(tree):
    _, skill = tree
    _write(skill / "SKILL.md", "---\nname: x\n---\n`conventions.md` `add-thing.md`\n")
    assert gi.check_skill_structure(skill) == ["SKILL.md: frontmatter missing `description:`"]
    _write(skill / "SKILL.md", "no frontmatter `conventions.md` `add-thing.md`\n")
    assert gi.check_skill_structure(skill) == ["SKILL.md: missing YAML frontmatter block"]


def test_routing_unique_passes_then_fails_on_duplicate_row(tree):
    _, skill = tree
    assert gi.check_routing_unique(skill) == []
    _write(skill / "SKILL.md", SKILL_MD + "| Again | `references/conventions.md` |\n")
    assert gi.check_routing_unique(skill) == [
        "SKILL.md: `references/conventions.md` is routed from 2 table rows (expected 1)"
    ]


def test_routing_unique_ignores_non_table_mentions(tree):
    _, skill = tree
    _write(skill / "SKILL.md", SKILL_MD + "\nProse mentioning conventions.md again.\n")
    assert gi.check_routing_unique(skill) == []


# ── single home ───────────────────────────────────────────────────────────────


def test_single_home_token_in_two_files_fails(tree):
    root, skill = tree
    _write(root / "dev_tools" / "README.md", "# d\n\n### R6 elsewhere\n")
    out = _run(tree)
    assert out == ["token `R6` has 2 homes: .claude/skills/mini/references/conventions.md:3, dev_tools/README.md:3"]


def test_single_home_ignores_level_1_and_4_and_non_tokens(tree):
    root, _ = tree
    _write(root / "dev_tools" / "README.md", "# R6 top\n\n#### R6 deep\n\n## R6x nope\n\n## C3 a\n")
    _write(root / "README.md", "# Root\n\n### C3 b\n")
    out = _run(tree)
    assert len(out) == 1 and out[0].startswith("token `C3` has 2 homes")


# ── rule files / command skills ──────────────────────────────────────────────

RULE_OK = '---\npaths:\n  - "dev_tools/**"\n---\nFollow `dev_tools/tool.py`.\n'
CMD_OK = "---\nname: cmd\ndescription: does a thing\n---\nFollow `dev_tools/tool.py`.\n"


def test_rule_max_body_lines_is_pinned():
    assert gi.RULE_MAX_BODY_LINES == 6


def test_rule_dead_pointer_reported_by_run_all(tree):
    root, _ = tree
    _write(root / ".claude" / "rules" / "r.md", RULE_OK.replace("tool.py", "gone.py"))
    assert _run(tree) == [".claude/rules/r.md:5: dead pointer `dev_tools/gone.py`"]


def test_rule_files_valid_passes(tree):
    root, _ = tree
    _write(root / ".claude" / "rules" / "r.md", RULE_OK)
    assert gi.check_rule_files(root) == []
    assert _run(tree) == []


def test_rule_files_missing_paths_fails(tree):
    root, _ = tree
    _write(root / ".claude" / "rules" / "a.md", RULE_OK.replace("paths:", "globs:"))
    _write(root / ".claude" / "rules" / "b.md", "Follow `dev_tools/tool.py`.\n")
    out = gi.check_rule_files(root)
    assert len(out) == 2 and all("`paths:`" in v for v in out)


def test_rule_files_body_over_cap_fails(tree):
    root, _ = tree
    _write(root / ".claude" / "rules" / "r.md", RULE_OK + "extra\n" * 6)
    out = gi.check_rule_files(root)
    assert len(out) == 1 and "7 non-blank lines (max 6)" in out[0]
    assert gi.check_rule_files(root, max_body_lines=7) == []


def test_rule_files_body_without_pointer_fails(tree):
    root, _ = tree
    _write(root / ".claude" / "rules" / "r.md", '---\npaths:\n  - "x/**"\n---\nno pointer here\n')
    out = gi.check_rule_files(root)
    assert len(out) == 1 and "no backticked pointer" in out[0]


def test_command_skill_valid_passes(tree):
    root, skill = tree
    _write(root / ".claude" / "skills" / "cmd" / "SKILL.md", CMD_OK)
    assert gi.check_command_skills(root, skill) == []
    assert _run(tree) == []


def test_command_skill_missing_description_fails(tree):
    root, skill = tree
    _write(root / ".claude" / "skills" / "cmd" / "SKILL.md", CMD_OK.replace("description:", "summary:"))
    assert gi.check_command_skills(root, skill) == [".claude/skills/cmd/SKILL.md: frontmatter missing `description:`"]


def test_command_skill_body_without_pointer_fails(tree):
    root, skill = tree
    _write(root / ".claude" / "skills" / "cmd" / "SKILL.md", "---\nname: cmd\ndescription: d\n---\nDo it.\n")
    out = gi.check_command_skills(root, skill)
    assert len(out) == 1 and "no backticked pointer" in out[0]


def test_second_skill_dead_pointer_reported_by_run_all(tree):
    root, _ = tree
    _write(root / ".claude" / "skills" / "cmd" / "SKILL.md", CMD_OK.replace("tool.py", "gone.py"))
    assert _run(tree) == [".claude/skills/cmd/SKILL.md:5: dead pointer `dev_tools/gone.py`"]


def test_run_all_reports_rule_file_without_paths(tree):
    root, _ = tree
    _write(root / ".claude" / "rules" / "r.md", "Follow `dev_tools/tool.py`.\n")
    assert _run(tree) == [".claude/rules/r.md: missing YAML frontmatter with a `paths:` key"]


def test_run_all_reports_second_skill_without_description(tree):
    root, _ = tree
    _write(root / ".claude" / "skills" / "cmd" / "SKILL.md", CMD_OK.replace("description:", "summary:"))
    assert _run(tree) == [".claude/skills/cmd/SKILL.md: frontmatter missing `description:`"]


# ── live repo ─────────────────────────────────────────────────────────────────


def test_real_guidance_stack_is_intact():
    assert gi.run_all() == []
