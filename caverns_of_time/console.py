"""Console output: the only module of the package that prints or reads.

Input: messages and tabular or JSON-able values from the CLI, and the answer to a confirmation from stdin. Output:
lines on stdout (results, success and information) or stderr (warnings, errors and the confirmation question), so
output can be piped. Icon vocabulary: ✅ success,
ℹ️ information, ⚠️ warning, ❌ error; the text never repeats the level the icon shows.
Conventions: `.claude/skills/caverns-of-time-development/references/style.md#messages`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Collection, Sequence

__all__ = ["confirm", "emit_json", "error", "info", "interactive", "success", "table", "warning"]

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


def interactive() -> bool:
    """Whether stdin is a terminal, so a person can be asked."""
    return sys.stdin.isatty()


def confirm(question: str) -> bool:
    """Ask *question* on stderr and read one line from stdin; true only for `y` or `yes` (any case), else false.

    End of input (no line) and Ctrl-C are refusals.
    """
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        print(file=sys.stderr)  # the answer's line was never ended
        return False
    return answer.strip().lower() in ("y", "yes")


def table(headers: Sequence[str] | None, rows: Sequence[Sequence[object]], *, right: Collection[int] = ()) -> None:
    """Print a table on stdout: columns padded to the widest cell, `None` shown as `-`.

    Without `headers` (None) no header line is printed and the widths come from the rows alone. Columns whose
    index is in `right` are right-aligned; trailing whitespace is stripped from each line.
    """
    cells = [[str(h) for h in headers]] if headers is not None else []
    cells += [[_NONE_CELL if value is None else str(value) for value in row] for row in rows]
    widths = [max(len(line[i]) for line in cells) for i in range(len(cells[0]) if cells else 0)]
    for line in cells:
        padded = [text.rjust(widths[i]) if i in right else text.ljust(widths[i]) for i, text in enumerate(line)]
        print(_GAP.join(padded).rstrip())


def emit_json(value: object) -> None:
    """Print `value` as indented JSON on stdout; values JSON cannot encode are written as text."""
    print(json.dumps(value, indent=2, default=str))
