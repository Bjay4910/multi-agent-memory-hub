"""SQLite graph store for memory: nodes, edges, provenance and full-text search.

One file, ``~/.agent-room/memory.db``, in WAL mode. Every write runs inside a
single ``BEGIN IMMEDIATE`` transaction, so a batch of changes is applied
completely or not at all. See docs/MEMORY.md for the schema contract.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .paths import ensure_outside_repo, memory_db_path

NODE_TYPES = (
    "person", "organization", "project", "decision", "fact",
    "task", "document", "preference", "application", "skill",
)
RELATIONS = ("relates_to", "part_of", "decided_in", "supersedes", "mentions", "works_on", "applied_to", "requires")
# For these types a newer, different summary replaces the old node (which is
# kept as 'superseded'). For all other types summaries are merged.
STATEFUL_TYPES = frozenset({"fact", "decision", "preference", "application", "task"})
SUMMARY_MAX_CHARS = 1200

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
        CREATE TABLE nodes (
            id          INTEGER PRIMARY KEY,
            type        TEXT NOT NULL,
            name        TEXT NOT NULL,
            norm_name   TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            aliases     TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
            confidence  REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX nodes_active_key ON nodes(type, norm_name) WHERE status = 'active';
        CREATE INDEX nodes_norm_name ON nodes(norm_name);
        CREATE TABLE edges (
            id          INTEGER PRIMARY KEY,
            src         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            dst         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            relation    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE (src, dst, relation)
        );
        CREATE INDEX edges_dst ON edges(dst);
        CREATE TABLE provenance (
            id          INTEGER PRIMARY KEY,
            node_id     INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
            edge_id     INTEGER REFERENCES edges(id) ON DELETE CASCADE,
            session_id  TEXT NOT NULL,
            agent       TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'session',
            ts          TEXT NOT NULL,
            CHECK ((node_id IS NULL) <> (edge_id IS NULL))
        );
        CREATE INDEX provenance_node ON provenance(node_id);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """),
    (2, """
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            name, summary, aliases, content='nodes', content_rowid='id', tokenize='porter unicode61'
        );
        CREATE TRIGGER nodes_fts_ai AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, name, summary, aliases) VALUES (new.id, new.name, new.summary, new.aliases);
        END;
        CREATE TRIGGER nodes_fts_ad AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, name, summary, aliases)
            VALUES ('delete', old.id, old.name, old.summary, old.aliases);
        END;
        CREATE TRIGGER nodes_fts_au AFTER UPDATE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, name, summary, aliases)
            VALUES ('delete', old.id, old.name, old.summary, old.aliases);
            INSERT INTO nodes_fts(rowid, name, summary, aliases) VALUES (new.id, new.name, new.summary, new.aliases);
        END;
        INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild');
    """),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]

STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have how i i'm im in is it its "
    "me my of on or our should so than that the their them then there these they this to was we were what "
    "when where which who whom why will with would you your about into over any all also just more most "
    "tell know say said want".split()
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).lower()
    text = re.sub(r"[\W_]+", " ", text).strip()
    text = re.sub(r"^the ", "", text)
    return re.sub(r"\s+", " ", text)


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\W_]+", " ", text.lower())).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def merge_summaries(old: str, new: str) -> str:
    """Append the sentences of ``new`` that ``old`` does not already contain."""
    merged = old.strip()
    seen = _norm_text(merged)
    for sentence in _sentences(new):
        if _norm_text(sentence) and _norm_text(sentence) not in seen:
            merged = f"{merged} {sentence}".strip()
            seen = _norm_text(merged)
    return merged[:SUMMARY_MAX_CHARS]


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: quoted terms joined with OR."""
    terms = []
    for tok in re.findall(r"\w+", text.lower()):
        if len(tok) < 2 or tok in STOPWORDS or tok in terms:
            continue
        terms.append(tok)
    return " OR ".join(f'"{t}"' for t in terms)


@dataclass(frozen=True)
class Node:
    id: int
    type: str
    name: str
    summary: str
    aliases: str
    status: str
    confidence: float
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Node":
        return cls(*(row[k] for k in ("id", "type", "name", "summary", "aliases", "status", "confidence", "created_at", "updated_at")))


@dataclass(frozen=True)
class Edge:
    id: int
    src: int
    dst: int
    relation: str
    created_at: str


@dataclass(frozen=True)
class Provenance:
    session_id: str
    agent: str
    source: str = "session"  # 'session', 'manual', 'clipboard', 'mcp-desktop' or 'claude-code'


class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = ensure_outside_repo(Path(path) if path else memory_db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        self.conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        if new_file:
            os.chmod(self.path, 0o600)  # personal data: owner-only
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.migrate()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def schema_version(self) -> int:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        return row[0] if row else 0

    def migrate(self, target: int | None = None) -> int:
        target = MIGRATIONS[-1][0] if target is None else target
        current = self.schema_version()
        for version, sql in MIGRATIONS:
            if current < version <= target:
                with self.tx() as c:
                    for statement in _split_sql(sql):
                        c.execute(statement)
                    c.execute("DELETE FROM schema_version")
                    c.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                current = version
        return current

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    @property
    def is_demo(self) -> bool:
        return self.get_meta("demo") == "1"

    # -- writes ------------------------------------------------------------

    def upsert_node(self, type: str, name: str, summary: str, confidence: float, prov: Provenance, ts: str | None = None) -> tuple[int, str]:
        with self.tx():
            return self._upsert_node(type, name, summary, confidence, prov, ts or utc_now())

    def add_edge(self, src: int, dst: int, relation: str, prov: Provenance, ts: str | None = None) -> int | None:
        with self.tx():
            return self._add_edge(src, dst, relation, prov, ts or utc_now())

    def apply_extraction(self, data: dict, prov: Provenance, ts: str | None = None) -> dict[str, int]:
        """Store a validated extraction ({"nodes": [...], "edges": [...]}) in one transaction."""
        ts = ts or utc_now()
        counts = {"created": 0, "merged": 0, "superseded": 0, "edges": 0, "edges_skipped": 0}
        with self.tx():
            ids: dict[str, int] = {}
            for n in data.get("nodes", []):
                node_id, action = self._upsert_node(n["type"], n["name"], n.get("summary", ""), float(n.get("confidence", 0.5)), prov, ts)
                ids[normalize_name(n["name"])] = node_id
                counts[action] += 1
            for e in data.get("edges", []):
                src = ids.get(normalize_name(e["src"])) or self._find_active_id(e["src"])
                dst = ids.get(normalize_name(e["dst"])) or self._find_active_id(e["dst"])
                if src is None or dst is None or src == dst:
                    counts["edges_skipped"] += 1
                    continue
                if self._add_edge(src, dst, e["relation"], prov, ts) is not None:
                    counts["edges"] += 1
        return counts

    def _upsert_node(self, type: str, name: str, summary: str, confidence: float, prov: Provenance, ts: str) -> tuple[int, str]:
        if type not in NODE_TYPES:
            raise ValueError(f"unknown node type {type!r}")
        name = name.strip()
        norm = normalize_name(name)
        if not norm:
            raise ValueError("node name is empty")
        summary = (summary or "").strip()[:SUMMARY_MAX_CHARS]
        confidence = min(1.0, max(0.0, confidence))
        c = self.conn
        row = c.execute("SELECT * FROM nodes WHERE type = ? AND norm_name = ? AND status = 'active'", (type, norm)).fetchone()
        if row is None:
            node_id = self._insert_node(type, name, norm, summary, confidence, ts)
            action = "created"
        elif type in STATEFUL_TYPES and summary and _norm_text(summary) not in _norm_text(row["summary"]):
            # Newer, different information about the same thing: keep history, retire the old node.
            c.execute("UPDATE nodes SET status = 'superseded', updated_at = ? WHERE id = ?", (ts, row["id"]))
            node_id = self._insert_node(type, name, norm, summary, confidence, ts)
            self._carry_edges(row["id"], node_id, ts)
            self._add_edge(node_id, row["id"], "supersedes", prov, ts)
            action = "superseded"
        else:
            node_id = row["id"]
            aliases = [a for a in row["aliases"].split("|") if a]
            if name != row["name"] and name not in aliases:
                aliases.append(name)
            c.execute(
                "UPDATE nodes SET summary = ?, aliases = ?, confidence = ?, updated_at = ? WHERE id = ?",
                (merge_summaries(row["summary"], summary), "|".join(aliases), max(confidence, row["confidence"]), ts, node_id),
            )
            action = "merged"
        c.execute(
            "INSERT INTO provenance (node_id, session_id, agent, source, ts) VALUES (?, ?, ?, ?, ?)",
            (node_id, prov.session_id, prov.agent, prov.source, ts),
        )
        return node_id, action

    def _insert_node(self, type: str, name: str, norm: str, summary: str, confidence: float, ts: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO nodes (type, name, norm_name, summary, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (type, name, norm, summary, confidence, ts, ts),
        )
        return int(cur.lastrowid)

    def _carry_edges(self, old_id: int, new_id: int, ts: str) -> None:
        """Give the replacement node the old node's relationships (history edges excluded)."""
        c = self.conn
        c.execute(
            "INSERT OR IGNORE INTO edges (src, dst, relation, created_at) "
            "SELECT ?, dst, relation, ? FROM edges WHERE src = ? AND relation <> 'supersedes' AND dst <> ?",
            (new_id, ts, old_id, new_id),
        )
        c.execute(
            "INSERT OR IGNORE INTO edges (src, dst, relation, created_at) "
            "SELECT src, ?, relation, ? FROM edges WHERE dst = ? AND relation <> 'supersedes' AND src <> ?",
            (new_id, ts, old_id, new_id),
        )

    def _add_edge(self, src: int, dst: int, relation: str, prov: Provenance, ts: str) -> int | None:
        if relation not in RELATIONS:
            raise ValueError(f"unknown relation {relation!r}")
        c = self.conn
        c.execute("INSERT OR IGNORE INTO edges (src, dst, relation, created_at) VALUES (?, ?, ?, ?)", (src, dst, relation, ts))
        row = c.execute("SELECT id FROM edges WHERE src = ? AND dst = ? AND relation = ?", (src, dst, relation)).fetchone()
        if row is None:
            return None
        if relation == "supersedes":
            c.execute("UPDATE nodes SET status = 'superseded', updated_at = ? WHERE id = ? AND status = 'active'", (ts, dst))
        c.execute(
            "INSERT INTO provenance (edge_id, session_id, agent, source, ts) VALUES (?, ?, ?, ?, ?)",
            (row["id"], prov.session_id, prov.agent, prov.source, ts),
        )
        return int(row["id"])

    def _find_active_id(self, name: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM nodes WHERE norm_name = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
            (normalize_name(name),),
        ).fetchone()
        return int(row["id"]) if row else None

    def forget(self, node_id: int) -> bool:
        """Delete a node, its edges and its provenance."""
        with self.tx() as c:
            return c.execute("DELETE FROM nodes WHERE id = ?", (node_id,)).rowcount > 0

    # -- reads -------------------------------------------------------------

    def search(self, query: str, k: int = 8, include_superseded: bool = False) -> list[tuple[Node, float]]:
        """BM25 full-text search. Returns (node, relevance) with higher relevance = better."""
        q = fts_query(query)
        if not q:
            return []
        status = "" if include_superseded else "AND n.status = 'active'"
        rows = self.conn.execute(
            f"SELECT n.*, bm25(nodes_fts, 10.0, 2.0, 6.0) AS rank FROM nodes_fts "
            f"JOIN nodes n ON n.id = nodes_fts.rowid WHERE nodes_fts MATCH ? {status} ORDER BY rank LIMIT ?",
            (q, k),
        ).fetchall()
        return [(Node.from_row(r), -float(r["rank"])) for r in rows]

    def get_node(self, node_id: int) -> Node | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return Node.from_row(row) if row else None

    def find_nodes(self, name: str) -> list[Node]:
        """All nodes (any type or status) with this normalized name, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE norm_name = ? ORDER BY status = 'active' DESC, updated_at DESC, id DESC",
            (normalize_name(name),),
        ).fetchall()
        return [Node.from_row(r) for r in rows]

    def edges_of(self, node_id: int) -> list[tuple[Edge, Node]]:
        """Edges touching ``node_id`` together with the node on the other end."""
        rows = self.conn.execute(
            "SELECT e.*, CASE WHEN e.src = ? THEN e.dst ELSE e.src END AS other FROM edges e WHERE e.src = ? OR e.dst = ? ORDER BY e.id",
            (node_id, node_id, node_id),
        ).fetchall()
        out = []
        for r in rows:
            other = self.get_node(r["other"])
            if other:
                out.append((Edge(r["id"], r["src"], r["dst"], r["relation"], r["created_at"]), other))
        return out

    def neighbours(self, node_ids: list[int]) -> list[tuple[Node, int, Edge]]:
        """Active 1-hop neighbours of ``node_ids``: (neighbour, via node id, edge). Skips history edges."""
        out = []
        for nid in node_ids:
            for edge, other in self.edges_of(nid):
                if edge.relation != "supersedes" and other.status == "active":
                    out.append((other, nid, edge))
        return out

    def provenance(self, node_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT session_id, agent, source, ts FROM provenance WHERE node_id = ? ORDER BY ts, id", (node_id,)).fetchall()

    def all_nodes(self, include_superseded: bool = False) -> list[Node]:
        where = "" if include_superseded else "WHERE status = 'active'"
        return [Node.from_row(r) for r in self.conn.execute(f"SELECT * FROM nodes {where} ORDER BY type, name, id").fetchall()]

    def all_edges(self, include_superseded: bool = False) -> list[Edge]:
        sql = "SELECT e.* FROM edges e"
        if not include_superseded:
            sql += (" JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst"
                    " WHERE s.status = 'active' AND d.status = 'active'")
        return [Edge(r["id"], r["src"], r["dst"], r["relation"], r["created_at"]) for r in self.conn.execute(sql + " ORDER BY e.id").fetchall()]

    def stats(self) -> dict:
        c = self.conn
        by_type = {r[0]: r[1] for r in c.execute("SELECT type, COUNT(*) FROM nodes WHERE status = 'active' GROUP BY type ORDER BY type")}
        by_relation = {r[0]: r[1] for r in c.execute("SELECT relation, COUNT(*) FROM edges GROUP BY relation ORDER BY relation")}
        return {
            "nodes_active": c.execute("SELECT COUNT(*) FROM nodes WHERE status = 'active'").fetchone()[0],
            "nodes_superseded": c.execute("SELECT COUNT(*) FROM nodes WHERE status = 'superseded'").fetchone()[0],
            "edges": c.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "sessions": c.execute("SELECT COUNT(DISTINCT session_id) FROM provenance").fetchone()[0],
            "manual_notes": c.execute("SELECT COUNT(DISTINCT session_id) FROM provenance WHERE source = 'manual'").fetchone()[0],
            "by_type": by_type,
            "by_relation": by_relation,
            "schema_version": self.schema_version(),
        }


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies (BEGIN ... END;) intact."""
    statements, buf, in_trigger = [], [], False
    for line in script.strip().splitlines():
        buf.append(line)
        stripped = line.strip().upper()
        if stripped.startswith("CREATE TRIGGER"):
            in_trigger = True
        if (in_trigger and stripped == "END;") or (not in_trigger and stripped.endswith(";")):
            statements.append("\n".join(buf).strip())
            buf, in_trigger = [], False
    if "".join(buf).strip():
        statements.append("\n".join(buf).strip())
    return statements
