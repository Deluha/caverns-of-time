# tests

## Purpose & scope

The only test location (hard rule 4). Two kinds of test: pure tests that need nothing, and database
tests that run against the local PostgreSQL 18 test instance (hard rule 8). There are no mocks of the
database.

## Files & entry points

| File | Covers | Database |
|---|---|---|
| `conftest.py` | The `db_dsn`, `db_conn`, `ledger_conn` and `second_conn` fixtures, the local-only guard, the `db` marker, the required-run check | — |
| `test_database.py` | The instance itself: major version 18, rollback of uncommitted work, the image tag and connection string agreeing across CI, the dev container and `dev_tools/database.py`; the fixtures' guards | yes |
| `test_nozdormu_changeset.py` | Nozdormu's pure parts: step and changeset validation, the checksum, the file and directory loaders, the status decision on synthetic ledger rows, the line-break and `None` argument refusals, the SQL constants naming one ledger table | no |
| `test_nozdormu_ledger.py` | Nozdormu against PostgreSQL, once per driver (psycopg 3, psycopg2): installation (and its recovery when the table exists without its row), the refusal of a connection with uncommitted work, a failed transaction or a lost connection, the transaction left idle on every error path, the ledger's constraints and its append-only triggers, preflight's four statuses and refusals, apply (skip, refusal, `reapply`, `accept`, failure, a failure that cannot be recorded, a runner that applied a step after preflight, two connections contending for the advisory lock), baseline and its fingerprint (independent of `search_path`), a check that is one statement | yes |
| `test_chromie.py` | Chromie and the console helpers: connection-string precedence, the usage error without a command, table alignment, the stdout / stderr split and icons, JSON output | no |
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
