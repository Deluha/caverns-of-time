"""The schema fingerprint shared by the components: a canonical document of the catalogs and its hash.

Input: a cursor in an open transaction and the names of the schemas to read. Output: a `Fingerprint`, the
document keyed by schema, kind and object name, and the SHA-256 of its canonical JSON; the storage of
fingerprints in `caverns.fingerprint`; the comparison of two documents. Not public: Infinite re-exports
`Fingerprint` and `Difference`, and Nozdormu records fingerprints. Contract: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

from caverns_of_time._connection import Cursor, names, pin_definition_settings, pin_search_path
from caverns_of_time.errors import CavernsError, ChangesetError

__all__ = [
    "FINGERPRINT_EXISTS_SQL",
    "FINGERPRINT_SERVER_SQL",
    "Difference",
    "Fingerprint",
    "capture",
    "compare",
    "compared_schemas",
    "default_schemas",
    "fingerprint_current",
    "fingerprint_table_exists",
    "latest",
    "named_schemas",
    "read_document",
    "record",
    "resolve_schemas",
    "restrict",
    "watched_schemas",
]

# The kinds of object a document holds, in the order they are read.
# The last four were added after the first release; `compare` skips a kind the recorded document lacks.
_KINDS = ("table", "column", "index", "constraint", "view", "function", "trigger", "row_security", "policy", "enum")

# The schemas a fingerprint covers when the caller names none: every one but the system's and Caverns' own.
_DEFAULT_SCHEMAS_SQL = (
    "SELECT nspname::text FROM pg_namespace"
    " WHERE left(nspname, 3) <> 'pg_' AND nspname NOT IN ('information_schema', 'caverns') ORDER BY nspname"
)
# The fingerprint table is Caverns' own (`caverns.fingerprint`, the own step `fingerprint` of Nozdormu).
FINGERPRINT_EXISTS_SQL = "SELECT to_regclass('caverns.fingerprint') IS NOT NULL"
# True once the own step `fingerprint_server` added its column (false, never an error, without the table): the
# table can be read and written as the current release does.
FINGERPRINT_SERVER_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('caverns.fingerprint')"
    " AND attname = 'server_version' AND NOT attisdropped)"
)
# NOTE: the column lists below are `version` and then the fields of `Fingerprint`; a test pins them.
_LATEST_FINGERPRINT_SQL = (
    "SELECT version, schemas, document, hash, server_version FROM caverns.fingerprint ORDER BY version DESC LIMIT 1"
)
_RECORD_FINGERPRINT_SQL = (
    "INSERT INTO caverns.fingerprint (version, schemas, document, hash, server_version)"
    " VALUES (%s, %s, %s::jsonb, %s, %s)"
)
_SERVER_VERSION_SQL = "SELECT current_setting('server_version_num')::integer"
_EXISTING_SCHEMAS_SQL = "SELECT nspname::text FROM pg_namespace WHERE nspname = ANY(%s::text[])"
# The schemas of the fingerprint recorded with the latest `baseline` ledger row (see `watched_schemas`).
_BASELINE_SCHEMAS_SQL = (
    "SELECT f.schemas FROM caverns.fingerprint f JOIN caverns.ledger l ON l.version = f.version"
    " WHERE l.outcome = 'baseline' ORDER BY f.version DESC LIMIT 1"
)
# NOTE: hard rule 3 forbids composing a statement, so each kind's query spells its extension filter literally:
# an object an extension owns (a `pg_depend` row of type `e`) is left out, and the indexes and constraints of
# an extension's table go with the table.
_TABLES_SQL = """
SELECT n.nspname::text, c.relname::text, c.relkind::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'f') AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY n.nspname, c.relname
"""
# The generation expression and the default of a column are both a `pg_attrdef` row; `attgenerated` tells them apart.
_COLUMNS_SQL = """
SELECT n.nspname::text, c.relname::text, a.attname::text, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       CASE WHEN a.attgenerated = '' THEN pg_get_expr(d.adbin, d.adrelid) END,
       CASE WHEN a.attgenerated <> '' THEN pg_get_expr(d.adbin, d.adrelid) END,
       CASE a.attidentity WHEN 'a' THEN 'always' WHEN 'd' THEN 'by default' END,
       CASE WHEN a.attcollation <> t.typcollation
            THEN (SELECT o.collname::text FROM pg_collation o WHERE o.oid = a.attcollation) END
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE c.relkind IN ('r', 'p', 'f') AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = 'pg_class'::regclass
       AND e.objid = c.oid AND e.deptype = 'e')
ORDER BY n.nspname, c.relname, a.attnum
"""
_INDEXES_SQL = """
SELECT n.nspname::text, i.relname::text, pg_get_indexdef(x.indexrelid)
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_namespace n ON n.oid = i.relnamespace
WHERE n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = c.oid AND d.deptype = 'e')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = i.oid AND d.deptype = 'e')
ORDER BY n.nspname, i.relname
"""
# Table constraints only. PostgreSQL 18 also lists NOT NULL as a constraint (the column's `not_null` has it) and a
# constraint trigger as one (`pg_get_constraintdef` refuses it): both are left out.
_CONSTRAINTS_SQL = """
SELECT n.nspname::text, c.relname::text, k.conname::text, pg_get_constraintdef(k.oid)
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE k.conrelid <> 0 AND k.contype IN ('c', 'f', 'p', 'u', 'x') AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY n.nspname, c.relname, k.conname
"""
_VIEWS_SQL = """
SELECT n.nspname::text, c.relname::text, c.relkind::text, pg_get_viewdef(c.oid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v', 'm') AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY n.nspname, c.relname
"""
_FUNCTIONS_SQL = """
SELECT n.nspname::text, p.proname::text, pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'p') AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
       AND d.objid = p.oid AND d.deptype = 'e')
ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
"""
# User triggers on any relation (a constraint trigger included). `tgisinternal` leaves out the triggers PostgreSQL
# makes for a foreign key; a trigger an extension owns, or one on an extension's relation, goes too.
_TRIGGERS_SQL = """
SELECT n.nspname::text, c.relname::text, t.tgname::text, pg_get_triggerdef(t.oid),
       CASE t.tgenabled WHEN 'O' THEN 'origin' WHEN 'D' THEN 'disabled' WHEN 'R' THEN 'replica'
            WHEN 'A' THEN 'always' END
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = c.oid AND d.deptype = 'e')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_trigger'::regclass
       AND d.objid = t.oid AND d.deptype = 'e')
ORDER BY n.nspname, c.relname, t.tgname
"""
# Only a table with row-level security enabled or forced has an entry.
_ROW_SECURITY_SQL = """
SELECT n.nspname::text, c.relname::text, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND (c.relrowsecurity OR c.relforcerowsecurity) AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY n.nspname, c.relname
"""
# A role oid of 0 is PUBLIC; the names are sorted in Python, so the order does not depend on the collation.
_POLICIES_SQL = """
SELECT n.nspname::text, c.relname::text, p.polname::text,
       CASE p.polcmd WHEN '*' THEN 'all' WHEN 'r' THEN 'select' WHEN 'a' THEN 'insert' WHEN 'w' THEN 'update'
            WHEN 'd' THEN 'delete' END,
       p.polpermissive,
       ARRAY(SELECT CASE WHEN r.oid = 0 THEN 'public'
                         ELSE (SELECT o.rolname::text FROM pg_roles o WHERE o.oid = r.oid) END
             FROM unnest(p.polroles) AS r(oid)),
       pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
FROM pg_policy p
JOIN pg_class c ON c.oid = p.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
       AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY n.nspname, c.relname, p.polname
"""
_ENUMS_SQL = """
SELECT n.nspname::text, t.typname::text,
       ARRAY(SELECT e.enumlabel::text FROM pg_enum e WHERE e.enumtypid = t.oid ORDER BY e.enumsortorder)
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'e' AND n.nspname = ANY(%s::text[])
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass
       AND d.objid = t.oid AND d.deptype = 'e')
ORDER BY n.nspname, t.typname
"""


@dataclass(frozen=True)
class Fingerprint:
    """The schemas read (sorted), the document read from their catalogs and the SHA-256 hex of its canonical JSON.

    `server_version` is the server's `server_version_num` when it was read (170002 for 17.2): kept beside the
    hash, not in it. None only for a row recorded before Caverns kept it.
    """

    schemas: tuple[str, ...]
    document: dict[str, Any]
    hash: str
    server_version: int | None


@dataclass(frozen=True)
class Difference:
    """One difference between two documents: the kind of object, its `<schema>.<key>` name and what happened to it."""

    kind: str
    name: str
    change: str  # "added", "removed" or "changed"


def named_schemas(schemas: Collection[str] | None) -> list[str] | None:
    """The schema names a caller gave, sorted and de-duplicated; None when it gave none.

    Raises `ChangesetError` for a bare string, a name that is not a string or an empty collection.
    """
    if schemas is None:
        return None
    unique = names("schemas", schemas)
    for name in unique:
        if not isinstance(name, str):
            raise ChangesetError(f"schemas must hold names, not {name!r}")
    if not unique:
        raise ChangesetError("schemas is empty; name at least one schema or leave it out")
    return sorted(unique)


def default_schemas(cursor: Cursor) -> list[str]:
    """Every schema but `pg_*`, `information_schema` and `caverns`, sorted."""
    cursor.execute(_DEFAULT_SCHEMAS_SQL)
    return sorted(row[0] for row in cursor.fetchall())


def missing_schemas(cursor: Cursor, schemas: Sequence[str]) -> list[str]:
    """The named schemas that do not exist, sorted."""
    cursor.execute(_EXISTING_SCHEMAS_SQL, (list(schemas),))
    return sorted(set(schemas) - {row[0] for row in cursor.fetchall()})


def fingerprint_table_exists(cursor: Cursor) -> bool:
    """Whether `caverns.fingerprint` exists."""
    cursor.execute(FINGERPRINT_EXISTS_SQL)
    row = cursor.fetchone()
    return row is not None and bool(row[0])


def fingerprint_current(cursor: Cursor) -> bool:
    """Whether `caverns.fingerprint` exists with its `server_version` column (the own step `fingerprint_server`)."""
    cursor.execute(FINGERPRINT_SERVER_SQL)
    row = cursor.fetchone()
    return row is not None and bool(row[0])


def latest(cursor: Cursor) -> tuple[int, Fingerprint] | None:
    """The newest recorded fingerprint and the ledger version it was recorded at; None when there is none."""
    cursor.execute(_LATEST_FINGERPRINT_SQL)
    row = cursor.fetchone()
    if row is None:
        return None
    version, schemas, document, digest, server_version = row
    if isinstance(document, str):  # a driver that leaves jsonb as text
        document = json.loads(document)
    return version, Fingerprint(tuple(schemas), document, digest, server_version)


def record(cursor: Cursor, version: int, fingerprint: Fingerprint) -> None:
    """Store *fingerprint* for the ledger row *version*."""
    cursor.execute(
        _RECORD_FINGERPRINT_SQL,
        (
            version,
            list(fingerprint.schemas),
            json.dumps(fingerprint.document, sort_keys=True),
            fingerprint.hash,
            fingerprint.server_version,
        ),
    )


def watched_schemas(cursor: Cursor) -> list[str]:
    """The schemas of the fingerprint recorded with the latest `baseline` row, else the default schemas.

    The default is read live each time, so a schema created later is watched; it also serves before
    `caverns.fingerprint` exists.
    """
    if fingerprint_table_exists(cursor):
        cursor.execute(_BASELINE_SCHEMAS_SQL)
        row = cursor.fetchone()
        if row is not None:
            return sorted(row[0])
    return default_schemas(cursor)


def compared_schemas(cursor: Cursor, recorded: Fingerprint) -> list[str]:
    """The schemas to compare with *recorded*: the watched ones plus those *recorded* covers, sorted.

    A schema that was dropped is no longer watched, but *recorded* still holds it, so its objects read as removed.
    """
    return sorted(set(watched_schemas(cursor)) | set(recorded.schemas))


def resolve_schemas(
    cursor: Cursor,
    schemas: list[str] | None,
    refusal: type[CavernsError],
    default: Callable[[Cursor], list[str]],
) -> list[str]:
    """The schemas to fingerprint: the named ones, which must exist (else *refusal* is raised), else *default*'s."""
    if schemas is None:
        return default(cursor)
    missing = missing_schemas(cursor, schemas)
    if missing:
        raise refusal(f"the schema {missing[0]!r} does not exist")
    return schemas


def read_document(cursor: Cursor, schemas: Sequence[str]) -> dict[str, Any]:
    """The objects of *schemas* as a document keyed by schema, kind and object name, read from the system catalogs.

    Every schema appears with all its kinds, empty when it holds none. Pins `search_path` to `pg_catalog` for
    the transaction, because type, default and definition texts print names relative to it, and the settings
    that shape how a definition prints (`pin_definition_settings`), so the document, and its hash, do not
    depend on the session that read it.
    """
    pin_search_path(cursor)
    pin_definition_settings(cursor)
    document: dict[str, Any] = {schema: {kind: {} for kind in _KINDS} for schema in schemas}
    bound = (list(schemas),)

    def rows(sql: str) -> Sequence[Sequence[Any]]:
        cursor.execute(sql, bound)
        return cursor.fetchall()

    for schema, table, kind in rows(_TABLES_SQL):
        document[schema]["table"][table] = {"kind": kind}
    for schema, table, column, column_type, not_null, default, generated, identity, collation in rows(_COLUMNS_SQL):
        document[schema]["column"][f"{table}.{column}"] = {
            "type": column_type,
            "not_null": not_null,
            "default": default,
            "generated": generated,
            "identity": identity,
            "collation": collation,
        }
    for schema, index, definition in rows(_INDEXES_SQL):
        document[schema]["index"][index] = definition
    for schema, table, constraint, definition in rows(_CONSTRAINTS_SQL):
        document[schema]["constraint"][f"{table}.{constraint}"] = definition
    for schema, view, kind, definition in rows(_VIEWS_SQL):
        document[schema]["view"][view] = {"kind": kind, "definition": definition}
    for schema, function, arguments, definition in rows(_FUNCTIONS_SQL):
        document[schema]["function"][f"{function}({arguments})"] = definition
    for schema, table, trigger, definition, enabled in rows(_TRIGGERS_SQL):
        document[schema]["trigger"][f"{table}.{trigger}"] = {"definition": definition, "enabled": enabled}
    for schema, table, enabled, forced in rows(_ROW_SECURITY_SQL):
        document[schema]["row_security"][table] = {"enabled": enabled, "forced": forced}
    for schema, table, policy, command, permissive, roles, using, with_check in rows(_POLICIES_SQL):
        document[schema]["policy"][f"{table}.{policy}"] = {
            "command": command,
            "permissive": permissive,
            "roles": sorted(roles),
            "using": using,
            "with_check": with_check,
        }
    for schema, enum, labels in rows(_ENUMS_SQL):
        document[schema]["enum"][enum] = list(labels)
    return document


def hash_document(document: dict[str, Any]) -> str:
    """SHA-256 hex of the document's canonical JSON."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def restrict(document: dict[str, Any], schemas: Collection[str]) -> dict[str, Any]:
    """The part of *document* that covers *schemas*; a schema it lacks is left out, so it reads as empty."""
    return {schema: document[schema] for schema in schemas if schema in document}


def compare(recorded: dict[str, Any], live: dict[str, Any]) -> list[Difference]:
    """The differences between two documents, sorted by schema, kind (in `_KINDS` order) and key.

    An object only in *live* is `added`, only in *recorded* `removed`, in both with another value `changed`.
    For a schema *recorded* holds, only the kinds its entry holds are compared, so a fingerprint recorded by a
    release that knew fewer kinds reports no difference for the others; for a schema it lacks, every kind is
    compared (its objects read as added).
    """
    differences = []
    for schema in sorted(set(recorded) | set(live)):
        before, after = recorded.get(schema, {}), live.get(schema, {})
        kinds = [kind for kind in _KINDS if kind in before] if schema in recorded else _KINDS
        for kind in kinds:
            old, new = before.get(kind, {}), after.get(kind, {})
            for key in sorted(set(old) | set(new)):
                if key not in old:
                    change = "added"
                elif key not in new:
                    change = "removed"
                elif old[key] != new[key]:
                    change = "changed"
                else:
                    continue
                differences.append(Difference(kind, f"{schema}.{key}", change))
    return differences


def capture(cursor: Cursor, schemas: Sequence[str]) -> Fingerprint:
    """The fingerprint of *schemas* (sorted and de-duplicated), read through *cursor*'s transaction.

    The server's version is read in the same transaction as the document.
    """
    unique = tuple(sorted(set(schemas)))
    document = read_document(cursor, unique)
    cursor.execute(_SERVER_VERSION_SQL)
    row = cursor.fetchone()
    assert row is not None  # a SELECT without FROM returns its row
    return Fingerprint(unique, document, hash_document(document), row[0])
