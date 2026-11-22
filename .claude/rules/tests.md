---
paths:
  - "tests/**"
---
Tests: read `tests/README.md` before editing. R8
(`.claude/skills/caverns-of-time-development/references/conventions.md#r8`): database behaviour is tested on
the local PostgreSQL 18 instance through the conftest fixtures; a test never opens its own connection and
never skips itself.
