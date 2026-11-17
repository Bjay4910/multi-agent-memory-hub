"""Filesystem locations used by the room.

Everything lives under ``~/.agent-room`` unless ``AGENT_ROOM_HOME`` is set
(the tests use that to stay out of the real home directory).
"""

from __future__ import annotations

import os
from pathlib import Path


def room_home() -> Path:
    override = os.environ.get("AGENT_ROOM_HOME")
    return Path(override).expanduser() if override else Path.home() / ".agent-room"


def presence_dir() -> Path:
    return room_home() / "presence"


def sessions_dir() -> Path:
    return room_home() / "sessions"
