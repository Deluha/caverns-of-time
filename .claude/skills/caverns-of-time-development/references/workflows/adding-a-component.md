# Workflow: Adding a Component or Module

For the first module of a component (`references/architecture.md#components`) or a new module inside one.
Rule: R9 (`references/conventions.md#r9`).

## Preconditions

- The component is in the current phase of `ROADMAP.md` and its sign-off items are confirmed and recorded
  there. A component from a later phase is not started early.
- ⚑ Its public surface (names, signatures, exceptions, CLI commands and exit codes) is ask-first.

## Steps

1. Grep for an existing helper before writing one (`references/recipes.md#grep`); shared helpers have one
   home (R2).
2. Create the module with a module docstring; state its public names explicitly.
3. Keep database access behind functions that take a connection; compose SQL per R3.
4. Library code logs, it does not print; only the `chromie` entry module parses a command line.
5. Write the tests with the code: pure logic as plain tests, anything touching the database through the
   `db_conn` fixture (R8, `tests/README.md`).
6. Name no consumer and assume no consumer's schema (R1).

## Verification

Run `references/qa-and-ci.md#verify` with the test instance up.

- [ ] Every public function has a test; every SQL statement runs in at least one database test
- [ ] `python dev_tools/check_conventions.py` is clean

## Docs to update

- `caverns_of_time/README.md` (files and public surface)
- `references/architecture.md` (the module map or the components table, when a row changes)
- `tests/README.md` (the inventory row)
- `RELEASE_NOTES.md` (the in-development entry)
