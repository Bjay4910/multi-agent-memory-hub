"""Export memory as a folder of markdown notes (an Obsidian-style vault).

One note per node, in a folder per node type, plus ``index.md``. Notes carry
YAML frontmatter and a "Related" section with ``[[wikilinks]]`` for each edge.

Safety rules:
- The vault defaults to ``~/.agent-room/vault`` and is created with mode 700;
  notes are written with mode 600.
- A vault inside any git work tree (including this repository) is refused
  unless the caller explicitly allows it.
- Every note goes through :func:`redact_secrets`; a note that still matches a
  secret pattern afterwards is not written.
- Re-running is idempotent. The exporter only ever changes or deletes files it
  wrote itself (listed in ``.room-vault.json``); other files in the folder are left alone.

No network calls and no model calls.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from .paths import ensure_outside_repo, memory_db_path
from .redact import find_secrets, redact_secrets
from .store import MemoryStore, Node

MANIFEST = ".room-vault.json"
INDEX = "index.md"
FOLDERS = {
    "person": "people", "organization": "organizations", "project": "projects", "decision": "decisions",
    "fact": "facts", "task": "tasks", "document": "documents", "preference": "preferences",
    "application": "applications", "skill": "skills",
}
# "<slug>-<id>" stays under 32 characters, the shortest run that redaction may treat
# as a random token, so links and filenames are never mangled by it.
STEM_MAX = 31


class VaultSafetyError(RuntimeError):
    """The vault location is not safe to write to."""


@dataclass
class ExportResult:
    vault: Path
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (relative path, reason)

    @property
    def written(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged)


def default_vault() -> Path:
    return paths.room_home() / "vault"


def git_work_tree(path: Path) -> Path | None:
    """The git work tree containing ``path`` (existing or not), if any."""
    p = Path(path).expanduser().resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def check_vault_location(vault: Path, allow_repo: bool = False) -> Path:
    vault = Path(vault).expanduser().resolve()
    keys_dir = (Path.home() / ".config" / "agent-keys").resolve()
    if vault == keys_dir or keys_dir in vault.parents or vault in keys_dir.parents:
        raise VaultSafetyError(f"refusing to use {vault} as a vault: it overlaps the key directory")
    if not allow_repo:
        tree = git_work_tree(vault)
        if tree is not None:
            raise VaultSafetyError(
                f"refusing to export into a git work tree ({tree}); personal memory could be committed. "
                "Choose a folder outside any repository, or pass --i-know-this-is-a-repo."
            )
        ensure_outside_repo(vault)  # also covers a checkout whose .git is elsewhere
    return vault


def slugify(name: str, max_len: int = STEM_MAX - 2) -> str:
    """Lowercase ASCII words joined by '-', cut at a word boundary to ``max_len``."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[: max_len + 1]
        slug = cut[: cut.rfind("-")] if "-" in cut[1:] else slug[:max_len]
    return slug.strip("-") or "node"


def note_path(node: Node) -> str:
    """Deterministic path relative to the vault: ``<folder>/<slug>-<id>.md``."""
    slug = slugify(redact_secrets(node.name), STEM_MAX - len(str(node.id)) - 1)
    return f"{FOLDERS.get(node.type, 'other')}/{slug}-{node.id}.md"


def _q(value: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def _link(node: Node) -> str:
    target = note_path(node)[: -len(".md")]
    label = " ".join(redact_secrets(node.name).split()).replace("|", "/").replace("[", "(").replace("]", ")")
    return f"[[{target}|{label}]]"


def render_note(store: MemoryStore, node: Node, included: dict[int, Node]) -> str:
    sources = []
    for p in store.provenance(node.id):
        entry = f"{p['source']}:{p['session_id']}"
        if entry not in sources:
            sources.append(entry)
    aliases = [node.name] + [a for a in node.aliases.split("|") if a]
    lines = [
        "---",
        f"id: {node.id}",
        f"type: {node.type}",
        f"name: {_q(node.name)}",
        f"created: {_q(node.created_at)}",
        f"updated: {_q(node.updated_at)}",
        f"status: {node.status}",
        f"confidence: {node.confidence:.2f}",
        "aliases: [" + ", ".join(_q(a) for a in aliases) + "]",
        "sources:" + ("" if sources else " []"),
        *(f"  - {_q(s)}" for s in sources),
        "---",
        "",
        f"# {node.name}",
        "",
    ]
    if node.status == "superseded":
        lines += ["> [!warning] SUPERSEDED: this is outdated history, kept for reference. "
                  "Do not treat it as current.", ""]
    lines += [node.summary.strip() or "_No summary._", "", "## Related", ""]
    related = []
    for edge, other in store.edges_of(node.id):
        if other.id not in included:
            continue
        mark = " (superseded)" if other.status == "superseded" else ""
        if edge.src == node.id:
            related.append(f"- {edge.relation} {_link(other)}{mark}")
        else:
            related.append(f"- {_link(other)}{mark} {edge.relation} this")
    lines += related or ["_None._"]
    return "\n".join(lines) + "\n"


def render_index(nodes: list[Node]) -> str:
    lines = ["# Memory vault", "",
             "Exported by `room memory export`. Personal data: keep this folder private and local.", ""]
    active = [n for n in nodes if n.status == "active"]
    old = [n for n in nodes if n.status != "active"]
    for type_, folder in FOLDERS.items():
        group = [n for n in active if n.type == type_]
        if group:
            lines += [f"## {folder.capitalize()}", "", *(f"- {_link(n)}" for n in group), ""]
    if old:
        lines += ["## Superseded (history, not current)", "",
                  *(f"- {_link(n)} ({n.type})" for n in old), ""]
    if not nodes:
        lines += ["_Memory is empty._", ""]
    return "\n".join(lines)


def _read_manifest(vault: Path) -> list[str]:
    try:
        data = json.loads((vault / MANIFEST).read_text(encoding="utf-8"))
        return [f for f in data.get("files", []) if isinstance(f, str)]
    except (OSError, ValueError, AttributeError):
        return []


def _safe_rel(vault: Path, rel: str) -> Path | None:
    """``vault/rel`` if it stays inside the vault (manifest entries are not trusted blindly)."""
    target = (vault / rel).resolve()
    return target if vault in target.parents else None


def _write_private(target: Path, text: str) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def export_vault(vault: Path | None = None, include_superseded: bool = False, allow_repo: bool = False,
                 store_path: Path | None = None) -> ExportResult:
    vault = check_vault_location(vault or default_vault(), allow_repo)
    vault.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(vault, 0o700)
    result = ExportResult(vault)

    db = Path(store_path) if store_path else memory_db_path()
    notes: dict[str, str] = {}
    if db.exists():  # no database (never created, or purged): export nothing, remove old notes
        with MemoryStore(db) as store:
            nodes = store.all_nodes(include_superseded)
            included = {n.id: n for n in nodes}
            for n in nodes:
                notes[note_path(n)] = render_note(store, n, included)
            notes[INDEX] = render_index(nodes)
    else:
        notes[INDEX] = render_index([])

    previous = set(_read_manifest(vault))
    managed: list[str] = []
    for rel, text in sorted(notes.items()):
        text = redact_secrets(text)
        leftover = find_secrets(text)
        if leftover:
            result.refused.append((rel, "still matches a secret pattern after redaction"))
            continue
        target = vault / rel
        if target.exists() and rel not in previous:
            result.refused.append((rel, "a file with this name exists and was not written by room"))
            continue
        managed.append(rel)
        if target.exists():
            if target.read_text(encoding="utf-8") == text:
                result.unchanged.append(rel)
                continue
            result.updated.append(rel)
        else:
            result.created.append(rel)
        _write_private(target, text)

    keep = set(managed)  # an earlier copy of a now-refused note is removed, not left stale
    for rel in sorted(previous - keep):
        target = _safe_rel(vault, rel)
        if target is None:
            continue
        if target.exists():
            target.unlink()
            result.removed.append(rel)
        parent = target.parent
        if parent != vault and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    manifest = {"version": 1, "files": sorted(keep)}
    _write_private(vault / MANIFEST, json.dumps(manifest, indent=1) + "\n")
    return result
