"""Strictly read-only access to the memory database, for other programs (the MCP server).

The file is opened with SQLite's URI ``mode=ro``, so SQLite itself refuses every
write, and ``PRAGMA query_only`` is set on top. No migration runs, no file or
directory is created, and permissions are not touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import memory_db_path
from .store import SCHEMA_VERSION, MemoryStore


class MemoryUnavailable(RuntimeError):
    """The database is missing, empty or not usable; the message is meant for people."""


class ReadOnlyMemory(MemoryStore):
    """A :class:`MemoryStore` whose connection cannot write. Only the read methods are meant for use."""

    def __init__(self, path: Path | None = None) -> None:  # noqa: D107 - deliberately skips MemoryStore.__init__
        self.path = Path(path) if path else memory_db_path()
        if not self.path.is_file():
            raise MemoryUnavailable("No memory yet: the room has not stored anything on this machine.")
        try:
            self.conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only = ON")
            row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.Error as exc:
            raise MemoryUnavailable(f"The memory database could not be read ({exc}).") from None
        version = row[0] if row else 0
        if version < 2:
            self.conn.close()
            raise MemoryUnavailable("The memory database is empty or was never initialized.")
        if version > SCHEMA_VERSION:
            self.conn.close()
            raise MemoryUnavailable(f"The memory database uses schema v{version}; this room understands v{SCHEMA_VERSION}.")

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        return row[0] if row else 0

    def tx(self):  # type: ignore[override]
        raise PermissionError("read-only memory: writes are not allowed")

    def migrate(self, target: int | None = None) -> int:  # noqa: ARG002
        raise PermissionError("read-only memory: migrations are not allowed")
