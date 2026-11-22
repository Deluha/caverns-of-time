# Workflow: Changing Guidance

Guidance is `CLAUDE.md`, a skill reference or workflow, an agent, a path rule, a command skill, a hook,
`.claude/settings.json`, and the CI and pre-commit configuration that enforce it. Every fact has one home;
the change edits that home and nothing else. Checker mechanics: `references/checkers.md#checker-guidance-integrity`.

## Preconditions

- The fact's one home is found with the routing table of `.claude/skills/caverns-of-time-development/SKILL.md`;
  no second copy is created.
- Budgets: `CLAUDE.md` at most 80 lines and 8 KiB (rewrap or tighten a line rather than raise them); path rules
  (`.claude/rules/`, at most 6 non-blank body lines) and command skills (`.claude/skills/<name>/SKILL.md`) stay
  pointer-only.
- Model tiers (frontmatter `model` and `effort`) are named only in agent frontmatter and the roles table of
  `references/recipes.md#orchestration`.

## Steps

1. Edit the one home. A new reference or workflow gets exactly one routing row in the main `SKILL.md`; an
   adjusted one, exactly one row adjusted.
2. A move of existing text is verbatim: only the heading level and the file change. Prove it with a word-level
   comparison (old text against new, headings stripped) and quote the result in the PR description.
3. A review finding becomes, in order of preference, a checker rule, a test, a hook, or one guidance line
   (`references/recipes.md#lessons-loop`); a declined finding is listed with the reason.
4. A pointer into a heading you rename or re-level keeps resolving: grep the old anchor repo-wide first; the
   checker reports a dead one.
5. A new permission gate goes in `.claude/settings.json`; when it mirrors an "Ask first" bullet, both change
   together, and `references/qa-and-ci.md#gates` (the `permissions.ask` row) lists it.

## Verification

Run `references/qa-and-ci.md#verify`, then the task-specific checks below.

- [ ] `python dev_tools/check_guidance_integrity.py` reports nothing for the touched files
- [ ] `pytest -q tests/test_check_guidance_integrity.py tests/test_check_docs_sync.py` when a checker or a budget constant changed
- [ ] `actionlint .github/workflows/*.yml` when a workflow changed
- [ ] The prompts of `evals/evals.json` are run by hand before a change to `CLAUDE.md`, the skill or `.claude/rules/`
      merges (`references/recipes.md#lessons-loop`); the outcome is in the PR description
- [ ] A moved text has its word-level comparison in the PR description

## Docs to update

- `references/checkers.md` when a checker rule changes
- `references/qa-and-ci.md` when a hook, a permission gate or a workflow changes (docs-sync needs it for
  `.github/workflows/` and `.claude/hooks/`)
- `references/recipes.md` (the roles table) when `.claude/agents/` changes (docs-sync needs it)
- `references/architecture.md` (the `.claude/` row of the module map) when a skill, rule, agent or hook is added or removed

## ⚑ Ask first

- The hard rules and the ask-first list of `CLAUDE.md`; any budget constant of the checker.
- Agent tiers: the frontmatter `model` and `effort` of any agent; the tool list of any agent.
- `.claude/settings.json` permissions and hooks; the session-start hook's opt-out.
- `.github/workflows/`, `.pre-commit-config.yaml`, `.gitleaks.toml`: any relaxation of a guard, an allowlist
  entry for a secret finding, a new third-party action or download.
- Anything that changes which database a test, a hook or a workflow connects to, or its version.
