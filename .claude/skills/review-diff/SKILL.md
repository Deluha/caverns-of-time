---
name: review-diff
description: Review a diff with the reviewer agent and relay only verified findings.
disable-model-invocation: true
argument-hint: "[diff range, default origin/main...HEAD plus the working tree]"
---
Launch the `reviewer` agent (`.claude/agents/reviewer.md`) on the diff range given in $ARGUMENTS; with no argument, review `origin/main...HEAD` plus the working tree.
Brief it with the range, the goal of the change and the acceptance criteria, never the author's reasoning
(`.claude/skills/caverns-of-time-development/references/recipes.md#orchestration`).
Verify each finding against the code before relaying it, most severe first.
