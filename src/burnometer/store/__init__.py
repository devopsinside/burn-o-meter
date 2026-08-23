"""SQLite persistence — the boundary between the Python engine and any UI."""

from .db import ScanState, Store

__all__ = ["Store", "ScanState"]
