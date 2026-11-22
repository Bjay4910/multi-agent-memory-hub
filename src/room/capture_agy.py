"""Capture Antigravity CLI (``agy``) conversations into the working layer (opt-in), and install the hook.

Flow: agy has no end-of-session hook, so a ``Stop`` hook (it fires when the agent's
loop ends, about once per turn) runs ``room capture antigravity`` with the hook
payload on stdin. That process does only cheap checks, hands the payload to a
detached worker over a pipe, prints ``{}`` and exits 0: it never blocks or fails
agy. The worker streams the transcript, keeps only the user's typed request and
the assistant's final reply of each turn, redacts it, and upserts it into the
working layer keyed by conversation id, so repeated hooks add only new turns.

Guards (besides the opt-in flag shared with Claude Code capture):
- ``ROOM_SPAWNED=1`` (set for every CLI agent the room starts; agy passes it on to
  its hooks) makes capture a no-op.
- The hook skips agy runs in print mode (``agy -p`` / ``--print``), found from the
  hook's parent processes, so non-interactive runs are never captured.
- A transcript whose first request is the room's own system prompt is skipped.
- The conversation id must be a lowercase UUID, and the transcript must be exactly
  ``<brain>/<id>/.system_generated/logs/transcript[_full].jsonl`` with no symlinks
  leading out of ``$ANTIGRAVITY_CLI_HOME/brain`` (default
  ``~/.gemini/antigravity-cli/brain``).
- Transcripts are read line by line with caps on total bytes, lines and line length.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import capture
from .agents.base import SYSTEM_PROMPT
from .memory import working
from .memory.redact import redact_secrets

SOURCE = "antigravity"
HOOK_EVENT = "Stop"
HOOK_GROUP = "agent-room"
HOOK_TIMEOUT_S = 5
WORKER_ARGS = ("-m", "room.cli", "capture", "antigravity", "--worker")  # fixed; input goes over stdin
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_LINES = 100_000
MAX_LINE_BYTES = 4 * 1024 * 1024
TRANSCRIPT_NAMES = ("transcript_full.jsonl", "transcript.jsonl")

_CID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_TAG_BLOCK_RE = re.compile(r"<([A-Z][A-Z_]*)>.*?</\1>", re.S)
_OURS = re.compile(r"(?:^|/)room'?\s+capture\s+antigravity\s*$")
# The room's own prompt starts with this (see room.agents.base.SYSTEM_PROMPT).
_ROOM_PROMPT_PREFIX = SYSTEM_PROMPT.split("{name}", 1)[1][:60]


class HooksError(capture.SettingsError):
    pass


# -- locations ---------------------------------------------------------------------------

def agy_home() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_CLI_HOME") or Path.home() / ".gemini" / "antigravity-cli").expanduser()


def brain_root() -> Path:
    return agy_home() / "brain"


def default_hooks_path() -> Path:
    return Path.home() / ".gemini" / "config" / "hooks.json"


# -- payload checks ------------------------------------------------------------------------

def valid_conversation_id(value: object) -> bool:
    return isinstance(value, str) and _CID_RE.fullmatch(value) is not None


def _project_source(hook: dict) -> str:
    cwd = hook.get("cwd")
    if isinstance(cwd, str) and 0 < len(cwd) <= 4096:
        return cwd
    paths = hook.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and len(paths[0]) <= 4096:
        return paths[0]
    return ""


def transcript_for(hook: dict) -> tuple[str, Path | None]:
    """(status, path): the transcript file the payload names, if it is the one it should be."""
    cid, given = hook.get("conversationId"), hook.get("transcriptPath")
    if not valid_conversation_id(cid) or not isinstance(given, str) or not 0 < len(given) <= 4096 or "\x00" in given:
        return "bad-input", None
    given_path = Path(given).expanduser()
    if not given_path.is_absolute() or given_path.name not in TRANSCRIPT_NAMES:
        return "bad-input", None
    root = brain_root().resolve()
    expected = root / cid / ".system_generated" / "logs" / given_path.name
    try:
        resolved = given_path.resolve()
    except (OSError, RuntimeError):
        return "outside-brain", None
    if resolved != expected:
        return "outside-brain", None
    return "ok", expected


# -- transcript parsing ---------------------------------------------------------------------

def _open_binary(path: Path) -> BinaryIO:
    return open(path, "rb")


def _lines(path: Path):
    """Yield decoded lines; skip lines over MAX_LINE_BYTES. Raises OverflowError past the caps."""
    total = 0
    with _open_binary(path) as fh:
        for _ in range(MAX_LINES):
            line = fh.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            total += len(line)
            if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):  # drain the rest of the giant line, bounded reads
                    line = fh.readline(MAX_LINE_BYTES + 1)
                    total += len(line)
                    if total > MAX_TRANSCRIPT_BYTES:
                        raise OverflowError("bytes")
                continue
            if total > MAX_TRANSCRIPT_BYTES:
                raise OverflowError("bytes")
            yield line.decode("utf-8", errors="replace")
        if fh.readline(1):
            raise OverflowError("lines")


def user_request_text(content: str) -> str:
    """The user's typed request: the USER_REQUEST blocks, never metadata or settings blocks."""
    blocks = _REQUEST_RE.findall(content)
    text = "\n\n".join(b.strip() for b in blocks) if blocks else _TAG_BLOCK_RE.sub("", content)
    return text.strip()


def parse_transcript(path: Path) -> tuple[str | None, list[dict]]:
    """(first raw user request, turns). Keeps each user request and the last reply text before the next request."""
    first_request, items, pending = None, [], None

    def flush():
        nonlocal pending
        if pending:
            items.append(pending)
        pending = None

    for line in _lines(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind, content, step = entry.get("type"), entry.get("content"), entry.get("step_index")
        key = f"step:{step}" if isinstance(step, int) else None
        if kind == "USER_INPUT":
            flush()  # any input ends the turn, so its final reply is kept
            if entry.get("source", "USER_EXPLICIT") != "USER_EXPLICIT" or not isinstance(content, str):
                continue  # only what the user typed is stored
            raw = user_request_text(content)
            if first_request is None:
                first_request = raw
            text = capture.strip_text(raw)
            if text and key:
                items.append({"key": key, "role": "user", "ts": entry.get("created_at"), "text": text, "raw": text,
                              "cut": capture.MAX_TURN_CHARS})
        elif kind == "PLANNER_RESPONSE" and isinstance(content, str) and content.strip() and key:
            # Only the reply text: never 'thinking' or 'tool_calls'. A later reply in the same turn replaces it.
            text = capture.strip_text(content)
            if text:
                if pending:  # a later reply replaces it. Never stored as a new turn: its raw text is used for
                    # withholding, and its key lets working.upsert rebuild it if an earlier Stop hook stored it.
                    items.append({"role": "draft", "key": pending["key"], "raw": pending["raw"], "cut": pending["cut"]})
                pending = {"key": key, "role": "assistant", "ts": entry.get("created_at"), "text": text, "raw": text,
                           "cut": capture.MAX_TURN_CHARS}
        # GENERIC (tool output, file contents) and SYSTEM_MESSAGE are dropped.
    flush()
    return first_request, items


def is_room_prompt(first_request: str | None) -> bool:
    return bool(first_request) and _ROOM_PROMPT_PREFIX in first_request[:300]


# -- capture -----------------------------------------------------------------------------------

def capture_antigravity(hook: dict) -> str:
    """Store one agy conversation's turns in the working layer. Returns a status word."""
    return _capture_antigravity(hook)[0]


def _capture_antigravity(hook: dict) -> tuple[str, dict | None]:
    """(status, the note as written, if one was)."""
    if os.environ.get(capture.ROOM_SPAWNED_ENV) == "1":
        return "room-spawned", None
    if not capture.is_enabled():
        return "disabled", None
    if not isinstance(hook, dict):
        return "bad-input", None
    status, path = transcript_for(hook)
    if path is None:
        return status, None
    if not path.is_file():
        return "missing-transcript", None
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return "too-large", None
    try:
        first_request, items = parse_transcript(path)
    except OverflowError:
        return "too-large", None
    if first_request is None:
        return "no-user-input", None
    if is_room_prompt(first_request):
        return "room-spawned", None
    try:
        source = _project_source(hook)
        return working.upsert(SOURCE, hook["conversationId"], Path(source).name if source else "", items)
    except working.WorkingSafetyError:
        return "refused-location", None


def _ancestor_args(depth: int = 3) -> list[str]:
    """Command lines of the hook's parent processes (nearest first). Empty on any failure."""
    out, pid = [], os.getppid()
    try:
        for _ in range(depth):
            if pid <= 1:
                break
            res = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=1, check=False)
            ppid, _, args = res.stdout.strip().partition(" ")
            if not args:
                break
            out.append(args)
            pid = int(ppid)
    except Exception:  # noqa: BLE001 - unknown ancestry: the other guards still apply
        pass
    return out


def is_print_mode(ancestors: list[str]) -> bool:
    """True if an ``agy`` ancestor was started with -p / --print (non-interactive)."""
    for args in ancestors:
        tokens = args.split()
        if not tokens or Path(tokens[0]).name != "agy":
            continue
        return any(t in ("-p", "--print") or t.startswith("--print=") for t in tokens[1:])
    return False


def run_hook(raw: str) -> int:
    """The Stop hook: cheap checks, start the detached worker, exit 0. Never raises."""
    try:
        if os.environ.get(capture.ROOM_SPAWNED_ENV) == "1" or not capture.is_enabled() or len(raw) > capture.MAX_HOOK_INPUT:
            return 0
        hook = json.loads(raw)
        if not isinstance(hook, dict) or not valid_conversation_id(hook.get("conversationId")):
            return 0
        if is_print_mode(_ancestor_args()):
            capture.log_event("antigravity:non-interactive")
            return 0
        # Small and bounded (well under a pipe buffer), so the write below never waits on the worker.
        payload = {k: hook[k] for k in ("conversationId", "transcriptPath", "cwd") if isinstance(hook.get(k), str)}
        if isinstance(hook.get("workspacePaths"), list):
            payload["workspacePaths"] = [p for p in hook["workspacePaths"][:1] if isinstance(p, str)]
        payload = {k: (v[:4096] if isinstance(v, str) else [x[:4096] for x in v]) for k, v in payload.items()}
        if len(json.dumps(payload)) > 8192:  # odd, long paths: keep only what capture needs (project becomes 'unknown')
            payload = {k: payload[k] for k in ("conversationId", "transcriptPath") if k in payload}
        payload = json.dumps(payload)
        proc = subprocess.Popen(
            [sys.executable, *WORKER_ARGS],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(capture.capture_dir()), start_new_session=True, close_fds=True,
        )
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.close()
    except Exception as exc:  # noqa: BLE001 - the hook must never fail agy
        capture.log_event(f"antigravity:hook-error:{type(exc).__name__}")
    return 0


def run_worker(raw: str) -> int:
    """The detached worker: capture, log the status (no content). Never raises."""
    try:
        hook = json.loads(raw[: capture.MAX_HOOK_INPUT])
        status, note = _capture_antigravity(hook)
        # the project name exactly as stored in the note (masked and scrubbed with the turns' values)
        capture.log_event(f"antigravity:{status}", note["project"] if note and status in ("created", "updated") else "")
    except Exception as exc:  # noqa: BLE001
        capture.log_event(f"antigravity:error:{type(exc).__name__}")
    return 0


# -- hooks.json installer -------------------------------------------------------------------------

def hook_command(executable: str | None = None) -> str:
    return f"{shlex.quote(executable or capture.room_executable())} capture antigravity"


def hook_config(executable: str | None = None) -> dict:
    return {HOOK_GROUP: {HOOK_EVENT: [{"type": "command", "command": hook_command(executable), "timeout": HOOK_TIMEOUT_S}]}}


def is_our_handler(handler: object) -> bool:
    return (isinstance(handler, dict) and handler.get("type", "command") == "command"
            and isinstance(handler.get("command"), str) and bool(_OURS.search(handler["command"].strip())))


def _our_group_state(data: dict) -> str:
    """'absent', 'ours' or raises HooksError if the 'agent-room' key holds something else."""
    if HOOK_GROUP not in data:
        return "absent"
    group = data[HOOK_GROUP]
    handlers = group.get(HOOK_EVENT) if isinstance(group, dict) else None
    if (isinstance(handlers, list) and handlers and all(is_our_handler(h) for h in handlers)
            and set(group) <= {"enabled", HOOK_EVENT}):
        return "ours"
    raise HooksError(f"the '{HOOK_GROUP}' group exists but was not written by room; remove it by hand first")


def _is_disabled(data: dict) -> bool:
    return data.get(HOOK_GROUP, {}).get("enabled", True) is False


def install_state(path: Path | None = None) -> str:
    """'installed', 'installed but disabled' or 'not installed'."""
    path = Path(path or default_hooks_path()).expanduser()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    try:
        if not isinstance(data, dict) or _our_group_state(data) != "ours":
            return "not installed"
    except HooksError:
        return "not installed"
    return "installed but disabled" if _is_disabled(data) else "installed"


def is_installed(path: Path | None = None) -> bool:
    return install_state(path) == "installed"


_WS = " \t\n\r"


def _skip(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WS:
        i += 1
    return i


def _members(text: str) -> tuple[int, int, list[tuple[str, int, int]]]:
    """Top-level object layout: (index of '{', index of the closing '}', [(key, start, end), ...]).

    ``start`` is where the key's quote begins and ``end`` is just past its value, so a
    member can be added or removed without re-serializing (or even touching) any other.
    """
    dec = json.JSONDecoder()
    i = _skip(text, 0)
    if i >= len(text) or text[i] != "{":
        raise HooksError("the file does not hold a JSON object")
    opening, members = i, []
    i = _skip(text, i + 1)
    if i < len(text) and text[i] == "}":
        return opening, i, members
    while True:
        start = i
        key, i = dec.raw_decode(text, i)
        i = _skip(text, i)
        if not isinstance(key, str) or text[i:i + 1] != ":":
            raise HooksError("unexpected JSON layout")
        _, end = dec.raw_decode(text, _skip(text, i + 1))
        members.append((key, start, end))
        i = _skip(text, end)
        if text[i:i + 1] == ",":
            i = _skip(text, i + 1)
            continue
        if text[i:i + 1] == "}":
            return opening, i, members
        raise HooksError("unexpected JSON layout")


def _member_text() -> str:
    block = json.dumps(hook_config()[HOOK_GROUP], indent=2, ensure_ascii=False).replace("\n", "\n  ")
    return f"{json.dumps(HOOK_GROUP)}: {block}"


def _edited(text: str, action: str) -> str:
    opening, closing, members = _members(text)
    ours = [m for m in members if m[0] == HOOK_GROUP]
    if len(ours) > 1:
        raise HooksError(f"the file has more than one '{HOOK_GROUP}' group; fix it by hand first")
    if action == "install":
        if not members:
            return text[:opening + 1] + "\n  " + _member_text() + "\n" + text[opening + 1:]
        last_end = members[-1][2]
        return text[:last_end] + ",\n  " + _member_text() + text[last_end:]
    _, start, end = ours[0]
    index = members.index(ours[0])
    if len(members) == 1:
        tail = end + 1 if text[end:end + 1] == "\n" else end
        return text[:opening + 1] + text[tail:]
    if index > 0:
        return text[:members[index - 1][2]] + text[end:]
    return text[:start] + text[members[1][1]:]


def change_hooks(path: Path, action: str) -> str:
    """Add (``install``) or remove (``uninstall``) only the 'agent-room' group.

    The edit inserts or cuts only that member's text, so every other group (such as
    the power manager's 'agent-power') stays byte-for-byte as it was. The result is
    parsed and checked before it is written.
    """
    import tempfile

    path = Path(path).expanduser()
    data, raw = capture._load(path)  # refuses invalid JSON and non-objects
    state = _our_group_state(data or {})
    if action == "install" and state == "ours":
        if _is_disabled(data or {}):
            return (f"The '{HOOK_GROUP}' group is installed but disabled (\"enabled\": false); nothing changed. "
                    "To turn it on, set \"enabled\" to true in the file yourself.")
        return "Hook already installed; nothing changed."
    if action == "uninstall" and state == "absent":
        return "Hook not installed; nothing to remove."
    if raw is None:
        new_text = "{\n  " + _member_text() + "\n}\n"
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HooksError(f"{path} is not UTF-8") from None
        new_text = _edited(text, action)
    expected = {k: v for k, v in (data or {}).items() if k != HOOK_GROUP}
    if action == "install":
        expected[HOOK_GROUP] = hook_config()[HOOK_GROUP]
    if json.loads(new_text) != expected:
        raise HooksError("internal check failed: the edited file would not match; nothing was changed")
    backup = None
    if raw is not None:
        if path.read_bytes() != raw:
            raise HooksError(f"{path} changed while it was being edited; try again")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            backup, n = path.with_name(f"{path.name}.bak-{stamp}-{n}"), n + 1
        shutil.copy2(path, backup)
    mode = (path.stat().st_mode & 0o777) if raw is not None else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if raw is not None and path.read_bytes() != raw:
            raise HooksError(f"{path} changed while it was being edited; try again")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    verb = "Installed" if action == "install" else "Removed"
    return (f"{verb} the '{HOOK_GROUP}' {HOOK_EVENT} hook in {path}"
            + (f" (backup: {backup.name})" if backup else "") + ". Other groups were not changed.")
