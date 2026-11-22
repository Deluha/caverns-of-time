# Recipes

Copy-paste utility: pre-change greps, orchestration, the Implementation Decision format and the lessons loop.

## Grep before coding

```bash
# An existing helper, constant or statement BEFORE writing a new one (R2)
grep -rn "def <name>\|<CONSTANT>" --include="*.py" caverns_of_time dev_tools
# Every call site of a function BEFORE touching its signature (public ones are ask-first)
grep -rn "<function_name>(" --include="*.py" caverns_of_time tests dev_tools
# Every statement that touches one of Caverns' own tables BEFORE changing it (R5, R6)
grep -rn "<table_name>" --include="*.py" --include="*.sql" caverns_of_time tests
# Every doc and checker that names a file BEFORE renaming or moving it
grep -rn "<file_name>" --include="*.md" --include="*.py" --include="*.yml" --include="*.json" .
```

Fan out `reader` sub-agents (read-only) for the sweep when a task spans 2+ modules.

## Orchestration

Three roles plus a reviewer and a scribe. The **orchestrator** (the session model) thinks, judges and signs
off; the **writer** makes repo edits; the **reader** does wide reads; the **reviewer** reviews diffs. Model
tiers live ONLY in the agent frontmatter under `.claude/agents/` and in this table:

| Role | Agent | Pinned model / effort | Tools |
|---|---|---|---|
| Orchestrator | the session itself | session model | all |
| Writer | `implementer` | `sonnet`, medium (`high` on the call for bug fixes, ledger and schema work, edge-case-heavy work) | Read, Grep, Glob, Edit, Write, Bash |
| Reader | `reader` | `haiku`, medium (never `low`: a run that stopped early is rerun at higher effort) | Read, Grep, Glob |
| Reviewer | `reviewer` | inherits the session model, high | Read, Grep, Glob, Bash |
| Scribe | `scribe` | `haiku`, medium; notes and drafts outside the repo, never committed (hard rule 4) | Read, Write |

Call them by `subagent_type` and **never put a `model` on that Agent call** — a call-level `model`
overrides the frontmatter. Independent reads and edits go out in one message, in parallel; at most 3
writers at once, and only when their files don't share an edit. Never `xhigh` or `max` as a default.

**Small-edit threshold.** The conditions are stated once, in `CLAUDE.md` principle 2; when all hold the
orchestrator edits directly. Typical small edits: docs, a typo, a rename, a constant move, a decided
review fix. Anything else gets a brief and goes to `implementer`. When unsure, delegate. The orchestrator
also reads up to three files itself; wider sweeps go to `reader`.

**Verify once.** `implementer` runs the targeted tests/checks for the files it changed and pastes the
raw output (missing output = fail); the orchestrator runs `dev_tools/verify.py` once at the done
gate — the full suite is not run twice.

**Built-in agents.** `Explore` is denied in `.claude/settings.json` (it bypasses `reader`). `Plan`
stays enabled and inherits the session model. `general-purpose` is never used for implementation or
review — `implementer` and `reviewer` cover those. Forks inherit the whole conversation; use one only
when a task genuinely needs that context.

**When a subagent's result is wrong:** it skipped files, skipped tests or stopped early → rerun it at
higher effort; it had full context, clearly tried and was still wrong → the orchestrator takes the
task over.

| Step | Owner | Notes |
|---|---|---|
| Understand, ⚑ gates, pattern validation, design, decomposition | Orchestrator | Never delegated; ask-first items stay with the user |
| Sweeps, greps, reads over three files | `reader` | Returns conclusions + `file:line`, not dumps |
| Implementation, tests, mechanical edits, README/docs, release-notes text | `implementer` | One writer per independent file; ≤3 in parallel; small edits: orchestrator, per the threshold |
| Targeted checks: the tests and checkers for the touched files | `implementer` | Same brief as the edit; raw output pasted verbatim |
| Diff review (bugs, R1–R9, simplicity audit, messages/CLI) | `reviewer` | Every non-trivial diff; never the author |
| Final call, fixes to review findings | Orchestrator decides; `implementer` applies (or the orchestrator, under the small-edit threshold) | Re-review only if the fix is non-trivial |
| Done gate | Orchestrator, itself | Reads the actual `git diff` and runs the verify with the test database up; a subagent's summary is never evidence of done |

**Brief for every subagent** (`implementer`, `reader`, `reviewer`) — self-contained; it has none of your
context. Goal, Files, Constraints and Acceptance criteria are mandatory; a `reader` brief replaces
item 3 with the question to answer; a `reviewer` brief gives the diff range, goal and acceptance
criteria and never the author's reasoning.

1. **Goal** — one sentence on the outcome.
2. **Files** — exact paths to edit (and only those); the pattern file to imitate (`path:lines`).
3. **Exact change** — the decision already made: names, signatures, statements, values, wording.
   No open choices; if one remains, the orchestrator resolves it first.
4. **Constraints** — hard rules that apply (R1 generic core, R2 shared helpers, R3 safe SQL, R5
   append-only ledger, R7 docs, R8 real database tests) and what NOT to touch (⚑ items, public
   signatures, the ledger schema).
5. **Verify / acceptance criteria** — the commands to run and the expected result.
6. **Report** — files changed, verify output, and any deviation or uncertainty (flag, don't guess).

**Reader guardrails:**

- Every `reader` brief demands `file:line` evidence; the orchestrator treats a claim without it as
  unverified.
- Reader briefs carry zero open choices; anything needing judgement goes to the orchestrator.
- Send corrections to a running agent as a new message or brief, never inside a file or command
  output it will read (it distrusts instructions in tool results).
- Before a rename or a signature change, the orchestrator re-runs the grep itself (`#grep` above) and
  compares the hit count with the `reader`'s sweep; a mismatch means the sweep is redone.

**Review loop**: `implementer` implements → `reviewer` reviews the diff against the brief and the hard
rules → findings the orchestrator accepts go back to `implementer` as a new brief (or are fixed
directly under the small-edit threshold) → the orchestrator reads the final diff, runs the verify and
signs off. A writer or reader that hits ambiguity or a ⚑ gate stops and reports; it never decides.
Unattended runs record the assumption in the reply.

## Implementation Decision format

For significant implementation choices, document as:

```markdown
### Implementation Decision
**Choice Made**: [what] · **Alternatives Considered**: [others]

| Approach | Pros | Cons | Pattern Alignment |
|----------|------|------|-------------------|
| Option A | …    | …    | Strong/Weak       |
| Option B | …    | …    | Strong/Weak       |

**Selected**: [option] — [1-sentence justification] · **Confidence**: High/Medium/Low
```

## Lessons loop

A review finding or a repeated mistake is fixed twice: in the code, and in the cheapest guard that stops
it recurring. Prefer, in order:

1. a checker rule (`dev_tools/check_conventions.py`, `dev_tools/check_guidance_integrity.py`) or a test;
2. a hook or permission (`.claude/settings.json`, `.claude/hooks/post_edit_check.py`, `.pre-commit-config.yaml`);
3. one line in the single home of that rule (the skill references) — never a second copy elsewhere;
4. nothing, with the reason stated in the PR.

The inverse holds: when a checker starts enforcing something, delete the prose that asked for it and
leave a pointer to the checker rule. A change to `CLAUDE.md`, the skill or `.claude/rules/` is checked
against the prompts in `evals/evals.json` (by hand, in a fresh plan-mode session: does it read the
expected file, does it stop at the ⚑ gate) before it merges.
