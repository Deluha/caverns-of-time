"""Caverns of Time: a recorded timeline for PostgreSQL.

The components (Nozdormu, Chromie, Epochs, Infinite, Murozond, Timewalking) arrive
phase by phase; see ROADMAP.md. Present so far: Nozdormu (`caverns_of_time.nozdormu`, the
migration ledger) and the shared exceptions (`caverns_of_time.errors`).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("caverns-of-time")
except PackageNotFoundError:  # a bare checkout that was never installed
    __version__ = "0+unknown"
