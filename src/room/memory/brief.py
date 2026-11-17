"""Context brief: a clean markdown summary of memory to paste into any chat app."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .paths import briefs_dir, ensure_outside_repo
from .store import MemoryStore, Node

PURPOSES = ("applications",)


def _line(node: Node, bold: bool = False) -> str:
    name = f"**{node.name}**" if bold else node.name
    summary = " ".join(node.summary.split())
    return f"- {name}: {summary}" if summary else f"- {name}"


def build_brief(store: MemoryStore, purpose: str = "applications", now: datetime | None = None) -> str:
    """Markdown brief from active memory. Contains no internal ids."""
    now = now or datetime.now(timezone.utc)
    nodes = store.all_nodes()
    by_type: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        by_type[n.type].append(n)
    by_id = {n.id: n for n in nodes}

    def related(node: Node, types: tuple[str, ...]) -> list[str]:
        names = []
        for edge, other in store.edges_of(node.id):
            if edge.relation != "supersedes" and other.id in by_id and other.type in types and other.name not in names:
                names.append(other.name)
        return names

    out = [
        f"# Context brief: {purpose}",
        "",
        f"_Generated {now.date().isoformat()} from my local memory. Some facts may be outdated; "
        "ask me to confirm anything important._",
    ]

    def section(title: str, lines: list[str]) -> None:
        if lines:
            out.extend(["", f"## {title}", "", *lines])

    section("Profile", [_line(n) for n in by_type["person"]])
    section("Skills", [_line(n) for n in by_type["skill"]])

    projects = []
    for p in by_type["project"]:
        projects.append(_line(p, bold=True))
        uses = related(p, ("skill",))
        if uses:
            projects.append(f"  - Uses: {', '.join(uses)}")
    section("Projects and results", projects)
    section("Decisions", [_line(n) for n in by_type["decision"]])

    groups: dict[str, list[Node]] = defaultdict(list)
    for app in by_type["application"]:
        orgs = related(app, ("organization",))
        groups[orgs[0] if orgs else "Other"].append(app)
    apps = []
    for org in sorted(groups, key=lambda o: (o == "Other", o.lower())):
        apps.extend(["", f"### {org}"] if apps else [f"### {org}"])
        apps.extend(_line(a) for a in groups[org])
    section("Applications (status per company or program)", apps)

    section("Open tasks", [_line(n) for n in by_type["task"]])
    section("Preferences", [_line(n) for n in by_type["preference"]])
    section("Key facts", [_line(n) for n in by_type["fact"]])
    section("Documents", [_line(n) for n in by_type["document"]])
    if len(out) == 3:
        out.extend(["", "_No memory recorded yet. Use `room remember` or `room ask` to add some._"])
    return "\n".join(out) + "\n"


def write_brief(store: MemoryStore, purpose: str = "applications", out: Path | None = None) -> Path:
    path = Path(out).expanduser() if out else briefs_dir() / f"{purpose}-{datetime.now(timezone.utc):%Y-%m-%d}.md"
    ensure_outside_repo(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_brief(store, purpose), encoding="utf-8")
    os.chmod(path, 0o600)
    return path
