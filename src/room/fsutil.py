"""Small filesystem helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(target: Path, data: dict) -> None:
    """Write JSON to a temp file in the same directory, fsync, then rename over ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
