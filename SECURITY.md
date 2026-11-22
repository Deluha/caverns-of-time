# Security Policy

## Reporting a vulnerability

Report vulnerabilities **privately** — never in a public issue or PR.

- Preferred: open a private report via GitHub Security Advisories
  (*Security → Report a vulnerability* on the repository).
- Alternatively: contact the repository owner directly through a private channel.

Include the affected file(s), reproduction steps, and impact. You will receive an acknowledgement, and
a fix or mitigation will be coordinated before any public disclosure.

## Supported versions

Nothing is released yet. From the first release on, the current minor line (`pyproject.toml`) is
supported.

## Credentials model

This repository holds no credential and its workflows use no secret.

- **Test database** — the tests run against a throwaway PostgreSQL instance on the local machine
  (`dev_tools/database.py`, the CI service container, the dev container). Its password is written in the
  repository on purpose: the instance holds no data and listens on localhost only.
- **Consumer databases** — no test, workflow or development session connects to a consumer's database.
  The test fixtures refuse a connection string whose host is not the local machine
  (`tests/conftest.py`). A consumer supplies its own connection at run time; Caverns never stores one.
- **Enforcement** — convention rule C8 (`dev_tools/check_conventions.py`) fails pre-commit and CI if a
  secret file or build artifact is tracked.
- **Secret scan** — gitleaks (default ruleset, `.gitleaks.toml`) scans the staged changes at commit
  time, and in `ci-pre-commit` the PR's commits and the tree
  (`.claude/skills/caverns-of-time-development/references/checkers.md#checker-secret-scan`). A real
  finding is fixed and the credential rotated, never allow-listed.

## SQL safety

Caverns runs SQL built from names its caller supplies. Identifiers are composed with the driver's
quoting and values are bound as parameters, never formatted into a statement (hard rule 3). A report
of a statement that breaks this rule is a vulnerability report.

## Workflow supply chain

Third-party actions used by the workflows are pinned to a full commit SHA (not a mutable tag) and
reviewed on update; Dependabot's `github-actions` ecosystem proposes those bumps. The gitleaks binary
CI downloads is pinned by version and sha256.
