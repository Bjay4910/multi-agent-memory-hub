"""The working layer: raw, temporary, redacted session notes.

Memory has two layers:

- **Working** (this module): what was said in a session, kept as captured
  (after redaction), one JSON file per session under
  ``~/.agent-room/working/``. Notes expire after ``DEFAULT_TTL_DAYS`` and are
  never sent to an extractor or the graph automatically.
- **Long-term** (``memory.db``): curated nodes and edges. The only way from
  working to long-term is ``room working promote <id>``, which runs the normal
  extraction pipeline.

Safety: the directory is 0700 and files 0600; the store refuses to live in a git
work tree, inside this repository or near the key folder (the same rules as the
vault). Text is redacted before every write, and a note in which a secret
pattern still matches is not written at all. Reading functions only open files
for reading: they never create, change or delete anything (expired notes are
just skipped until ``purge``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .. import paths
from ..fsutil import atomic_write_json
from ..transcript import ROLE_AGENT, ROLE_USER, Message, new_session_id
from .paths import RepoWriteError, ensure_outside_repo
from .redact import find_secrets, redact_secrets

SOURCES = ("claude-code", "antigravity")
_ASSISTANT_AGENT = {"claude-code": "claude", "antigravity": "gemini"}  # agent name for the assistant's turns
DEFAULT_TTL_DAYS = 14
MAX_NOTE_CHARS = 8000  # total turn text kept per note; the oldest turns are dropped first
MIN_USER_TURNS = 2
MIN_CHARS = 400
ID_RE = re.compile(r"[a-z][a-z-]*-\d{8}T\d{6}Z-[0-9a-f]{6}")
FORMAT_VERSION = 1


class WorkingSafetyError(RuntimeError):
    """The working store would live somewhere unsafe; nothing was written."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def working_dir() -> Path:
    return paths.room_home() / "working"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# -- location and writing ------------------------------------------------------------

def check_location(path: Path) -> Path:
    """Refuse a git work tree, this repository, and anything overlapping the key folder."""
    from .vault import VaultSafetyError, _check_not_keys_dir, git_work_tree

    path = Path(path).expanduser().resolve()
    try:
        _check_not_keys_dir(path)
    except VaultSafetyError:
        raise WorkingSafetyError(f"refusing to keep working notes at {path}: it overlaps the key folder") from None
    tree = git_work_tree(path)
    if tree is not None:
        raise WorkingSafetyError(f"refusing to keep working notes in a git work tree ({tree}); "
                                 "transcripts could be committed. Set AGENT_ROOM_HOME outside any repository.")
    try:
        ensure_outside_repo(path)
    except RepoWriteError as exc:
        raise WorkingSafetyError(str(exc)) from None
    return path


def ensure_dir() -> Path:
    directory = check_location(working_dir())
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    """One writer at a time across processes (two sessions ending together, or a resume)."""
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _clean_turn(turn: dict) -> dict | None:
    role = turn.get("role")
    text = turn.get("text")
    if role not in ("user", "assistant") or not isinstance(text, str):
        return None
    text = redact_secrets(text.strip())[:MAX_NOTE_CHARS]
    if not text:
        return None
    return {"role": role, "ts": str(turn.get("ts") or "")[:40], "text": text}


def upsert(source: str, source_key: str, project: str, items: list[dict], *,
           min_user_turns: int = MIN_USER_TURNS, min_chars: int = MIN_CHARS) -> tuple[str, dict | None]:
    """Add a session's turns to its working note, storing only turns not seen before.

    ``items`` are ``{"key", "role", "ts", "text"}`` dicts in order; ``key`` is stable per
    turn (so a resumed session adds only its new turns). Returns ``(status, note)`` with
    status ``created``, ``updated``, ``unchanged``, ``too-short`` or ``refused-secret``.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown working source {source!r}")
    directory = ensure_dir()
    key_hash = _hash(f"{source}:{source_key}")
    now = utcnow()
    with _lock(directory):
        existing = next((n for n in _read_all(directory)
                         if n.get("source") == source and n.get("source_key") == key_hash), None)
        live = existing is not None and not _expired(existing, now)
        seen = set(existing.get("turn_keys", [])) if existing else set()
        new_items = [i for i in items if isinstance(i, dict) and i.get("key") and _hash(str(i["key"])) not in seen]
        new_turns = [t for t in (_clean_turn(i) for i in new_items) if t]
        if not new_turns:
            return "unchanged", existing if live else None
        turns = (existing["turns"] if live else []) + new_turns
        if not live:
            users = sum(1 for t in turns if t["role"] == "user")
            if users < min_user_turns or sum(len(t["text"]) for t in turns) < min_chars:
                return "too-short", None
        dropped = existing.get("dropped_turns", 0) if live else 0
        while len(turns) > 1 and sum(len(t["text"]) for t in turns) > MAX_NOTE_CHARS:
            turns.pop(0)
            dropped += 1
        project = redact_secrets(project)[:40] or "unknown"
        if find_secrets("\n".join([project] + [t["text"] for t in turns])):
            return "refused-secret", None
        note = {
            "version": FORMAT_VERSION,
            "id": existing["id"] if existing else f"{source}-{new_session_id()}",
            "source": source,
            "project": project,
            "source_key": key_hash,
            "captured_at": existing["captured_at"] if live else iso(now),
            "updated_at": iso(now),
            "expires_at": iso(now + timedelta(days=DEFAULT_TTL_DAYS)),
            "promoted_at": existing.get("promoted_at") if live else None,
            "dropped_turns": dropped,
            "turn_keys": sorted(seen | {_hash(str(i["key"])) for i in new_items}),
            "turns": turns,
        }
        atomic_write_json(directory / f"{note['id']}.json", note)  # mkstemp: 0600
        return ("updated" if live else "created"), note


def _mark(note_id: str, field: str) -> None:
    directory = ensure_dir()
    with _lock(directory):
        path = directory / f"{note_id}.json"
        note = _read(path)
        if note is not None:
            note[field] = iso(utcnow())
            atomic_write_json(path, note)


def mark_promoted(note_id: str) -> None:
    """Extraction finished and stored the note in long-term memory."""
    _mark(note_id, "promoted_at")


def mark_promote_queued(note_id: str) -> None:
    """Promotion started but extraction only queued or failed; ``promote`` can be retried."""
    _mark(note_id, "promote_queued_at")


def purge(everything: bool = False) -> int:
    """Delete expired notes (or all notes). Returns how many were removed."""
    directory = working_dir()
    if not directory.is_dir():
        return 0
    check_location(directory)
    now, removed = utcnow(), 0
    with _lock(directory):
        for path in sorted(directory.glob("*.json")):
            note = _read(path)
            if everything or note is None or _expired(note, now):
                path.unlink(missing_ok=True)
                removed += 1
    return removed


# -- reading (never writes) -------------------------------------------------------------

def _read(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(note, dict) or note.get("id") != path.stem or not isinstance(note.get("turns"), list):
        return None
    return note


def _read_all(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    return [n for n in (_read(p) for p in sorted(directory.glob("*.json"))) if n is not None]


def _expired(note: dict, now: datetime) -> bool:
    try:
        return parse_ts(note["expires_at"]) <= now
    except (KeyError, TypeError, ValueError):
        return True  # unreadable expiry: treat as expired, never return it


def list_notes() -> list[dict]:
    """Live (unexpired) notes, most recently updated first."""
    now = utcnow()
    notes = [n for n in _read_all(working_dir()) if not _expired(n, now)]
    return sorted(notes, key=lambda n: n.get("updated_at", ""), reverse=True)


def get_note(note_id: str) -> dict | None:
    if not ID_RE.fullmatch(note_id or ""):
        raise ValueError("not a working note id")
    note = _read(working_dir() / f"{note_id}.json")
    return note if note is not None and not _expired(note, utcnow()) else None


def latest(source: str, n: int = 1) -> list[dict]:
    return [note for note in list_notes() if note.get("source") == source][:n]


def _words(query: str) -> list[str]:
    return sorted({w for w in re.findall(r"\w+", query.lower()) if len(w) >= 2})


def search(query: str, limit: int = 5, source: str | None = None) -> list[tuple[dict, list[str]]]:
    """Live notes containing ALL words of ``query`` (case-insensitive), with short snippets.

    ``source`` limits the search to one capture source (``claude-code``, ``antigravity``)."""
    words = _words(query)
    if not words:
        return []
    results = []
    for note in list_notes():
        if source is not None and note.get("source") != source:
            continue
        body = "\n".join([note.get("project", "")] + [t["text"] for t in note["turns"]])
        lower = body.lower()
        if all(w in lower for w in words):
            snippets = []
            for w in words[:3]:
                i = lower.find(w)
                snippets.append("..." + " ".join(body[max(0, i - 80): i + 80].split()) + "...")
            results.append((note, snippets))
    return results[:limit]


# -- formatting -------------------------------------------------------------------------

def _day(value: str | None) -> str:
    return (value or "?")[:10]


def format_list(notes: list[dict]) -> str:
    lines = []
    for n in notes:
        chars = sum(len(t["text"]) for t in n["turns"])
        promoted = f", promoted {_day(n['promoted_at'])}" if n.get("promoted_at") else ""
        lines.append(f"{n['id']}  {n.get('project', '?')}: {len(n['turns'])} turns, {chars:,} chars, "
                     f"updated {_day(n.get('updated_at'))}, expires {_day(n.get('expires_at'))}{promoted}")
    return "\n".join(lines)


def format_note(note: dict) -> str:
    extra = []
    if note.get("promoted_at"):
        extra.append(f"promoted {_day(note['promoted_at'])}")
    if note.get("dropped_turns"):
        extra.append(f"{note['dropped_turns']} earlier turns dropped (size cap)")
    head = (f"{note['id']}  [{note.get('source')}, project {note.get('project', '?')}]\n"
            f"captured {_day(note.get('captured_at'))}, updated {_day(note.get('updated_at'))}, "
            f"expires {_day(note.get('expires_at'))}" + "".join(f", {e}" for e in extra))
    return head + "\n\n" + "\n\n".join(f"[{t['role']}] {t['text']}" for t in note["turns"])


def format_search(results: list[tuple[dict, list[str]]]) -> str:
    blocks = []
    for note, snippets in results:
        blocks.append(f"{note['id']}  {note.get('project', '?')}, updated {_day(note.get('updated_at'))}, "
                      f"expires {_day(note.get('expires_at'))}\n" + "\n".join(f"    {s}" for s in snippets))
    return "\n".join(blocks)


# -- promotion ---------------------------------------------------------------------------

def promotion_messages(note: dict) -> tuple[str, list[Message]]:
    """The note as a session log: the user's turns as ``user``, the assistant's as ``agent``.

    The default extraction scope then reads only the user's own words, as for room sessions.
    """
    sid = note["id"]
    messages, first_user = [], True
    for i, turn in enumerate(note["turns"]):
        if turn["role"] == "user":
            text = turn["text"]
            if first_user:
                text, first_user = f"Project: {note.get('project', '?')}\n\n{text}", False
            messages.append(Message(sid, note["source"], i, ROLE_USER, text))
        else:
            messages.append(Message(sid, _ASSISTANT_AGENT.get(note["source"], "claude"), i, ROLE_AGENT, turn["text"]))
    return sid, messages
