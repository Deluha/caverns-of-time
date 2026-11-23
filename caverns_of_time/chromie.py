"""Chromie, the command line: argument parsing, connection configuration and command dispatch.

Input: the command line and the `CAVERNS_DSN` environment variable. Output: console lines and an exit
code (0 success, 1 a finding, 2 a usage or environment error). This module holds command flow only; the
work lives in the component modules. Contract: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC
from pathlib import Path
from typing import Protocol

from caverns_of_time import console, epochs, infinite, nozdormu
from caverns_of_time.errors import CavernsError
from caverns_of_time.nozdormu import Connection

__all__ = ["main"]

ENV_DSN = "CAVERNS_DSN"
EXIT_OK = 0
EXIT_FINDING = 1
EXIT_USAGE = 2
DEFAULT_LOG_LIMIT = 20
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Shared by every subcommand: each passes `parents=[_common]`.
_common = argparse.ArgumentParser(add_help=False)
_common.add_argument("--dsn", help=f"connection string (default: the {ENV_DSN} environment variable)")
_common.add_argument("--format", choices=("text", "json"), default="text", help="output format")


class _ClosableConnection(Connection, Protocol):
    """A connection the CLI opened itself, so it also closes it."""

    def close(self) -> object: ...


class UsageError(Exception):
    """A usage or environment problem; the CLI reports it and exits with code 2."""


def _non_negative_int(text: str) -> int:
    """An argparse type: an integer of 0 or more."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is negative")
    return value


def _table_name(text: str) -> tuple[str, str]:
    """An argparse type: SCHEMA.TABLE split at the first dot, both parts not empty."""
    schema, _, table = text.partition(".")
    if not schema or not table:
        raise argparse.ArgumentTypeError(f"expected SCHEMA.TABLE, got {text!r}")
    return schema, table


def build_parser() -> argparse.ArgumentParser:
    """Build the `chromie` parser; each subcommand sets `handler` with `set_defaults`."""
    parser = argparse.ArgumentParser(
        prog="chromie", description="the timeline of a PostgreSQL database: its ledger and drift"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    log = commands.add_parser("log", parents=[_common], help="show the ledger, newest first")
    log.add_argument("--changeset", help="only this changeset's rows")
    log.add_argument(
        "--limit",
        type=_non_negative_int,
        default=DEFAULT_LOG_LIMIT,
        help=f"the most rows to show (0: all; default: {DEFAULT_LOG_LIMIT})",
    )
    log.set_defaults(handler=_log)
    status = commands.add_parser(
        "status", parents=[_common], help="show the status of each step of a changeset directory"
    )
    status.add_argument("directory", metavar="DIRECTORY", help="the directory of the changeset's .sql files")
    status.add_argument("--changeset", help="the changeset name (default: the directory's name)")
    status.set_defaults(handler=_status)
    drift = commands.add_parser(
        "drift", parents=[_common], help="compare the live schema with the last recorded fingerprint"
    )
    drift.add_argument(
        "directory", nargs="?", metavar="DIRECTORY", help="the changeset directory whose applied steps' checks to run"
    )
    drift.add_argument("--changeset", help="the changeset name (default: the directory's name)")
    drift.add_argument("--schema", action="append", help="narrow the report to this schema (repeatable)")
    drift.set_defaults(handler=_drift)
    as_of = commands.add_parser("as-of", parents=[_common], help="show a table as it was at an epoch")
    as_of.add_argument("epoch", metavar="EPOCH", help="the epoch name")
    as_of.add_argument("table", metavar="SCHEMA.TABLE", type=_table_name, help="the table, schema-qualified")
    as_of.add_argument(
        "--limit",
        type=_non_negative_int,
        default=DEFAULT_LOG_LIMIT,
        help=f"the most rows to show (0: all; default: {DEFAULT_LOG_LIMIT})",
    )
    as_of.set_defaults(handler=_as_of)
    diff = commands.add_parser("diff", parents=[_common], help="compare two epochs row by row")
    diff.add_argument("old", metavar="OLD", help="the earlier epoch name")
    diff.add_argument("new", metavar="NEW", help="the later epoch name")
    diff.add_argument("--table", action="append", type=_table_name, help="compare only this table (repeatable)")
    diff.set_defaults(handler=_diff)
    return parser


def resolve_dsn(argument: str | None, environ: Mapping[str, str]) -> str | None:
    """Return the connection string: the argument, else `CAVERNS_DSN`, else None."""
    return argument or environ.get(ENV_DSN) or None


def connect(dsn: str) -> _ClosableConnection:
    """Open a psycopg 3 connection (autocommit off); the message never contains the connection string."""
    try:
        import psycopg
    except ImportError as exc:
        raise UsageError("chromie needs the cli extra: pip install 'caverns-of-time[cli]'") from exc
    try:
        return psycopg.connect(dsn)
    except psycopg.Error as exc:
        raise UsageError(f"cannot connect: {exc}") from None


def _log(args: argparse.Namespace, connection: Connection) -> int:
    """`chromie log`: the ledger rows, newest first."""
    entries = nozdormu.history(connection, changeset=args.changeset, limit=args.limit or None)
    if args.format == "json":
        console.emit_json([{**asdict(entry), "recorded_at": entry.recorded_at.isoformat()} for entry in entries])
    elif not entries:
        scope = f"changeset {args.changeset} has no ledger rows" if args.changeset else "the ledger is empty"
        console.info(f"Nothing to show — {scope}.")
    else:
        rows = [
            (
                e.version,
                e.changeset,
                e.label,
                e.outcome,
                e.recorded_at.astimezone(UTC).strftime(_TIME_FORMAT),
                e.recorded_by,
            )
            for e in entries
        ]
        console.table(["VERSION", "CHANGESET", "LABEL", "OUTCOME", "RECORDED_AT (UTC)", "BY"], rows, right=[0])
    return EXIT_OK


def _directory_changeset(directory: str, name: str | None) -> nozdormu.Changeset:
    """The changeset of *directory*, named *name* or after the directory."""
    return nozdormu.Changeset.from_directory(name or Path(directory).resolve().name, directory)


def _status(args: argparse.Namespace, connection: Connection) -> int:
    """`chromie status`: each step of a changeset directory against the ledger; 1 when a step is not applied."""
    changeset = _directory_changeset(args.directory, args.changeset)
    name = changeset.name
    statuses = nozdormu.preflight(connection, changeset)
    versions = [s.entry.version if s.entry else None for s in statuses]
    open_steps = [s for s in statuses if s.status is not nozdormu.Status.APPLIED]
    code = EXIT_FINDING if open_steps else EXIT_OK
    if args.format == "json":
        steps = [
            {"label": s.step.label, "status": s.status.value, "version": version}
            for s, version in zip(statuses, versions, strict=True)
        ]
        console.emit_json({"changeset": name, "steps": steps})
        return code
    console.table(
        ["LABEL", "STATUS", "VERSION"],
        [(s.step.label, s.status.value, version) for s, version in zip(statuses, versions, strict=True)],
    )
    if open_steps:
        listed = ", ".join(f"{s.step.label} ({s.status.value})" for s in open_steps)
        console.error(f"{len(open_steps)} of {len(statuses)} steps of changeset {name} not applied: {listed}.")
    else:
        console.success(f"All {len(statuses)} steps of changeset {name} applied.")
    return code


_CHANGE_SYMBOLS = {"added": "+", "removed": "-", "changed": "~"}


def _drift(args: argparse.Namespace, connection: Connection) -> int:
    """`chromie drift`: the live schema against the last recorded fingerprint; 1 when anything differs."""
    if args.directory is None and args.changeset:
        raise UsageError("--changeset needs a DIRECTORY")
    changeset = _directory_changeset(args.directory, args.changeset) if args.directory else None
    result = infinite.drift(connection, changeset, schemas=args.schema)
    disagreements = [(s.entry.changeset, s.step.label, s.entry.version) for s in result.disagreements if s.entry]
    found = bool(result.differences or result.disagreements)
    code = EXIT_FINDING if found else EXIT_OK
    if args.format == "json":
        console.emit_json(
            {
                "fingerprint_version": result.fingerprint_version,
                "schemas": list(result.schemas),
                "differences": [asdict(d) for d in result.differences],
                "disagreements": [
                    {"changeset": name, "label": label, "version": version} for name, label, version in disagreements
                ],
            }
        )
        return code
    version = result.fingerprint_version
    console.info(f"Comparing with the fingerprint at ledger version {version} (schemas: {', '.join(result.schemas)}).")
    rows = [(_CHANGE_SYMBOLS[d.change], d.kind, d.name) for d in result.differences]
    rows += [("~", "step", f"{name}/{label}: recorded applied, check now false") for name, label, _ in disagreements]
    console.table(None, rows)
    if found:
        count = len(rows)
        console.error(f"Drift: {count} difference{'s' if count != 1 else ''}.")
    else:
        console.success(f"No drift — the live schema matches the fingerprint at ledger version {version}.")
    return code


def _cell(value: object) -> str | None:
    """A value of a snapshotted row as a table cell: None stays None, a string as is, else its compact JSON."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _as_of(args: argparse.Namespace, connection: Connection) -> int:
    """`chromie as-of`: a table's rows as they were at an epoch."""
    schema, table = args.table
    at = epochs.as_of(connection, args.epoch, schema, table, limit=args.limit or None)
    taken_at, columns, row_count = at.epoch.taken_at, at.table.columns, at.table.row_count
    if args.format == "json":
        console.emit_json(
            {
                "epoch": at.epoch.name,
                "ledger_version": at.epoch.ledger_version,
                "taken_at": taken_at.isoformat(),
                "schema": schema,
                "table": table,
                "key": list(at.table.key),
                "columns": [asdict(column) for column in columns],
                "row_count": row_count,
                "rows": list(at.rows),
            }
        )
        return EXIT_OK
    taken = taken_at.astimezone(UTC).strftime(_TIME_FORMAT)
    counted = f"{row_count} row" if row_count == 1 else f"{row_count} rows"
    console.info(
        f"{schema}.{table} at epoch {at.epoch.name} (ledger version {at.epoch.ledger_version}, taken {taken} UTC):"
        f" {counted}, key ({', '.join(at.table.key)})."
    )
    if at.rows:
        console.table([c.name for c in columns], [[_cell(row[c.name]) for c in columns] for row in at.rows])
    if len(at.rows) < row_count:
        console.info(f"Showing {len(at.rows)} of {row_count} rows — --limit 0 shows all.")
    return EXIT_OK


def _key_text(key: Mapping[str, object]) -> str:
    """A row's key as `id=42, code="x"`: each value as compact JSON."""
    return ", ".join(f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in key.items())


def _diff_lines(result: epochs.EpochDiff) -> list[tuple[str, str, str]]:
    """The (symbol, kind, name) line of each difference, table by table: the table, its columns, then its rows."""
    lines: list[tuple[str, str, str]] = []
    for t in result.tables:
        name = f"{t.schema}.{t.table}"
        if t.change != "changed":
            lines.append((_CHANGE_SYMBOLS[t.change], "table", name))
        for column in t.columns:
            detail = f": {column.old_type} → {column.new_type}" if column.change == "changed" else ""
            lines.append((_CHANGE_SYMBOLS[column.change], "column", f"{name}.{column.name}{detail}"))
        lines += [("+", "row", f"{name} ({_key_text(key)})") for key in t.added]
        lines += [("-", "row", f"{name} ({_key_text(key)})") for key in t.removed]
        lines += [("~", "row", f"{name} ({_key_text(row.key)}): {', '.join(row.old)}") for row in t.changed]
    return lines


def _diff(args: argparse.Namespace, connection: Connection) -> int:
    """`chromie diff`: two epochs row by row; 1 when anything differs."""
    result = epochs.diff(connection, args.old, args.new, tables=args.table)
    code = EXIT_FINDING if result.tables else EXIT_OK
    if args.format == "json":
        console.emit_json(
            {
                "old": {"epoch": result.old.name, "ledger_version": result.old.ledger_version},
                "new": {"epoch": result.new.name, "ledger_version": result.new.ledger_version},
                "tables": [asdict(t) for t in result.tables],
            }
        )
        return code
    old, new = result.old, result.new
    console.info(
        f"Comparing epoch {old.name} (ledger version {old.ledger_version})"
        f" with {new.name} (ledger version {new.ledger_version})."
    )
    lines = _diff_lines(result)
    console.table(None, lines)
    if result.tables:
        count, tables = len(lines), len(result.tables)
        console.error(f"{count} difference{'s' if count != 1 else ''} in {tables} table{'s' if tables != 1 else ''}.")
    else:
        console.success(f"No differences between epochs {old.name} and {new.name}.")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = build_parser().parse_args(argv)
    dsn = resolve_dsn(args.dsn, os.environ)
    if dsn is None:
        console.error(f"No connection string — pass --dsn or set {ENV_DSN}.")
        return EXIT_USAGE
    connection: _ClosableConnection | None = None
    try:
        connection = connect(dsn)
        return int(args.handler(args, connection))
    except (UsageError, CavernsError) as exc:
        console.error(str(exc))
        return EXIT_USAGE
    except Exception as exc:  # a driver, OS or unexpected error is an environment error, never a finding (exit 1)
        console.error(f"{type(exc).__name__}: {exc}")
        return EXIT_USAGE
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
