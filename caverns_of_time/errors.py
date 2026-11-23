"""The exceptions Caverns raises, all in one module so a consumer can catch them without a driver.

Every error derives from `CavernsError`. Each component raises its own subset; the
conditions are documented in `caverns_of_time/README.md`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # nozdormu imports this module
    from caverns_of_time.nozdormu import StepStatus


class CavernsError(Exception):
    """Base of every error Caverns raises."""


class ChangesetError(CavernsError):
    """An invalid step, changeset or directory, a reserved name, an unknown label or a broken check."""


class LedgerNotInstalled(CavernsError):
    """The ledger is not installed: Caverns' own changeset is not fully applied."""


class PreflightRefused(CavernsError):
    """A run was refused before anything ran; `statuses` holds the steps that caused it."""

    def __init__(self, statuses: Sequence[StepStatus]) -> None:
        self.statuses = tuple(statuses)
        listing = ", ".join(f"{s.step.label} ({s.status})" for s in self.statuses)
        super().__init__(f"the run is refused for: {listing}")


class StepFailed(CavernsError):
    """A step's SQL failed; its transaction was rolled back and the failure recorded as ledger `version`."""

    def __init__(self, label: str, version: int) -> None:
        self.label = label
        self.version = version
        super().__init__(f"step {label!r} failed and was recorded as ledger version {version}")


class BaselineRefused(CavernsError):
    """A baseline was refused: the changeset already has ledger rows, or a named schema does not exist."""


class NoFingerprint(CavernsError):
    """Drift found nothing to compare with: no fingerprint is recorded yet."""


class EpochNotFound(CavernsError):
    """An epoch or an epoch's table was not found."""


class EpochRefused(CavernsError):
    """An epoch operation was refused: a table or key that cannot be tracked, or an epoch that cannot be used."""
