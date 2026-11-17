"""Recall: find memory relevant to a task and render it as a compact prompt block.

1. FTS5 BM25 search for the task text over *active* nodes (top-k, default 8).
2. Add 1-hop neighbours of those hits (active nodes only, history edges skipped).
3. Score = relevance x confidence weight x recency weight (neighbours inherit
   half of their parent's relevance).
4. Render the best nodes, then the links between them, until the token budget
   (default 800 tokens, estimated as 4 characters per token) is used up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .store import Edge, MemoryStore, Node

DEFAULT_TOP_K = 8
DEFAULT_TOKEN_BUDGET = 800
CHARS_PER_TOKEN = 4
RECENCY_HALF_LIFE_DAYS = 90.0
NEIGHBOUR_FACTOR = 0.5
SUMMARY_MAX_CHARS = 240
HEADER = "Relevant memory (may be outdated, verify):"


@dataclass(frozen=True)
class Scored:
    node: Node
    score: float
    via: Edge | None = None  # set for nodes added as neighbours


@dataclass(frozen=True)
class RecallResult:
    block: str
    nodes: list[Node]


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
        return value if value > 0 else default
    except ValueError:
        return default


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def node_weight(node: Node, now: datetime) -> float:
    try:
        age_days = max(0.0, (now - _parse_ts(node.updated_at)).total_seconds() / 86400)
    except ValueError:
        age_days = RECENCY_HALF_LIFE_DAYS
    recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return (0.5 + 0.5 * node.confidence) * (0.6 + 0.4 * recency)


def rank(store: MemoryStore, query: str, k: int = DEFAULT_TOP_K, now: datetime | None = None) -> list[Scored]:
    """Ranked active nodes for ``query``: BM25 hits plus their 1-hop neighbours."""
    now = now or datetime.now(timezone.utc)
    hits = store.search(query, k)
    if not hits:
        return []
    best = max(rel for _, rel in hits)
    scored: dict[int, Scored] = {}
    for node, rel in hits:
        relevance = 0.3 + 0.7 * (rel / best if best > 0 else 0.0)
        scored[node.id] = Scored(node, relevance * node_weight(node, now))
    hit_ids = set(scored)
    for neighbour, via_id, edge in store.neighbours(list(hit_ids)):
        if neighbour.id in hit_ids:
            continue
        score = NEIGHBOUR_FACTOR * scored[via_id].score * node_weight(neighbour, now)
        if neighbour.id not in scored or scored[neighbour.id].score < score:
            scored[neighbour.id] = Scored(neighbour, score, edge)
    return sorted(scored.values(), key=lambda s: (s.score, s.node.updated_at), reverse=True)


def _node_line(node: Node) -> str:
    summary = " ".join(node.summary.split())
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 3] + "..."
    date = node.updated_at[:10]
    return f"- {node.name} ({node.type}, {date})" + (f": {summary}" if summary else "")


def render(store: MemoryStore, ranked: list[Scored], token_budget: int = DEFAULT_TOKEN_BUDGET) -> RecallResult:
    max_chars = token_budget * CHARS_PER_TOKEN
    lines = [HEADER]
    used = len(HEADER)
    included: list[Node] = []
    for item in ranked:
        line = _node_line(item.node)
        if used + 1 + len(line) > max_chars:
            break
        lines.append(line)
        used += 1 + len(line)
        included.append(item.node)
    if not included:
        return RecallResult("", [])
    ids = {n.id: n for n in included}
    seen: set[int] = set()
    links = []
    for n in included:
        for edge, other in store.edges_of(n.id):
            if edge.id in seen or other.id not in ids or edge.relation == "supersedes":
                continue
            seen.add(edge.id)
            links.append(f"- {ids[edge.src].name} {edge.relation} {ids[edge.dst].name}")
    if links and used + len("\nLinks:") <= max_chars:
        lines.append("Links:")
        used += len("\nLinks:")
        for link in links:
            if used + 1 + len(link) > max_chars:
                break
            lines.append(link)
            used += 1 + len(link)
    return RecallResult("\n".join(lines), included)


def recall(store: MemoryStore, task: str, k: int | None = None, token_budget: int | None = None, now: datetime | None = None) -> RecallResult:
    k = k or _env_int("MEMORY_TOP_K", DEFAULT_TOP_K)
    token_budget = token_budget or _env_int("MEMORY_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET)
    return render(store, rank(store, task, k, now), token_budget)
