# Workflow: Changing Caverns' Own Schema

For a change to an object Caverns itself owns inside a consumer's database: the ledger, the epoch
catalog, a history table. Rules: R5 and R6 (`references/conventions.md#r5`, `references/conventions.md#r6`).
Caverns' own changeset is `_OWN_CHANGESET` in `caverns_of_time/nozdormu.py` (its first step, `ledger`,
creates `caverns.ledger`); its database tests are in `tests/test_nozdormu_ledger.py`.

## Preconditions

- ⚑ The ledger schema, the checksum definition and the on-disk layout of epochs and history tables are
  ask-first: the owner confirms the exact DDL before code is written.
- The change is additive wherever possible. A consumer's existing ledger must stay readable after the
  upgrade; a change that cannot keep that promise stops here and goes to the owner.

## Steps

1. Grep every statement that touches the object (`references/recipes.md#grep`).
2. Express the change as a new step of Caverns' own changeset. Never edit a step that has shipped in a
   release: its checksum is recorded in consumers' ledgers.
3. Keep the step idempotent and give it a read-only check, like any consumer step.
4. Compose every statement per R3 (`references/conventions.md#r3`).
5. No step updates, deletes or truncates ledger rows (R5).

## Verification

Run `references/qa-and-ci.md#verify`, then:

- [ ] A database test installs the previous release's schema, applies the new step and reads old rows back
- [ ] A database test applies the full changeset to an empty database
- [ ] Applying twice changes nothing the second time

## Docs to update

- `caverns_of_time/README.md` (the object's contract)
- `RELEASE_NOTES.md` (an upgrade note when a consumer must act)
