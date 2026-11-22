"""Console output: the only module of the package that prints.

Input: messages and tabular or JSON-able values from the CLI. Output: lines on stdout (results, success and
information) or stderr (warnings and errors), so output can be piped. Icon vocabulary: ✅ success,
ℹ️ information, ⚠️ warning, ❌ error; the text never repeats the level the icon shows.
Conventions: `.claude/skills/caverns-of-time-development/references/style.md#messages`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Collection, Sequence

__all__ = ["emit_json", "error", "info", "success", "table", "warning"]

_GAP = "  "
_NONE_CELL = "-"


def success(message: str) -> None:
    """Print a success line on stdout."""
    print(f"✅ {message}")


def info(message: str) -> None:
    """Print an information line on stdout."""
    print(f"ℹ️ {message}")


def warning(message: str) -> None:
    """Print a warning line on stderr."""
    print(f"⚠️ {message}", file=sys.stderr)


def error(message: str) -> None:
    """Print an error line on stderr."""
    print(f"❌ {message}", file=sys.stderr)


def table(headers: Sequence[str], rows: Sequence[Sequence[object]], *, right: Collection[int] = ()) -> None:
    """Print a table on stdout: columns padded to the widest cell, `None` shown as `-`.

    Columns whose index is in `right` are right-aligned; trailing whitespace is stripped from each line.
    """
    cells = [[str(h) for h in headers]]
    cells += [[_NONE_CELL if value is None else str(value) for value in row] for row in rows]
    widths = [max(len(line[i]) for line in cells) for i in range(len(headers))]
    for line in cells:
        padded = [text.rjust(widths[i]) if i in right else text.ljust(widths[i]) for i, text in enumerate(line)]
        print(_GAP.join(padded).rstrip())


def emit_json(value: object) -> None:
    """Print `value` as indented JSON on stdout; values JSON cannot encode are written as text."""
    print(json.dumps(value, indent=2, default=str))
