"""Per-day token usage tracking, used as a budget guard for paid agents.

``~/.agent-room/usage/<agent>.json`` looks like::

    {"agent": "muse", "days": {"2026-11-17": 1834}, "updated_at": "2026-11-17T16:49:47Z"}

Days are UTC calendar dates. The file is rewritten atomically and guarded by
an exclusive lock so concurrent ``room`` processes do not lose updates.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import paths
from .fsutil import atomic_write_json


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class UsageTracker:
    def __init__(self, agent: str, directory: Path | None = None) -> None:
        self.agent = agent
        self.directory = directory or paths.room_home() / "usage"
        self.path = self.directory / f"{agent}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / f".{self.agent}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data.get("days"), dict):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return {"agent": self.agent, "days": {}}

    def used(self, day: str | None = None) -> int:
        return int(self._load()["days"].get(day or utc_today(), 0))

    def add(self, tokens: int, day: str | None = None) -> int:
        """Add ``tokens`` to the day's total and return the new total."""
        day = day or utc_today()
        with self._locked():
            data = self._load()
            total = int(data["days"].get(day, 0)) + max(0, int(tokens))
            data["days"][day] = total
            data["agent"] = self.agent
            data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            atomic_write_json(self.path, data)
        return total


class DailyBudget:
    """A daily token limit backed by a UsageTracker."""

    def __init__(self, tracker: UsageTracker, limit: int) -> None:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        self.tracker = tracker
        self.limit = limit

    def exceeded_reason(self) -> str | None:
        used = self.tracker.used()
        if used >= self.limit:
            return (
                f"{self.tracker.agent}: daily token limit reached "
                f"({used}/{self.limit} tokens used today, UTC); not calling the API"
            )
        return None

    def summary(self) -> str:
        return f"{self.tracker.used()}/{self.limit} tokens today"
