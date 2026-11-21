"""/api/sessions, /api/session/<id> and /api/agents: read-only, derived from the provenance table."""

import asyncio
import re
import sqlite3

import pytest

from room.memory import extractor as ex
from room.memory.store import MemoryStore, Provenance
from web.test_api_safety import _hostile_plus_a_real_fact, _secret_corpus, _words

from memory.test_extraction_stage8 import Scripted, msgs

SESSION_KEYS = {"session_id", "agent", "first_ts", "last_ts", "nodes_added", "edges_added"}


def _expected_sessions(db):
    """The same summary computed independently, straight from the provenance table (read-only)."""
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT session_id, MIN(ts), MAX(ts), COUNT(DISTINCT node_id), COUNT(DISTINCT edge_id) "
        "FROM provenance GROUP BY session_id").fetchall()
    conn.close()
    return {r[0]: {"first_ts": r[1], "last_ts": r[2], "nodes_added": r[3], "edges_added": r[4]} for r in rows}


# -- /api/sessions -------------------------------------------------------------------------------------------------

def test_sessions_newest_first_with_counts_from_provenance(serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "p").get("/api/sessions")
    assert status == 200
    sessions = body["sessions"]
    assert [s["session_id"] for s in sessions] == ["demo-2026-11-01", "demo-2026-09-01"]
    assert all(set(s) == SESSION_KEYS and s["agent"] == "demo" for s in sessions)
    expected = _expected_sessions(demo_db)
    for s in sessions:
        assert {k: s[k] for k in ("first_ts", "last_ts", "nodes_added", "edges_added")} == expected[s["session_id"]]


@pytest.fixture(scope="module")
def many_db(tmp_path_factory):
    """250 one-node sessions, and one session with 250 nodes."""
    db = tmp_path_factory.mktemp("many") / "memory.db"
    with MemoryStore(db) as store:
        for i in range(250):
            store.apply_extraction({"nodes": [{"type": "fact", "name": f"Fact {i}", "summary": "A fact.", "confidence": 0.5}],
                                    "edges": []}, Provenance(f"s-{i:03d}", "claude", "session"),
                                   ts=f"2026-08-{1 + i // 10:02d}T{i % 10:02d}:00:00Z")
        store.apply_extraction({"nodes": [{"type": "fact", "name": f"Big {i}", "summary": "A fact.", "confidence": 0.5}
                                          for i in range(250)], "edges": []},
                               Provenance("big-session", "gemini", "session"), ts="2026-07-01T00:00:00Z")
    return db


def test_sessions_limit_default_50_and_cap_200(serve, many_db, tmp_path):
    client = serve(many_db, tmp_path / "p")
    assert len(client.get("/api/sessions")[1]["sessions"]) == 50
    assert len(client.get("/api/sessions?limit=7")[1]["sessions"]) == 7
    assert len(client.get("/api/sessions?limit=200")[1]["sessions"]) == 200
    assert len(client.get("/api/sessions?limit=5000")[1]["sessions"]) == 200
    lasts = [s["last_ts"] for s in client.get("/api/sessions?limit=200")[1]["sessions"]]
    assert lasts == sorted(lasts, reverse=True)


@pytest.mark.parametrize("query", ["limit=abc", "limit=-1", "limit=0", "limit=1.5", "limit=" + "9" * 30, "limit=1&limit=2"])
def test_sessions_bad_limit_is_400(query, serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "p").get(f"/api/sessions?{query}")
    assert status == 400 and set(body) == {"error"}


# -- /api/session/<id> ---------------------------------------------------------------------------------------------

def test_session_detail_lists_its_nodes_and_edges_without_summaries(serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "p").get("/api/session/demo-2026-09-01")
    assert status == 200
    assert set(body) == {"session_id", "agent", "first_ts", "last_ts", "nodes", "edges"}
    assert body["session_id"] == "demo-2026-09-01" and body["agent"] == "demo"
    names = {n["name"] for n in body["nodes"]}
    assert {"Sam Rivera", "Lumen Notes", "Trail Tracker"} <= names and len(body["nodes"]) == 13
    assert all(set(n) == {"id", "name", "type", "status"} for n in body["nodes"])
    assert all(set(e) == {"src", "dst", "relation"} for e in body["edges"]) and len(body["edges"]) == 9
    assert "summary" not in str(body) and "Offline-first" not in str(body)


def test_session_detail_caps_nodes_at_200(serve, many_db, tmp_path):
    _, body = serve(many_db, tmp_path / "p").get("/api/session/big-session")
    assert len(body["nodes"]) == 200 and body["agent"] == "gemini"


def test_unknown_session_is_404(serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "p").get("/api/session/no-such-session")
    assert status == 404 and set(body) == {"error"}


@pytest.mark.parametrize("sid", ["", "a" * 201, "bad%20id", "a/b", "a'b", "a%27b", "<script>", "%2e%2e", "a;b", "a%20b"])
def test_bad_session_id_is_400(sid, serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "p").get(f"/api/session/{sid}")
    assert status == 400 and set(body) == {"error"}, sid


# -- /api/agents ---------------------------------------------------------------------------------------------------

def test_agents_from_provenance(serve, many_db, tmp_path):
    status, body = serve(many_db, tmp_path / "p").get("/api/agents")
    assert status == 200
    agents = {a["agent"]: a for a in body["agents"]}
    assert set(agents) == {"claude", "gemini"}
    assert agents["claude"]["nodes_added"] == 250 and agents["claude"]["sessions"] == 250
    assert agents["gemini"]["nodes_added"] == 250 and agents["gemini"]["sessions"] == 1
    assert agents["claude"]["last_session_id"] == "s-249"
    assert all(set(a) == {"agent", "nodes_added", "sessions", "last_ts", "last_session_id"} for a in body["agents"])
    assert [a["agent"] for a in body["agents"]] == ["claude", "gemini"]  # most recent first


# -- safety --------------------------------------------------------------------------------------------------------

def test_new_endpoints_leave_the_database_bytes_and_mtime_unchanged(serve, demo_db, tmp_path):
    before = (demo_db.read_bytes(), demo_db.stat().st_mtime_ns)
    client = serve(demo_db, tmp_path / "p")
    for path in ("/api/sessions", "/api/sessions?limit=200", "/api/session/demo-2026-09-01",
                 "/api/session/demo-2026-11-01", "/api/session/nope", "/api/agents"):
        for method in ("GET", "HEAD"):
            assert client.request(method, path)[0] in (200, 404)
        assert client.request("POST", path)[0] == 405
    assert (demo_db.read_bytes(), demo_db.stat().st_mtime_ns) == before


def test_new_endpoints_are_503_without_a_database(serve, tmp_path):
    client = serve(tmp_path / "none" / "memory.db", tmp_path / "p")
    for path in ("/api/sessions", "/api/session/x1", "/api/agents"):
        assert client.get(path)[0] == 503


def test_no_secret_reaches_the_new_endpoints(serve, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "hermes")
    monkeypatch.delenv("MEMORY_EXTRACTOR_FALLBACK", raising=False)
    db = tmp_path / "seeded" / "memory.db"
    db.parent.mkdir()
    corpus = _secret_corpus()
    assert len(corpus) == 45  # the F1 corpus: 9 + 4 + 8 + 20 + 4 (F1 was misreported as 46)
    for i, (text, value) in enumerate(corpus):
        agent = Scripted(_hostile_plus_a_real_fact(value))
        messages = msgs(f"I'm building Harbor Log, a log viewer for Trail Tracker. {text}", sid=f"web-{i}")
        asyncio.run(ex.extract_session(messages[0].session_id, messages, lambda name: agent, store_path=db))
    client = serve(db, tmp_path / "p")
    _, sessions = client.get("/api/sessions?limit=200")
    assert len(sessions["sessions"]) == 45
    bodies = [client.request("GET", p)[2].decode() for p in
              ["/api/sessions?limit=200", "/api/agents"] + [f"/api/session/{s['session_id']}" for s in sessions["sessions"]]]
    assert any('"nodes": [{' in b for b in bodies)  # the seeded sessions hold nodes, so the scan means something
    blob = "\n".join(bodies)
    leaks = [v for _, v in corpus if v in blob]
    leaks += [w for _, v in corpus for w in _words(v) if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", blob)]
    assert leaks == []
