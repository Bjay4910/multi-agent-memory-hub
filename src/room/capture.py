"""Capture Claude Code sessions into the working layer (opt-in), and install the hook.

Flow: Claude Code's ``SessionEnd`` hook runs ``room capture claude`` with the hook
input on stdin. That process does only cheap checks, hands the input to a
detached worker over a pipe, and exits 0 at once: it never blocks or fails
Claude Code. The worker reads the transcript, keeps only the user's typed text
and the assistant's text, redacts it, and stores it in the working layer
(:mod:`room.memory.working`). Nothing reaches the long-term graph unless the
user runs ``room working promote``.

Guards:
- Off unless ``~/.agent-room/capture/enabled`` exists (``room capture enable``).
- ``ROOM_SPAWNED=1`` (set for every CLI agent the room starts) makes capture a
  no-op, so the room never captures its own ``claude -p`` calls. Those calls
  also use ``--no-session-persistence``, and transcripts whose entrypoint is an
  SDK / print-mode one (``sdk-*``) are skipped as well.
- Only transcripts under ``$CLAUDE_CONFIG_DIR/projects`` (default
  ``~/.claude/projects``) are read.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .memory import working
from .memory.redact import redact_secrets

ROOM_SPAWNED_ENV = "ROOM_SPAWNED"
SOURCE = "claude-code"
MAX_HOOK_INPUT = 64 * 1024
MAX_TURN_CHARS = 2000
MAX_TRANSCRIPT_BYTES = 200 * 1024 * 1024
WORKER_ARGS = ("-m", "room.cli", "capture", "claude", "--worker")  # fixed; input goes over stdin
HOOK_EVENT = "SessionEnd"
HOOK_TIMEOUT_S = 5

# Harness wrappers inside user text: never the user's own words.
_WRAPPERS = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|command-contents|local-command-stdout|"
    r"local-command-stderr|local-command-caveat|bash-input|bash-stdout|bash-stderr|user-prompt-submit-hook)>"
    r".*?</\1>", re.S)
# Markers Claude Code writes as user text when a turn is interrupted.
_INTERRUPT = re.compile(r"^\[Request interrupted by user[^\]]*\]$")


# -- locations and switches --------------------------------------------------------------

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def projects_root() -> Path:
    return claude_home() / "projects"


def default_settings_path() -> Path:
    return claude_home() / "settings.json"


def capture_dir() -> Path:
    return paths.room_home() / "capture"


def enabled_flag() -> Path:
    return capture_dir() / "enabled"


def is_enabled() -> bool:
    return enabled_flag().is_file()


def set_enabled(on: bool) -> None:
    if on:
        directory = working.check_location(capture_dir())
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        fd = os.open(enabled_flag(), os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    else:
        enabled_flag().unlink(missing_ok=True)


def log_event(status: str, project: str = "") -> None:
    """One line per capture, no content. Never raises."""
    try:
        if not capture_dir().is_dir():
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        fd = os.open(capture_dir() / "capture.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {status}{' ' + project if project else ''}\n")
    except Exception:  # noqa: BLE001 - logging must never break capture
        pass


# -- transcript parsing --------------------------------------------------------------------

def clean_text(text: str) -> str:
    """Strip harness wrappers, redact, then cut. Redacting first means a secret that
    straddles the cut is removed whole, not left as a fragment too short to match."""
    text = _WRAPPERS.sub("", text).strip()
    if _INTERRUPT.match(text):
        return ""
    text = redact_secrets(text)
    if len(text) > MAX_TURN_CHARS:
        text = text[:MAX_TURN_CHARS] + f"\n[... {len(text) - MAX_TURN_CHARS:,} characters cut]"
    return text


def _texts(entry: dict) -> list[str]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [content] if entry.get("type") == "user" else []
    if not isinstance(content, list):
        return []
    # Only plain text blocks: thinking, tool_use, tool_result and images are dropped.
    return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def parse_transcript(path: Path) -> tuple[set[str], list[dict]]:
    """(entrypoints seen, turns) from a Claude Code transcript JSONL."""
    import hashlib

    entrypoints, items = set(), []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("entrypoint"), str):
                entrypoints.add(entry["entrypoint"])
            kind = entry.get("type")
            if kind not in ("user", "assistant") or entry.get("isSidechain") or entry.get("isMeta"):
                continue
            # Compaction summaries are written by the model, not typed by the user.
            if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
                continue
            text = "\n\n".join(t for t in (clean_text(x) for x in _texts(entry)) if t)
            if not text:
                continue
            key = entry.get("uuid") if isinstance(entry.get("uuid"), str) else hashlib.sha256(line.encode()).hexdigest()
            items.append({"key": key, "role": kind, "ts": entry.get("timestamp"), "text": text})
    return entrypoints, items


def project_tag(cwd: str) -> str:
    name = Path(cwd).name if cwd else ""
    tag = re.sub(r"[^A-Za-z0-9 _.\-]+", "-", name).strip(" -.")[:40]
    return working.redact_for_note(tag) or "unknown"


# -- capture -------------------------------------------------------------------------------

def _valid_hook(hook: object) -> bool:
    return (isinstance(hook, dict)
            and isinstance(hook.get("session_id"), str) and 0 < len(hook["session_id"]) <= 200
            and isinstance(hook.get("transcript_path"), str) and 0 < len(hook["transcript_path"]) <= 4096
            and isinstance(hook.get("cwd", ""), str))


def capture_claude(hook: dict) -> str:
    """Store one Claude Code session in the working layer. Returns a status word."""
    if os.environ.get(ROOM_SPAWNED_ENV) == "1":
        return "room-spawned"
    if not is_enabled():
        return "disabled"
    if not _valid_hook(hook):
        return "bad-input"
    path = Path(hook["transcript_path"]).expanduser().resolve()
    root = projects_root().resolve()
    if root not in path.parents or path.suffix != ".jsonl":
        return "outside-projects"
    if not path.is_file():
        return "missing-transcript"
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return "too-large"
    entrypoints, items = parse_transcript(path)
    if any(ep.startswith("sdk") for ep in entrypoints):
        return "non-interactive"
    for item in items:
        item["text"] = redact_secrets(item["text"])  # before anything is stored
    try:
        status, _ = working.upsert(SOURCE, hook["session_id"], project_tag(hook.get("cwd", "")), items)
    except working.WorkingSafetyError:
        return "refused-location"
    return status


def run_hook(raw: str) -> int:
    """The SessionEnd hook: cheap checks, start the detached worker, exit 0. Never raises."""
    try:
        if os.environ.get(ROOM_SPAWNED_ENV) == "1" or not is_enabled() or len(raw) > MAX_HOOK_INPUT:
            return 0
        hook = json.loads(raw)
        if not _valid_hook(hook):
            return 0
        payload = json.dumps({k: hook[k] for k in ("session_id", "transcript_path", "cwd") if k in hook})
        proc = subprocess.Popen(
            [sys.executable, *WORKER_ARGS],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(capture_dir()), start_new_session=True, close_fds=True,
        )
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.close()
    except Exception as exc:  # noqa: BLE001 - the hook must never fail Claude Code
        log_event(f"hook-error:{type(exc).__name__}")
    return 0


def run_worker(raw: str) -> int:
    """The detached worker: capture, log the status (no content). Never raises."""
    try:
        hook = json.loads(raw[:MAX_HOOK_INPUT])
        status = capture_claude(hook)
        project = project_tag(hook.get("cwd", "")) if isinstance(hook, dict) and isinstance(hook.get("cwd"), str) else ""
        log_event(status, project if status in ("created", "updated") else "")
    except Exception as exc:  # noqa: BLE001
        log_event(f"error:{type(exc).__name__}")
    return 0


# -- hook installer ---------------------------------------------------------------------------

class SettingsError(ValueError):
    pass


_OURS = re.compile(r"(?:^|/)room'?\s+capture\s+claude\s*$")


def room_executable() -> str:
    candidate = Path(sys.executable).parent / "room"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("room") or "room"


def hook_command(executable: str | None = None) -> str:
    return f"{shlex.quote(executable or room_executable())} capture claude"


def hook_config(executable: str | None = None) -> dict:
    return {"hooks": {HOOK_EVENT: [{"hooks": [
        {"type": "command", "command": hook_command(executable), "timeout": HOOK_TIMEOUT_S}]}]}}


def is_our_hook(hook: object) -> bool:
    return (isinstance(hook, dict) and hook.get("type") == "command"
            and isinstance(hook.get("command"), str) and bool(_OURS.search(hook["command"].strip())))


def _session_end_groups(settings: dict) -> list:
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError("'hooks' is not an object")
    groups = hooks.get(HOOK_EVENT, [])
    if not isinstance(groups, list) or not all(isinstance(g, dict) and isinstance(g.get("hooks", []), list) for g in groups):
        raise SettingsError(f"'hooks.{HOOK_EVENT}' is not a list of hook groups")
    return groups


def install(settings: dict) -> bool:
    groups = _session_end_groups(settings)
    if any(is_our_hook(h) for g in groups for h in g.get("hooks", [])):
        return False
    settings.setdefault("hooks", {}).setdefault(HOOK_EVENT, []).append(hook_config()["hooks"][HOOK_EVENT][0])
    return True


def uninstall(settings: dict) -> bool:
    groups = _session_end_groups(settings)
    changed = False
    for group in list(groups):
        kept = [h for h in group.get("hooks", []) if not is_our_hook(h)]
        if len(kept) != len(group.get("hooks", [])):
            changed = True
            if kept:
                group["hooks"] = kept
            else:
                groups.remove(group)
    if changed and not groups:
        del settings["hooks"][HOOK_EVENT]
        if not settings["hooks"]:
            del settings["hooks"]
    return changed


def _load(path: Path) -> tuple[dict | None, bytes | None]:
    if not path.exists():
        return None, None
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc.msg})") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{path} does not hold a JSON object")
    return data, raw


def change_settings(path: Path, action: str) -> str:
    """Merge (``install``) or remove (``uninstall``) only our hook. Backs up first; atomic write."""
    import tempfile

    path = Path(path).expanduser()
    data, raw = _load(path)
    settings = data if data is not None else {}
    changed = install(settings) if action == "install" else uninstall(settings)
    if not changed:
        return ("Hook already installed; nothing changed." if action == "install"
                else "Hook not installed; nothing to remove.")
    backup = None
    if raw is not None:
        if path.read_bytes() != raw:
            raise SettingsError(f"{path} changed while it was being edited; try again")
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
            json.dump(settings, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if raw is not None and path.read_bytes() != raw:
            raise SettingsError(f"{path} changed while it was being edited; try again")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    verb = "Installed" if action == "install" else "Removed"
    return f"{verb} the {HOOK_EVENT} hook in {path}" + (f" (backup: {backup.name})" if backup else "") + "."
