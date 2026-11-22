"""Chromie, the command line: argument parsing, connection configuration and command dispatch.

Input: the command line and the `CAVERNS_DSN` environment variable. Output: console lines and an exit
code (0 success, 1 a finding, 2 a usage or environment error). This module holds command flow only; the
work lives in the component modules. Contract: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC
from pathlib import Path
from typing import Protocol

from caverns_of_time import console, nozdormu
from caverns_of_time.errors import CavernsError
from caverns_of_time.nozdormu import Connection

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


def _status(args: argparse.Namespace, connection: Connection) -> int:
    """`chromie status`: each step of a changeset directory against the ledger; 1 when a step is not applied."""
    name = args.changeset or Path(args.directory).resolve().name
    statuses = nozdormu.preflight(connection, nozdormu.Changeset.from_directory(name, args.directory))
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
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
