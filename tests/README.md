# tests

## Purpose & scope

The only test location (hard rule 4). Two kinds of test: pure tests that need nothing, and database
tests that run against the local PostgreSQL 18 test instance (hard rule 8). There are no mocks of the
database.

## Files & entry points

| File | Covers | Database |
|---|---|---|
| `conftest.py` | The `db_dsn`, `db_conn`, `ledger_conn` and `second_conn` fixtures, the local-only guard, the `db` marker, the required-run check, and what several modules share: `RUNNER_GRANTS`, `wait_for_a_waiting_lock`, and `throwaway_roles` (cluster-wide roles named `caverns_test_<kind>_<suffix>`, only those created dropped, each drop independent) | — |
| `test_database.py` | The instance itself: major version 18, rollback of uncommitted work, the image tag and connection string agreeing across CI, the dev container and `dev_tools/database.py`; the fixtures' guards, `throwaway_roles` dropping its roles and leaking none when a create fails | yes |
| `test_nozdormu_changeset.py` | Nozdormu's pure parts: step and changeset validation, the checksum, the file and directory loaders, the status decision on synthetic ledger rows, the line-break and `None` argument refusals, the SQL constants of `nozdormu`, `_connection`, `_fingerprint` and `epochs` naming the own ledger, fingerprint and epoch tables in the own schema, the ledger column lists matching `LedgerEntry` and the fingerprint column lists matching `Fingerprint`, the own changeset's steps and every own step's checksum pinned, the lock timeout validation (from 1 ms to the integer limit; NaN, infinity, below 1 ms and an int too large for a float refused) and the SQLSTATE reader | no |
| `test_nozdormu_ledger.py` | Nozdormu against PostgreSQL, once per driver (psycopg 3, psycopg2): installation (all five own steps; the upgrade from a ledger with only the `ledger` step; the `privileges` step: `PUBLIC` holds nothing in `caverns`, its check, the upgrade from three steps, a restore that kept the grants; public grants made before the step revoked by `install`; the owner check with throwaway roles and `SET ROLE` (a stranger and a member without inherit refused, an inheriting member accepted) and a runner role with the documented grants; its recovery when the table exists without its row), the append-only and consistency rules of `caverns.fingerprint`, the `fingerprint_server` step (its check, an upgrade from four steps with an old fingerprint row read back with `server_version` None and a new apply recording it, `drift` refusing until then, the column recorded by `apply` and `baseline` and outside the hash, a restore that kept the column), the fingerprint recorded by apply (one per run, at the last row; with a failed row; none for a run that writes nothing, a failure that cannot be recorded or `install`) and by baseline, the schemas a baseline sets (apply after a baseline with `schemas` records only those), the pre-apply drift warning, the refusal of a connection with uncommitted work, a failed transaction or a lost connection, the transaction left idle on every error path, the ledger's constraints and its append-only triggers, preflight's five statuses (`before_baseline` included) and refusals, apply (skip, refusal, `reapply`, `accept`, failure and the error a `failed` row keeps (SQLSTATE and primary message only), a failure that cannot be recorded (a role that can read the ledger but not append to it), a runner that applied a step after preflight, two connections contending for the advisory lock, the lock timeout: the first step's wait for Caverns' lock given up with a `CavernsError`, a later step's and the failure record's wait for it unbounded (a releasing thread frees the lock only after a waiter was seen and 0.5 s passed), a step that waits for a table lock failing with `55P03` also as a later step, the setting each step sees and leaves behind), baseline (a row per step, `before_baseline` steps refused, reapplied and accepted) and its fingerprint (independent of `search_path`, following tables, columns and indexes), a check that is one statement, `history` (order, changeset filter, limit, refusals including a limit above the bigint maximum, writes nothing) | yes |
| `test_infinite.py` | Infinite against PostgreSQL, once per driver: the document of each of the six kinds (tables of kinds `r`, `p` and `f`; generated, identity and collated columns; indexes; constraints; views and materialized views; functions and procedures) and that changing each changes the hash, extension-owned objects left out, independence of `search_path` and of the session's printing settings (a view and column defaults printing a date, timestamptz, interval, float, bytea and a string with a backslash, under changed `DateStyle`, `TimeZone`, `IntervalStyle`, `extra_float_digits`, `bytea_output`, `quote_all_identifiers` and `standard_conforming_strings`; the session left as it was), an empty schema, the default and watched schemas (the latest baseline's, else the live default; a later schema is watched; a baseline without `schemas` takes the default; without a baseline a dropped schema is still compared, in `drift`, in `drift(schemas=...)` and in the pre-apply warning), the refusals, a read that writes nothing and leaves no open transaction, and the hash's canonicalisation; `compare` (pure); `drift` (no fingerprint, not installed, clean, added, removed, changed, a dropped schema, `schemas` narrowing and the unwatched refusal, disagreements and unknown steps, checks run with the session's `search_path`, nothing written) | yes |
| `test_epochs.py` | Epochs against PostgreSQL, once per driver: the own step `epochs` (the five tables, installing twice, the upgrade of a database installed with two steps and the refusals until then), the append-only triggers of `epoch`, `epoch_source` and `epoch_deletion`, the tombstone rule of `epoch_row`, the tables' constraints and the deferred foreign key; `register` (primary key by default without its `INCLUDE` columns, composite and explicit keys in order, the same key again a no-op, another key a warning, refusals for a table without a primary key, a missing table, a view and a key column that is not live, mixed-case names), `unregister`, `tracked` ordering, bad arguments; `snapshot` (the copy and its counts, empty and composite-key tables, user-type names, hostile identifiers, every refusal with nothing written, nulls checked before duplicates, a partitioned table, values stored under pinned output settings, the session lock held, released and its failed paths (a release that errors or finds the lock not held is a `CavernsError`), the writable check (every write refuses a read-only session and writes nothing, the reads still work; the standby branch shares its code path and has no replica to test), the wait for another writer's lock, the lock timeout on Caverns' lock and on a source table, the setting seen in the copy, row-level security (refused for a role that neither bypasses nor owns the table, or owns it with `FORCE`; accepted for the owner without `FORCE`, `BYPASSRLS` and a superuser, with throwaway roles and `SET ROLE`), `max_bytes` (refused above, `None`, partitions counted, bad values, the INFO estimate) and a key too large for an index entry) `catalog` (order, limit, tables, invalid limits), `delete` (the tombstone and only that epoch's rows removed, several names, all or nothing, bad names, the trigger afterwards, `dry_run`, the wait for the lock) and `as_of` (the epoch's own columns, key order for an integer and a composite key, `limit`, an empty table, every kind of value, the refusals of an unknown or deleted epoch and a missing table, bad arguments) and `diff` (rows added, removed and changed and their key order, column added, removed and of another type, a table in one epoch only, a key that differs in name or type and `tables=` narrowing, every refusal); every function's refusal before install, with autocommit on or with uncommitted work | yes |
| `test_chromie.py` | Chromie and the console helpers (including `confirm`): connection-string precedence, the exit-2 errors (no command, no connection string, a bad `--limit`, a `--limit` above the bigint maximum refused by the library, a refused connection, a missing driver, an unexpected error), `log`, `status`, `drift`, `as-of`, `diff`, `epochs` and `delete-epoch` (confirmation, `--yes`, `--dry-run`; text, JSON, `--schema`, `--changeset`, exit codes 0, 1 and 2) against an installed ledger, table alignment (also without a header), the stdout / stderr split and icons, JSON output | yes |
| `test_check_conventions.py` | Convention rules C1–C9 on synthetic input, then the real repository | no |
| `test_check_docs_sync.py` | The docs-sync classifier, token rules, git parsing | no |
| `test_check_guidance_integrity.py` | Each guidance check on a miniature tree, then the real repository | no |
| `test_post_edit_check.py` | The post-edit hook: which file it checks, decision order, exit codes | no |
| `test_verify.py` | The verify step list and the database requirement it exports | no |

## How it works

- `db_conn` yields a connection to the test instance and rolls its uncommitted work back when the test
  ends. Work a test commits is not undone: a test of committing code takes `ledger_conn` instead.
- `ledger_conn` yields a non-autocommit connection and runs the test twice, once on psycopg 3 and once on
  psycopg2 (Caverns must work for both). Nozdormu commits, so before and after each test the fixture drops
  the `caverns` schema (the ledger) and `nozdormu_test` (the schema test changesets build their objects in).
  A test using it ends its own reads with a commit or rollback, since Caverns wants a connection with no open work.
- `second_conn` yields a second non-autocommit connection on the driver of `ledger_conn` (torn down first);
  with it a test holds the advisory lock on one connection while a thread applies on the other.
- Every test that takes `db_dsn`, `db_conn`, `ledger_conn` or `second_conn` is marked `db` automatically.
- The connection string comes from `CAVERNS_TEST_DSN`. Every `host` and `hostaddr` entry, as libpq reads
  them, must be `localhost`, `127.0.0.1` or `::1`; a host list, a `host=` parameter, a service entry or
  a missing host fails the test, and so does a set `PGHOSTADDR`, `PGSERVICE` or `PGSERVICEFILE`.
- With `CAVERNS_REQUIRE_DB=1` a run in which no database test passed fails as a whole, so deselecting
  the database tests cannot produce a green run.

| How pytest is run | `CAVERNS_TEST_DSN` unset | Instance unreachable |
|---|---|---|
| `pytest` | database tests skip with a notice | database tests fail |
| `python dev_tools/verify.py` | falls back to the local default | database tests fail |
| CI (`CAVERNS_REQUIRE_DB=1`) | database tests fail | database tests fail |
| pre-push hook (`-m "not db"`, `CAVERNS_REQUIRE_DB` unset) | database tests not selected | database tests not selected |

## Interfaces & contracts

- Start the instance: `python dev_tools/database.py start`; its connection string:
  `python dev_tools/database.py dsn`.
- A test never opens its own connection, never names a connection string and never skips itself
  (checker rules C6 and C9). The two tests whose subject is a connection string may spell one:
  `test_database.py` (the guard) and `test_check_conventions.py` (the checker).
- With `CAVERNS_REQUIRE_DB=1` exported, a targeted run that selects no database test exits 1 by design.
- A changed behaviour means updating its test deliberately in the same PR, never loosening an assertion.

## Extending

Write a database test by taking `db_conn`. Create what the test needs inside the test; the rollback
removes whatever was not committed. A test of code that commits takes `ledger_conn`. Add the file to the table above.

## See also

`dev_tools/README.md` · `.claude/skills/caverns-of-time-development/references/qa-and-ci.md`
