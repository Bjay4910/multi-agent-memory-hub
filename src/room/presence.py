"""Presence files: who is working right now.

While an agent works, ``~/.agent-room/presence/<agent>.json`` says so. Other
processes (for example a power manager) read these files. The contract is
documented in docs/PRESENCE.md; keep the two in sync.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from . import paths

STATUS_WORKING = "working"
STATUS_IDLE = "idle"
TASK_MAX_CHARS = 120
HEARTBEAT_INTERVAL_S = 30.0
STALE_AFTER_S = 120.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def write_presence(agent: str, status: str, task: str = "", directory: Path | None = None) -> Path:
    """Atomically write one presence file (temp file in the same dir, then rename)."""
    if status not in (STATUS_WORKING, STATUS_IDLE):
        raise ValueError(f"invalid status {status!r}")
    directory = directory or paths.presence_dir()
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "agent": agent,
        "status": status,
        "task": task[:TASK_MAX_CHARS],
        "pid": os.getpid(),
        "updated_at": _iso(_now()),
    }
    target = directory / f"{agent}.json"
    fd, tmp = tempfile.mkstemp(prefix=f".{agent}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return target


@dataclass(frozen=True)
class PresenceEntry:
    agent: str
    status: str  # raw status from the file
    task: str
    pid: int
    updated_at: datetime

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or _now()) - self.updated_at).total_seconds()

    def is_stale(self, now: datetime | None = None, stale_after: float = STALE_AFTER_S) -> bool:
        return self.status == STATUS_WORKING and self.age_seconds(now) > stale_after

    def effective_status(self, now: datetime | None = None, stale_after: float = STALE_AFTER_S) -> str:
        """``working`` only if the file says so AND it is fresh; otherwise ``idle``."""
        if self.status == STATUS_WORKING and not self.is_stale(now, stale_after):
            return STATUS_WORKING
        return STATUS_IDLE


def read_presence(directory: Path | None = None) -> list[PresenceEntry]:
    """Read every presence file, skipping temp files and unparseable entries."""
    directory = directory or paths.presence_dir()
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries.append(
                PresenceEntry(
                    agent=str(data["agent"]),
                    status=str(data["status"]),
                    task=str(data.get("task", "")),
                    pid=int(data.get("pid", 0)),
                    updated_at=_parse_iso(str(data["updated_at"])),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return entries


@asynccontextmanager
async def working(
    agent: str,
    task: str,
    directory: Path | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
) -> AsyncIterator[None]:
    """Mark ``agent`` as working for the duration of the block.

    Writes ``working`` on entry, refreshes ``updated_at`` every
    ``heartbeat_interval`` seconds, and always writes ``idle`` on exit
    (including on errors and cancellation).
    """

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval)
            write_presence(agent, STATUS_WORKING, task, directory)

    write_presence(agent, STATUS_WORKING, task, directory)
    beat = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass
        write_presence(agent, STATUS_IDLE, "", directory)
