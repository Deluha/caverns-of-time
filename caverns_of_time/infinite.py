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
    fingerprint_table_exists,
    latest,
    named_schemas,
    read_document,
    resolve_schemas,
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

    @property
    def found(self) -> bool:
        """Whether there is any difference or disagreement."""
        return bool(self.differences or self.disagreements)


def fingerprint(connection: Connection, *, schemas: Collection[str] | None = None) -> Fingerprint:
    """The fingerprint of *schemas* (default: the schemas of the latest recorded fingerprint, else every schema
    but `pg_*`, `information_schema` and `caverns`).

    Reads the tables, columns, indexes, constraints, views and functions in one read-only transaction and
    writes nothing. Raises `ChangesetError` for a bare string, a non-string name or an empty collection for
    *schemas*, and for a named schema that does not exist.
    """
    check_connection(connection)
    named = named_schemas(schemas)
    with read_only(connection) as cursor:
        return capture(cursor, resolve_schemas(cursor, named, ChangesetError))


def drift(
    connection: Connection, changeset: Changeset | None = None, *, schemas: Collection[str] | None = None
) -> Drift:
    """Compare the live schemas with the latest recorded fingerprint, in one read-only transaction; writes nothing.

    *schemas* narrows the report to those of the watched schemas. With a *changeset*, a step recorded as
    applied whose check is now false is a disagreement. Raises `LedgerNotInstalled` before `install()`,
    `NoFingerprint` when none is recorded yet, `ChangesetError` for an unwatched schema or bad *schemas*,
    and the errors of `preflight` for a *changeset*.
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
        unwatched = [name for name in named or () if name not in recorded.schemas]
        if unwatched:
            raise ChangesetError(f"the schema {unwatched[0]!r} is not watched (watched: {', '.join(recorded.schemas)})")
        narrowed = tuple(named) if named is not None else recorded.schemas
        # The checks run before the document is read: reading pins `search_path` to `pg_catalog` for the transaction.
        disagreements = tuple(
            status
            for status in statuses
            if status.status is Status.APPLIED
            and status.step.check is not None
            and not run_check(cursor, status.step.label, status.step.check)
        )
        live = read_document(cursor, narrowed)
    differences = compare({schema: recorded.document[schema] for schema in narrowed}, live)
    return Drift(version, narrowed, tuple(differences), disagreements)
