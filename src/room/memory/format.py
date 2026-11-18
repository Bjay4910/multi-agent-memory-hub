"""Plain-text rendering of search results and nodes, shared by the CLI and the MCP server."""

from __future__ import annotations

from .store import MemoryStore, Node


def short(text: str, limit: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_search(results: list[tuple[Node, float]]) -> str:
    """The text `room memory search` prints."""
    if not results:
        return "No matches."
    lines = []
    for node, _score in results:
        flag = "" if node.status == "active" else " (superseded)"
        lines.append(f"#{node.id:<4} {node.type:<12} {node.name}{flag}\n       {short(node.summary)}")
    return "\n".join(lines)


def format_node(store: MemoryStore, node: Node) -> str:
    """The text `room memory show` prints for one node."""
    lines = [f"#{node.id} {node.name}  [{node.type}, {node.status}, confidence {node.confidence:.2f}]"]
    if node.summary:
        lines.append(f"  {node.summary}")
    if node.aliases:
        lines.append(f"  aliases: {node.aliases.replace('|', ', ')}")
    lines.append(f"  created {node.created_at}, updated {node.updated_at}")
    for edge, other in store.edges_of(node.id):
        arrow = f"{edge.relation} -> {other.name}" if edge.src == node.id else f"<- {edge.relation} {other.name}"
        note = "" if other.status == "active" else ", superseded"
        lines.append(f"  link: {arrow} (#{other.id}{note})")
    for p in store.provenance(node.id):
        lines.append(f"  source: {p['source']} {p['session_id']} via {p['agent']} at {p['ts']}")
    return "\n".join(lines)
