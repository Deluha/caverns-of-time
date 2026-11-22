"""Caverns of Time: a recorded timeline for PostgreSQL.

The components (Nozdormu, Chromie, Epochs, Infinite, Murozond, Timewalking) arrive
phase by phase; see ROADMAP.md. Until then the package only states its version.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("caverns-of-time")
except PackageNotFoundError:  # a bare checkout that was never installed
    __version__ = "0+unknown"
