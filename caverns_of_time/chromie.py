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
from typing import Protocol

from caverns_of_time import console
from caverns_of_time.errors import CavernsError
from caverns_of_time.nozdormu import Connection

ENV_DSN = "CAVERNS_DSN"
EXIT_OK = 0
EXIT_FINDING = 1
EXIT_USAGE = 2

# Shared by every subcommand: each passes `parents=[_common]`.
_common = argparse.ArgumentParser(add_help=False)
_common.add_argument("--dsn", help=f"connection string (default: the {ENV_DSN} environment variable)")
_common.add_argument("--format", choices=("text", "json"), default="text", help="output format")


class _ClosableConnection(Connection, Protocol):
    """A connection the CLI opened itself, so it also closes it."""

    def close(self) -> object: ...


class UsageError(Exception):
    """A usage or environment problem; the CLI reports it and exits with code 2."""


def build_parser() -> argparse.ArgumentParser:
    """Build the `chromie` parser; subcommands are added to its `command` subparsers."""
    parser = argparse.ArgumentParser(
        prog="chromie", description="the timeline of a PostgreSQL database: its ledger and drift"
    )
    # Ready for the subcommands of later commits; each sets `handler` with `set_defaults`.
    parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
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
