# Release Notes

What shipped, newest first. What is planned: `ROADMAP.md`.

## v0.1.0 — in development

### Phase 0 — Development process

No product code. The development process is ported from Project Blackwing and adapted to this
repository:

- `CLAUDE.md`: four operating principles, nine hard rules, the ask-first list.
- Agents (`implementer`, `reader`, `reviewer`, `scribe`), the session-start and post-edit hooks,
  `.claude/settings.json`, three path rules.
- The development skill: a routing table over six references and three workflows, ten guidance evals,
  and the command skills `verify`, `review-diff`, `db-change`, `guidance-change`.
- Package skeleton (`caverns_of_time`, version only), `dev_tools/` with the verify command and the
  conventions, guidance-integrity and docs-sync checkers, and their tests.
- A PostgreSQL 18 test instance for every environment: Docker locally and in cloud sessions, a service
  container in CI, a compose service in the dev container. The verify command and CI require it.
- Pre-commit hooks, gitleaks, and three CI workflows ending in one `CI gate` check; mypy is strict and
  gating.
