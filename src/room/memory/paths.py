"""Where memory data lives, and a guard that keeps it out of the repository.

All personal memory data lives under ``~/.agent-room`` (or ``$AGENT_ROOM_HOME``).
Every memory write goes through :func:`ensure_outside_repo`, which refuses any
path inside this project's git checkout.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths


class RepoWriteError(RuntimeError):
    """Raised when memory data would be written inside the repository."""


def project_root() -> Path | None:
    """The git checkout this package is running from, if any (editable installs)."""
    root = Path(__file__).resolve().parents[3]
    if (root / ".git").exists() and (root / "pyproject.toml").exists():
        return root
    return None


def is_inside_repo(path: Path) -> bool:
    root = project_root()
    if root is None:
        return False
    resolved = Path(path).expanduser().resolve()
    return resolved == root or root in resolved.parents


def ensure_outside_repo(path: Path) -> Path:
    path = Path(path).expanduser()
    if is_inside_repo(path):
        raise RepoWriteError(
            f"refusing to write memory data inside the repository ({path}); "
            "use ~/.agent-room or set AGENT_ROOM_HOME to a directory outside the repo"
        )
    return path


def memory_db_path() -> Path:
    return paths.room_home() / "memory.db"


def queue_dir() -> Path:
    return paths.room_home() / "memory_queue"


def briefs_dir() -> Path:
    return paths.room_home() / "briefs"


def exports_dir() -> Path:
    return paths.room_home() / "exports"
