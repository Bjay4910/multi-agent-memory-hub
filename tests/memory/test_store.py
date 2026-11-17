import sqlite3

import pytest

from room.memory.paths import RepoWriteError, project_root
from room.memory.store import (
    SCHEMA_VERSION,
    MemoryStore,
    Provenance,
    fts_query,
    merge_summaries,
    normalize_name,
)

P = Provenance("s1", "hermes")


@pytest.fixture
def store(isolated_home):
    s = MemoryStore()
    yield s
    s.close()


def node(type, name, summary="", confidence=0.8):
    return {"type": type, "name": name, "summary": summary, "confidence": confidence}


def test_schema_created_with_wal_and_version(store, isolated_home):
    assert store.path == isolated_home / "memory.db"
    assert store.schema_version() == SCHEMA_VERSION == 2
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master")}
    assert {"nodes", "edges", "provenance", "schema_version", "nodes_fts", "meta"} <= tables
    assert oct(store.path.stat().st_mode)[-3:] == "600"


def test_reopen_is_idempotent(isolated_home):
    MemoryStore().close()
    with MemoryStore() as s:
        assert s.schema_version() == SCHEMA_VERSION
        assert s.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_migration_from_v1_indexes_existing_rows(isolated_home):
    s = MemoryStore.__new__(MemoryStore)
    # open a v1-only database by hand
    path = isolated_home / "memory.db"
    path.parent.mkdir(parents=True)
    s.path, s.conn = path, sqlite3.connect(path, isolation_level=None)
    s.conn.row_factory = sqlite3.Row
    assert s.migrate(target=1) == 1
    s.conn.execute("INSERT INTO nodes (type, name, norm_name, summary, created_at, updated_at) VALUES ('skill','Python','python','Writes Python','t','t')")
    s.conn.close()
    with MemoryStore() as upgraded:
        assert upgraded.schema_version() == 2
        assert [n.name for n, _ in upgraded.search("python")] == ["Python"]


def test_failed_migration_rolls_back(isolated_home, monkeypatch):
    import room.memory.store as mod

    monkeypatch.setattr(mod, "MIGRATIONS", mod.MIGRATIONS + [(3, "CREATE TABLE ok_table (x INT);\nTHIS IS NOT SQL;")])
    with pytest.raises(sqlite3.OperationalError):
        MemoryStore()
    with sqlite3.connect(isolated_home / "memory.db") as c:
        assert c.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert not c.execute("SELECT name FROM sqlite_master WHERE name = 'ok_table'").fetchone()


def test_normalize_and_fts_query():
    assert normalize_name("  The Agent-Room!! ") == "agent room"
    assert fts_query("What is my 'graph' database? the") == '"graph" OR "database"'
    assert fts_query("the of and") == ""
    assert merge_summaries("Uses SQLite.", "Uses SQLite. Has FTS5.") == "Uses SQLite. Has FTS5."


def test_fts_search_ranks_name_matches_first(store):
    store.apply_extraction({"nodes": [
        node("project", "Power Manager", "Keeps the laptop awake while agents work."),
        node("skill", "Python", "Main language for the power manager scripts."),
        node("organization", "TU Munich", "Technical university in Germany."),
    ], "edges": []}, P)
    results = store.search("power manager")
    assert [n.name for n, _ in results][:2] == ["Power Manager", "Python"]
    assert all(score > 0 for _, score in results)
    assert store.search("nonexistentword") == []
    store.search('quote " and OR NEAR( * ^')  # odd input must never raise


def test_dedupe_merges_summaries_and_aliases(store):
    a, act1 = store.upsert_node("project", "Agent Room", "A multi-agent room.", 0.6, P)
    b, act2 = store.upsert_node("project", "the agent-room", "A multi-agent room. Uses SQLite.", 0.9, Provenance("s2", "claude"))
    assert a == b and (act1, act2) == ("created", "merged")
    n = store.get_node(a)
    assert n.summary == "A multi-agent room. Uses SQLite."
    assert n.confidence == 0.9 and n.aliases == "the agent-room"
    assert len(store.provenance(a)) == 2
    assert store.stats()["nodes_active"] == 1


def test_same_name_different_type_is_not_merged(store):
    store.upsert_node("project", "Graph", "a project", 0.5, P)
    store.upsert_node("skill", "Graph", "a skill", 0.5, P)
    assert store.stats()["nodes_active"] == 2


def test_newer_fact_supersedes_older(store):
    old, _ = store.upsert_node("fact", "Thesis deadline", "Thesis is due on 1 March.", 0.9, P)
    org, _ = store.upsert_node("organization", "Uni", "", 0.9, P)
    store.add_edge(old, org, "relates_to", P)
    new, action = store.upsert_node("fact", "Thesis deadline", "Thesis is due on 15 April.", 0.9, Provenance("s2", "hermes"))
    assert action == "superseded" and new != old
    assert store.get_node(old).status == "superseded"
    rels = {(e.relation, o.id) for e, o in store.edges_of(new)}
    assert ("supersedes", old) in rels and ("relates_to", org) in rels  # history edge + carried relation
    names = [n.summary for n, _ in store.search("thesis deadline")]
    assert names == ["Thesis is due on 15 April."]
    assert len(store.search("thesis deadline", include_superseded=True)) == 2


def test_repeating_same_fact_merges(store):
    a, _ = store.upsert_node("fact", "Thesis deadline", "Thesis is due on 1 March.", 0.9, P)
    b, action = store.upsert_node("fact", "thesis deadline", "Thesis is due on 1 March.", 0.9, P)
    assert a == b and action == "merged"


def test_explicit_supersedes_edge(store):
    data = {"nodes": [node("decision", "Use SQLite"), node("decision", "Use Neo4j")],
            "edges": [{"src": "Use SQLite", "relation": "supersedes", "dst": "Use Neo4j"}]}
    store.apply_extraction(data, P)
    assert [n.name for n in store.all_nodes()] == ["Use SQLite"]


def test_edges_dedupe_and_resolve_existing_nodes(store):
    store.upsert_node("organization", "TU Munich", "", 0.9, P)
    data = {"nodes": [node("application", "TU Munich MSc", "Submitted.")],
            "edges": [{"src": "TU Munich MSc", "relation": "applied_to", "dst": "tu munich"},
                      {"src": "TU Munich MSc", "relation": "applied_to", "dst": "TU Munich"},
                      {"src": "TU Munich MSc", "relation": "mentions", "dst": "Unknown thing"}]}
    counts = store.apply_extraction(data, P)
    assert counts["edges"] == 2 and counts["edges_skipped"] == 1
    assert store.stats()["edges"] == 1


def test_apply_extraction_is_atomic(store):
    bad = {"nodes": [node("project", "Fine"), node("not-a-type", "Broken")], "edges": []}
    with pytest.raises(ValueError):
        store.apply_extraction(bad, P)
    assert store.stats()["nodes_active"] == 0


def test_forget_removes_node_edges_and_fts(store):
    a, _ = store.upsert_node("project", "Secret Plan", "hidden", 0.5, P)
    b, _ = store.upsert_node("person", "Ana", "", 0.5, P)
    store.add_edge(b, a, "works_on", P)
    assert store.forget(a) and not store.forget(a)
    assert store.search("secret plan") == []
    assert store.stats()["edges"] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM provenance WHERE node_id = ?", (a,)).fetchone()[0] == 0


def test_meta_and_demo_flag(store):
    assert not store.is_demo
    store.set_meta("demo", "1")
    assert store.is_demo


def test_refuses_database_inside_repo(monkeypatch):
    root = project_root()
    assert root is not None
    monkeypatch.setenv("AGENT_ROOM_HOME", str(root / "agent-room-data"))
    with pytest.raises(RepoWriteError):
        MemoryStore()
    assert not (root / "agent-room-data").exists()


def test_underscored_names_dedupe_with_spaced_names(store):
    a, _ = store.upsert_node("project", "agent_room", "x", 0.5, P)
    b, action = store.upsert_node("project", "Agent Room", "y", 0.5, P)
    assert a == b and action == "merged"
