"""Read the system clipboard (macOS ``pbpaste``) without a new dependency."""

from __future__ import annotations

import shutil
import subprocess

MAX_CLIPBOARD_CHARS = 50_000


class ClipboardError(RuntimeError):
    pass


def read_clipboard(timeout: float = 5.0) -> str:
    if shutil.which("pbpaste") is None:
        raise ClipboardError("pbpaste not found; --clipboard needs macOS (paste into a file and use --file instead)")
    try:
        proc = subprocess.run(["pbpaste"], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(f"pbpaste did not finish within {timeout:.0f}s") from exc
    if proc.returncode != 0:
        raise ClipboardError(f"pbpaste failed with exit code {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace")
