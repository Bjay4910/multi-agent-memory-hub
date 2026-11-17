"""Transcript messages and the JSONL session log.

Each line of ``~/.agent-room/sessions/<session_id>.jsonl`` is one message with
exactly these fields: ``ts``, ``session_id``, ``agent``, ``round``, ``role``,
``content``. See docs/SESSIONS.md for the full contract.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths

# Roles used in the transcript.
ROLE_USER = "user"  # the original task, round 0
ROLE_AGENT = "agent"  # an agent's reply during a discussion round
ROLE_NOTE = "note"  # system note, e.g. "gemini skipped: unreachable"
ROLE_FINAL = "final"  # the synthesized final answer
ROLE_MEMORY = "memory"  # recalled memory injected into prompts; never persisted


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class Message:
    session_id: str
    agent: str
    round: int
    role: str
    content: str
    ts: str = field(default_factory=utc_now_iso)

    def to_json(self) -> str:
        data = asdict(self)
        ordered = {k: data[k] for k in ("ts", "session_id", "agent", "round", "role", "content")}
        return json.dumps(ordered, ensure_ascii=False)


class SessionLog:
    """Append-only JSONL writer for one session."""

    def __init__(self, session_id: str, directory: Path | None = None) -> None:
        self.session_id = session_id
        self.directory = directory or paths.sessions_dir()
        self.path = self.directory / f"{session_id}.jsonl"

    def append(self, message: Message) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(message.to_json() + "\n")
            fh.flush()


def load_session(path: Path) -> list[Message]:
    messages = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                messages.append(Message(**json.loads(line)))
    return messages


def format_transcript(transcript: list[Message]) -> str:
    """Render the transcript as plain text for inclusion in a prompt."""
    lines = []
    for msg in transcript:
        if msg.role in (ROLE_USER, ROLE_MEMORY):
            continue  # the task and recalled memory are shown separately
        if msg.role == ROLE_NOTE:
            lines.append(f"[round {msg.round}] (note) {msg.content}")
        else:
            lines.append(f"[round {msg.round}] {msg.agent}:\n{msg.content}")
    return "\n\n".join(lines)
