"""Infinite, the schema fingerprint: the fingerprint of a database's schemas and drift from the recorded one.

Input: a DB-API 2.0 connection (autocommit off), optionally a `Changeset`. Output: a `Fingerprint` read from the
system catalogs, or a `Drift` comparing the live schemas with the latest recorded fingerprint; nothing is written.
The module imports no database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from caverns_of_time import nozdormu
from caverns_of_time._connection import Connection, check_connection, read_only, run_check
from caverns_of_time._fingerprint import (
    Difference,
    Fingerprint,
    capture,
    compare,
    compared_schemas,
    fingerprint_table_exists,
    latest,
    named_schemas,
    read_document,
    resolve_schemas,
    restrict,
    watched_schemas,
)
from caverns_of_time.errors import ChangesetError, LedgerNotInstalled, NoFingerprint
from caverns_of_time.nozdormu import Changeset, Status, StepStatus

__all__ = ["Difference", "Drift", "Fingerprint", "drift", "fingerprint"]


@dataclass(frozen=True)
class Drift:
    """What drift found: the fingerprint compared with, the schemas compared, the differences and the disagreements.

    A disagreement is a step the ledger records as applied whose check is now false.
    """

    fingerprint_version: int
    schemas: tuple[str, ...]
    differences: tuple[Difference, ...]
    disagreements: tuple[StepStatus, ...]


def fingerprint(connection: Connection, *, schemas: Collection[str] | None = None) -> Fingerprint:
    """The fingerprint of *schemas* (default: the watched schemas: those of the latest baseline's fingerprint, else
    every schema but `pg_*`, `information_schema` and `caverns`, read now).

    Reads the tables, columns, indexes, constraints, views and functions in one read-only transaction and
    writes nothing. Raises `ChangesetError` for a bare string, a non-string name or an empty collection for
    *schemas*, and for a named schema that does not exist.
    """
    check_connection(connection)
    named = named_schemas(schemas)
    with read_only(connection) as cursor:
        return capture(cursor, resolve_schemas(cursor, named, ChangesetError, watched_schemas))


def drift(
    connection: Connection, changeset: Changeset | None = None, *, schemas: Collection[str] | None = None
) -> Drift:
    """Compare the live database with the latest recorded fingerprint; writes nothing.

    The schemas compared are the watched schemas plus those the fingerprint covers: a watched schema the
    fingerprint lacks shows its objects as added, a covered schema since dropped shows them as removed, with or
    without a baseline. With a *changeset*, `preflight` runs first in a read-only transaction of its own; then one
    read-only transaction reads the fingerprint, runs the checks and reads the catalogs. *schemas* narrows the
    report to some of the compared schemas (`Drift.schemas` holds the narrowed set). With a
    *changeset*, a step recorded as applied whose check is now false is a disagreement. Raises
    `LedgerNotInstalled` before `install()`, `NoFingerprint` when none is recorded yet, `ChangesetError` for an
    unwatched schema or bad *schemas*, and the errors of `preflight` for a *changeset*.
    """
    check_connection(connection)
    named = named_schemas(schemas)
    statuses = nozdormu.preflight(connection, changeset) if changeset is not None else []
    with read_only(connection) as cursor:
        if not fingerprint_table_exists(cursor):
            raise LedgerNotInstalled(
                "the fingerprint table is not installed (caverns.fingerprint does not exist): call install()"
            )
        found = latest(cursor)
        if found is None:
            raise NoFingerprint("no fingerprint is recorded yet: apply a changeset or take a baseline first")
        version, recorded = found
        watched = compared_schemas(cursor, recorded)
        unwatched = [name for name in named or () if name not in watched]
        if unwatched:
            raise ChangesetError(f"the schema {unwatched[0]!r} is not watched (watched: {', '.join(watched)})")
        compared = tuple(named if named is not None else watched)
        # The checks run before the document is read: reading pins `search_path` to `pg_catalog` for the transaction.
        disagreements = tuple(
            status
            for status in statuses
            if status.status is Status.APPLIED
            and status.step.check is not None
            and not run_check(cursor, status.step.label, status.step.check)
        )
        live = read_document(cursor, compared)
    differences = compare(restrict(recorded.document, compared), live)
    return Drift(version, compared, tuple(differences), disagreements)
