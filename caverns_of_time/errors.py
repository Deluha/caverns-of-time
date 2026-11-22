"""The exceptions Caverns raises, all in one module so a consumer can catch them without a driver.

Every error derives from `CavernsError`. Each component raises its own subset; the
conditions are documented in `caverns_of_time/README.md`.
"""


class CavernsError(Exception):
    """Base of every error Caverns raises."""


class ChangesetError(CavernsError):
    """An invalid step, changeset or directory, a reserved name, an unknown label or a broken check."""


class LedgerNotInstalled(CavernsError):
    """The ledger is not installed: Caverns' own changeset is not fully applied."""
