"""Infinite, the schema fingerprint: the public face of the fingerprint of a database's schemas.

Input: a DB-API 2.0 connection (autocommit off). Output: a `Fingerprint` read from the system catalogs; nothing
is written. The module imports no database driver. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

from collections.abc import Collection

from caverns_of_time._connection import Connection, check_connection, read_only
from caverns_of_time._fingerprint import Fingerprint, capture, named_schemas, resolve_schemas
from caverns_of_time.errors import ChangesetError

__all__ = ["Fingerprint", "fingerprint"]


def fingerprint(connection: Connection, *, schemas: Collection[str] | None = None) -> Fingerprint:
    """The fingerprint of *schemas* (default: every schema but `pg_*`, `information_schema` and `caverns`).

    Reads the tables, columns, indexes, constraints, views and functions in one read-only transaction and
    writes nothing. Raises `ChangesetError` for a bare string, a non-string name or an empty collection for
    *schemas*, and for a named schema that does not exist.
    """
    check_connection(connection)
    named = named_schemas(schemas)
    with read_only(connection) as cursor:
        return capture(cursor, resolve_schemas(cursor, named, ChangesetError))
