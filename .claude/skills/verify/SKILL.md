---
name: verify
description: Run the repository verify script and report its summary table.
disable-model-invocation: true
---
Run `python dev_tools/verify.py`, then report the summary table.
On a failure, show the failing step's output and propose the fix. Do not loosen a test, skip a check or
run without the test database.

Reference: `.claude/skills/caverns-of-time-development/references/qa-and-ci.md#verify`.
