"""Dashboard API (room.web.api): the endpoints and their JSON shapes. Fake data only (the made-up demo set)."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone

from room.cli import build_parser
from room.memory.readonly import ReadOnlyMemory
from room.memory.store import MemoryStore, Provenance
from room.presence import write_presence

GRAPH_NODE_KEYS = {"id", "type", "name", "status", "confidence", "degree"}
EDGE_KEYS = {"id", "src", "dst", "relation"}
NODE_KEYS = {"id", "type", "name", "summary", "aliases", "status", "confidence", "created_at", "updated_at",
             "provenance", "neighbors"}


def _id_of(client, name, status="active"):
    _, graph = client.get(f"/api/graph?status={status}")
    return next(n["id"] for n in graph["nodes"] if n["name"] == name)


# -- /api/health ---------------------------------------------------------------------------------------------------

def test_health_reports_local_mode_and_schema_version(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    status, body = client.get("/api/health")
    assert status == 200
    with ReadOnlyMemory(demo_db) as store:
        assert body == {"ok": True, "mode": "local", "schema_version": store.schema_version()}


# -- /api/graph ----------------------------------------------------------------------------------------------------

def test_graph_lists_active_nodes_and_their_edges_without_summaries(serve, demo_db, tmp_path):
    status, graph = serve(demo_db, tmp_path / "presence").get("/api/graph")
    assert status == 200
    with ReadOnlyMemory(demo_db) as store:
        active = {n.id for n in store.all_nodes()}
    assert {n["id"] for n in graph["nodes"]} == active
    assert all(set(n) == GRAPH_NODE_KEYS and n["status"] == "active" for n in graph["nodes"])
    assert "summary" not in json.dumps(graph)
    ids = {n["id"] for n in graph["nodes"]}
    assert graph["edges"] and all(set(e) == EDGE_KEYS and e["src"] in ids and e["dst"] in ids for e in graph["edges"])


def test_graph_degree_counts_the_edges_returned(serve, demo_db, tmp_path):
    _, graph = serve(demo_db, tmp_path / "presence").get("/api/graph")
    for n in graph["nodes"]:
        assert n["degree"] == sum(n["id"] in (e["src"], e["dst"]) for e in graph["edges"])


def test_graph_filters_by_type_and_status(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    _, graph = client.get("/api/graph?types=project,skill")
    assert {n["type"] for n in graph["nodes"]} == {"project", "skill"} and len(graph["nodes"]) == 4
    _, sup = client.get("/api/graph?status=superseded")
    assert len(sup["nodes"]) == 2 and all(n["status"] == "superseded" for n in sup["nodes"])
    _, both = client.get("/api/graph?status=all")
    assert len(both["nodes"]) == 16
    assert any(e["relation"] == "supersedes" for e in both["edges"])


def test_graph_limit_and_cap_at_2000(serve, tmp_path):
    db = tmp_path / "big" / "memory.db"
    db.parent.mkdir()
    with MemoryStore(db) as store:
        nodes = [{"type": "fact", "name": f"Fact number {i}", "summary": "A made-up fact.", "confidence": 0.5}
                 for i in range(2100)]
        store.apply_extraction({"nodes": nodes, "edges": []}, Provenance("s-big", "test", "manual"))
    client = serve(db, tmp_path / "presence")
    assert len(client.get("/api/graph?limit=3")[1]["nodes"]) == 3
    assert len(client.get("/api/graph")[1]["nodes"]) == 500
    assert len(client.get("/api/graph?limit=2000")[1]["nodes"]) == 2000
    assert len(client.get("/api/graph?limit=5000")[1]["nodes"]) == 2000


# -- /api/node/<id> ------------------------------------------------------------------------------------------------

def test_node_detail_has_summary_provenance_and_neighbors(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    node_id = _id_of(client, "Lumen Notes")
    status, node = client.get(f"/api/node/{node_id}")
    assert status == 200 and set(node) == NODE_KEYS
    assert node["name"] == "Lumen Notes" and node["summary"].startswith("Offline-first notes app")
    assert isinstance(node["aliases"], list)
    assert node["provenance"] and all(set(p) == {"agent", "session_id", "source", "ts"} for p in node["provenance"])
    assert {p["session_id"] for p in node["provenance"]} == {"demo-2026-09-01"}
    neighbors = {(n["name"], n["relation"], n["direction"]) for n in node["neighbors"]}
    assert ("Sam Rivera", "works_on", "in") in neighbors and ("TypeScript", "requires", "out") in neighbors
    assert all(set(n) == {"id", "name", "type", "relation", "direction"} for n in node["neighbors"])


def test_unknown_node_is_404(serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "presence").get("/api/node/999999")
    assert status == 404 and set(body) == {"error"}


# -- /api/search ---------------------------------------------------------------------------------------------------

def test_search_reuses_the_store_fts_search(serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "presence").get("/api/search?q=Lumen%20Notes")
    assert status == 200
    with ReadOnlyMemory(demo_db) as store:
        expected = [n.id for n, _ in store.search("Lumen Notes", 25)]
    assert [r["id"] for r in body["results"]] == expected and expected
    assert len(body["results"]) <= 25


def test_search_returns_at_most_25(serve, tmp_path):
    db = tmp_path / "many" / "memory.db"
    db.parent.mkdir()
    with MemoryStore(db) as store:
        nodes = [{"type": "fact", "name": f"Harbor fact {i}", "summary": "Harbor detail.", "confidence": 0.5}
                 for i in range(40)]
        store.apply_extraction({"nodes": nodes, "edges": []}, Provenance("s-many", "test", "manual"))
    _, body = serve(db, tmp_path / "presence").get("/api/search?q=Harbor")
    assert len(body["results"]) == 25


# -- /api/presence -------------------------------------------------------------------------------------------------

def test_presence_reports_effective_status_and_age(serve, demo_db, tmp_path):
    pdir = tmp_path / "presence"
    write_presence("claude", "working", "made-up task text", pdir)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
    (pdir / "gemini.json").write_text(json.dumps({"agent": "gemini", "status": "working", "task": "x", "pid": 1,
                                                   "updated_at": old}))
    status, body = serve(demo_db, pdir).get("/api/presence")
    assert status == 200
    agents = {a["agent"]: a for a in body["agents"]}
    assert agents["claude"]["effective_status"] == "working" and agents["gemini"]["effective_status"] == "idle"
    assert 0 <= agents["claude"]["age_seconds"] < 60 and agents["gemini"]["age_seconds"] >= 590
    assert "made-up task text" not in json.dumps(body)  # free text is not exposed


# -- /api/stats ----------------------------------------------------------------------------------------------------

def test_stats_match_the_store_and_privacy_facts_are_read_not_invented(serve, demo_db, tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_EXTRACTOR", raising=False)
    status, body = serve(demo_db, tmp_path / "presence").get("/api/stats")
    assert status == 200
    with ReadOnlyMemory(demo_db) as store:
        s = store.stats()
    assert body["nodes_by_type"] == s["by_type"]
    assert body["nodes_active"] == s["nodes_active"] == 14 and body["nodes_superseded"] == s["nodes_superseded"] == 2
    assert body["edges"] == s["edges"] and body["sessions"] == s["sessions"] == 2
    from room.memory.extractor import DEFAULT_EXTRACTOR
    assert body["privacy"] == {
        "database_read_only": True,
        "database_file_mode": oct(stat.S_IMODE(os.stat(demo_db).st_mode)),
        "extractor": DEFAULT_EXTRACTOR,
        "demo_data": True,
        "bind_address": "127.0.0.1",
    }


def test_stats_extractor_follows_the_environment(serve, demo_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "gemini")
    assert serve(demo_db, tmp_path / "presence").get("/api/stats")[1]["privacy"]["extractor"] == "gemini"


# -- general -------------------------------------------------------------------------------------------------------

def test_head_returns_headers_only(serve, demo_db, tmp_path):
    status, headers, body = serve(demo_db, tmp_path / "presence").request("HEAD", "/api/health")
    assert status == 200 and body == b"" and headers["content-type"].startswith("application/json")


def test_unknown_path_is_404_json(serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "presence").get("/api/nope")
    assert status == 404 and set(body) == {"error"}


# -- demo mode -----------------------------------------------------------------------------------------------------

def test_demo_mode_serves_a_temp_copy_and_never_reads_the_real_home(serve, isolated_home, monkeypatch):
    from room.web import api

    real_db = isolated_home / "memory.db"
    isolated_home.mkdir(parents=True, exist_ok=True)
    with MemoryStore(real_db) as store:
        store.apply_extraction({"nodes": [{"type": "project", "name": "Real Private Project", "summary": "x",
                                           "confidence": 0.9}], "edges": []}, Provenance("s-real", "test", "manual"))
    write_presence("claude", "working", "", isolated_home / "presence")
    before = real_db.read_bytes()

    home = api.demo_home()
    assert isolated_home not in home.parents and home != isolated_home
    client = serve(home / "memory.db", home / "presence", mode="demo")
    assert client.get("/api/health")[1]["mode"] == "demo"
    names = {n["name"] for n in client.get("/api/graph")[1]["nodes"]}
    assert "Lumen Notes" in names and "Real Private Project" not in names
    assert client.get("/api/presence")[1]["agents"] == []
    assert real_db.read_bytes() == before


def test_cli_has_the_dashboard_api_subcommand_and_keeps_the_others():
    parser = build_parser()
    args = parser.parse_args(["dashboard-api", "--port", "9999", "--demo"])
    assert (args.command, args.port, args.demo) == ("dashboard-api", 9999, True)
    args = parser.parse_args(["dashboard-api"])
    assert (args.port, args.demo) == (8765, False)
    assert parser.parse_args(["memory", "search", "x"]).command == "memory"
    assert parser.parse_args(["mcp"]).command == "mcp"


def test_cli_dashboard_api_runs_serve(monkeypatch):
    from room import cli
    from room.web import api

    calls = []
    monkeypatch.setattr(api, "serve", lambda port, demo: calls.append((port, demo)) or 0)
    assert cli.main(["dashboard-api", "--port", "9001", "--demo"]) == 0
    assert calls == [(9001, True)]
