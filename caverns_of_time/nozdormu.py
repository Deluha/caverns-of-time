"""Nozdormu, the migration ledger: the step and changeset model, the checksum, the loaders.

Input: SQL text, a list of `Step`, or a directory of `.sql` files. Output: validated, immutable
`Step` and `Changeset` values. Later commits of this component add the ledger itself; the model
here touches no database. Contract and behaviour: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from caverns_of_time.errors import ChangesetError

__all__ = ["Changeset", "Step"]

_SQL_SUFFIX = ".sql"
_CHECK_SUFFIX = ".check.sql"


def _validate_name(kind: str, value: object) -> None:
    """A label or changeset name: a non-empty single line without surrounding whitespace."""
    if not isinstance(value, str):
        raise ChangesetError(f"{kind} {value!r} is not a string")
    if not value:
        raise ChangesetError(f"{kind} is empty")
    if value != value.strip():
        raise ChangesetError(f"{kind} {value!r} has surrounding whitespace")
    if "\n" in value or "\r" in value:
        raise ChangesetError(f"{kind} {value!r} spans more than one line")


def _checksum(sql: str) -> str:
    """SHA-256 hex of the SQL as UTF-8 with line endings normalised and the outer whitespace trimmed."""
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ChangesetError(f"cannot read {str(path)!r}: {error}") from error


@dataclass(frozen=True)
class Step:
    """One change: a label, the SQL that makes it and an optional check that it is present.

    A check is SQL returning exactly one row with one boolean column, true when the step's effect
    is already in the database. The checksum covers the SQL only.
    """

    label: str
    sql: str
    check: str | None = None

    def __post_init__(self) -> None:
        _validate_name("step label", self.label)
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ChangesetError(f"step {self.label!r} has no SQL")
        if self.check is not None and (not isinstance(self.check, str) or not self.check.strip()):
            raise ChangesetError(f"step {self.label!r} has an empty check")

    @classmethod
    def from_file(cls, path: str | os.PathLike[str], *, label: str | None = None, check: str | None = None) -> Step:
        """A step whose SQL is the UTF-8 text of *path*; the label defaults to the file name without `.sql`."""
        file = Path(path)
        return cls(label if label is not None else file.stem, _read_text(file), check)

    @property
    def checksum(self) -> str:
        """SHA-256 hex of the normalised SQL."""
        return _checksum(self.sql)


@dataclass(frozen=True)
class Changeset:
    """A named, ordered list of steps with unique labels."""

    name: str
    steps: Sequence[Step]

    def __post_init__(self) -> None:
        _validate_name("changeset name", self.name)
        steps = tuple(self.steps)
        if not steps:
            raise ChangesetError(f"changeset {self.name!r} has no steps")
        for step in steps:
            if not isinstance(step, Step):
                raise ChangesetError(f"changeset {self.name!r} holds {step!r}, which is not a Step")
        labels = [step.label for step in steps]
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        if duplicates:
            raise ChangesetError(f"changeset {self.name!r} repeats the step label {duplicates[0]!r}")
        object.__setattr__(self, "steps", steps)

    @classmethod
    def from_directory(cls, name: str, path: str | os.PathLike[str]) -> Changeset:
        """The `.sql` files of one directory, in file-name order, as steps.

        `<label>.check.sql` is the check of the step `<label>.sql`; other files are ignored and
        sub-directories are not read.
        """
        directory = Path(path)
        if not directory.is_dir():
            raise ChangesetError(f"{str(directory)!r} is not a directory")
        files = sorted(f for f in directory.iterdir() if f.is_file() and f.name.endswith(_SQL_SUFFIX))
        checks = {f.name.removesuffix(_CHECK_SUFFIX): f for f in files if f.name.endswith(_CHECK_SUFFIX)}
        steps = []
        for file in files:
            if file.name.endswith(_CHECK_SUFFIX):
                continue
            check_file = checks.pop(file.name.removesuffix(_SQL_SUFFIX), None)
            steps.append(Step.from_file(file, check=_read_text(check_file) if check_file else None))
        if checks:
            orphan = min(checks.values())
            step_file = orphan.name.removesuffix(_CHECK_SUFFIX) + _SQL_SUFFIX
            raise ChangesetError(f"check file {orphan.name!r} has no step file {step_file!r}")
        return cls(name, steps)
