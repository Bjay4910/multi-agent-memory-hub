"""Shared helpers for agents that shell out to a local CLI (claude, agy)."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import tempfile

from .base import SECRET_ENV_VARS

# CSI sequences (colors, cursor moves), OSC sequences (titles, hyperlinks), and lone escapes.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒"


def child_env() -> dict[str, str]:
    """The current environment minus the room's provider API keys, marked as room-spawned.

    ``ROOM_SPAWNED=1`` tells ``room capture`` hooks running inside the child to do
    nothing, so the room never captures its own CLI calls.
    """
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS}
    env["ROOM_SPAWNED"] = "1"
    return env


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def clean_output(text: str, noise_prefixes: tuple[str, ...] = ()) -> str:
    """Remove ANSI codes, carriage-return redraws, spinner lines and status-noise lines."""
    lines = []
    for raw in strip_ansi(text).replace("\r\n", "\n").split("\n"):
        line = raw.rsplit("\r", 1)[-1]  # keep only the last redraw of a \r-updated line
        stripped = line.strip()
        if stripped and all(ch in _SPINNER_CHARS or ch.isspace() for ch in stripped):
            continue
        if noise_prefixes and stripped.lower().startswith(noise_prefixes):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_cli(args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run ``args`` in an empty temp dir with no stdin and a hard timeout.

    The child gets its own process group (``start_new_session``) so that on
    timeout or cancellation the whole group, including any helpers it spawned,
    is killed with SIGKILL. Raises ``asyncio.TimeoutError`` on timeout.
    """
    with tempfile.TemporaryDirectory(prefix="agent-room-cli-") as cwd:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _kill_group(proc)
            await proc.wait()
            raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
