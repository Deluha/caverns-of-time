"""The exceptions Caverns raises, all in one module so a consumer can catch them without a driver.

Every error derives from `CavernsError`. Each component raises its own subset; the
conditions are documented in `caverns_of_time/README.md`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # nozdormu imports this module
    from caverns_of_time._fingerprint import Difference
    from caverns_of_time.murozond import Classification
    from caverns_of_time.nozdormu import StepStatus

# The differences a `DriftRefused` message lists before it counts the rest.
_SHOWN_DIFFERENCES = 5


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


class GuardRefused(PreflightRefused):
    """Murozond refused a run: a step that is not safe is not named in `acknowledge`; `statuses` and
    `classifications` hold those steps, in parallel."""

    def __init__(self, statuses: Sequence[StepStatus], classifications: Sequence[Classification]) -> None:
        self.statuses = tuple(statuses)
        self.classifications = tuple(classifications)
        listing = ", ".join(
            f"{s.step.label} ({c.risk}: {', '.join(dict.fromkeys(f.reason for f in c.findings))})"
            for s, c in zip(self.statuses, self.classifications, strict=True)
        )
        CavernsError.__init__(self, f"Murozond refuses the run for: {listing}: name each in acknowledge to run it")


class DriftRefused(CavernsError):
    """A run was refused because the live schema differs from the latest recorded fingerprint: `fingerprint_version`
    is the ledger version the fingerprint was recorded at, `differences` holds what differs."""

    def __init__(self, fingerprint_version: int, differences: Sequence[Difference]) -> None:
        self.fingerprint_version = fingerprint_version
        self.differences = tuple(differences)
        shown = [f"{d.change} {d.kind} {d.name}" for d in self.differences[:_SHOWN_DIFFERENCES]]
        listing = ", ".join(shown)
        if len(self.differences) > _SHOWN_DIFFERENCES:
            listing += f", and {len(self.differences) - _SHOWN_DIFFERENCES} more"
        super().__init__(
            f"the live schema differs from the fingerprint at ledger version {fingerprint_version}: {listing}: "
            "apply with on_drift='warn' to proceed and record the drift"
        )


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
