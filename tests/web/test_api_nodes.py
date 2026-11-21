"""/api/nodes: the Explorer's paged, sorted, filtered node list (read-only, store methods only, no summaries)."""

import asyncio
import re

import pytest

from memory.test_extraction_stage8 import Scripted, msgs
from room.memory import extractor as ex
from room.memory.readonly import ReadOnlyMemory
from room.memory.store import MemoryStore, Provenance
from web.test_api_safety import _hostile_plus_a_real_fact, _secret_corpus, _words

ITEM_KEYS = {"id", "type", "name", "status", "confidence", "updated_at"}


@pytest.fixture(scope="module")
def rich_db(tmp_path_factory):
    from room.web.demo_data import load_rich_demo

    home = tmp_path_factory.mktemp("rich")
    load_rich_demo(home).close()
    return home / "memory.db"


def _all(db):
    with ReadOnlyMemory(db) as store:
        return store.all_nodes(include_superseded=True)


def test_default_lists_every_node_by_name_with_total(serve, rich_db, tmp_path):
    status, body = serve(rich_db, tmp_path / "p").get("/api/nodes")
    assert status == 200 and set(body) == {"total", "items"}
    nodes = _all(rich_db)
    assert body["total"] == len(nodes) == 117
    assert len(body["items"]) == 50
    assert all(set(i) == ITEM_KEYS for i in body["items"])
    names = [i["name"].lower() for i in body["items"]]
    assert names == sorted(names)
    assert "summary" not in str(body)


def test_filters_by_type_and_status(serve, rich_db, tmp_path):
    client = serve(rich_db, tmp_path / "p")
    _, body = client.get("/api/nodes?type=decision&status=superseded&limit=200")
    expected = [n for n in _all(rich_db) if n.type == "decision" and n.status == "superseded"]
    assert body["total"] == len(expected) and {i["id"] for i in body["items"]} == {n.id for n in expected}
    _, active = client.get("/api/nodes?status=active&limit=200")
    assert active["total"] == 103 and all(i["status"] == "active" for i in active["items"])


@pytest.mark.parametrize("sort", ["name", "type", "confidence", "updated_at"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_sorts_by_each_column_both_ways(sort, order, serve, rich_db, tmp_path):
    _, body = serve(rich_db, tmp_path / "p").get(f"/api/nodes?sort={sort}&order={order}&limit=200")
    keys = [(i[sort].lower() if isinstance(i[sort], str) else i[sort]) for i in body["items"]]
    assert keys == sorted(keys, reverse=order == "desc")


def test_paging_with_offset_and_limit_cap(serve, rich_db, tmp_path):
    client = serve(rich_db, tmp_path / "p")
    first = client.get("/api/nodes?limit=50&offset=0")[1]["items"]
    second = client.get("/api/nodes?limit=50&offset=50")[1]["items"]
    last = client.get("/api/nodes?limit=50&offset=100")[1]
    assert not {i["id"] for i in first} & {i["id"] for i in second}
    assert len(last["items"]) == 17 and last["total"] == 117
    assert client.get("/api/nodes?offset=500")[1] == {"total": 117, "items": []}
    assert len(client.get("/api/nodes?limit=5000")[1]["items"]) == 117  # capped at 200, fewer exist
    with MemoryStore(tmp_path / "big.db") as store:
        store.apply_extraction({"nodes": [{"type": "fact", "name": f"F {i}", "summary": "x", "confidence": 0.5}
                                          for i in range(260)], "edges": []}, Provenance("s", "claude", "session"))
    big = serve(tmp_path / "big.db", tmp_path / "p")
    assert len(big.get("/api/nodes?limit=5000")[1]["items"]) == 200


def test_q_uses_the_fts_search(serve, rich_db, tmp_path):
    _, body = serve(rich_db, tmp_path / "p").get("/api/nodes?q=Tern%20Atlas&limit=200")
    with ReadOnlyMemory(rich_db) as store:
        expected = {n.id for n, _ in store.search("Tern Atlas", 2000, include_superseded=True)}
    assert body["total"] == len(expected) > 0 and {i["id"] for i in body["items"]} == expected
    _, sorted_body = serve(rich_db, tmp_path / "p2").get("/api/nodes?q=Tern&sort=name&order=asc")
    names = [i["name"].lower() for i in sorted_body["items"]]
    assert names == sorted(names)


@pytest.mark.parametrize("query", [
    "limit=0", "limit=-1", "limit=abc", "limit=" + "9" * 30, "offset=-1", "offset=abc", "offset=1.5",
    "offset=" + "9" * 30, "sort=summary", "sort=name;DROP%20TABLE%20nodes", "sort=id", "order=up",
    "order=asc%20--", "type=Project", "type=x'%20OR%201=1", "status=deleted", "status=active'--",
    "q=" + "a" * 201, "limit=1&limit=2",
])
def test_bad_input_is_400(query, serve, rich_db, tmp_path):
    status, body = serve(rich_db, tmp_path / "p").get(f"/api/nodes?{query}")
    assert status == 400 and set(body) == {"error"}, query


@pytest.mark.parametrize("q", ["' OR 1=1 --", "\"; DROP TABLE nodes; --", "nodes_fts MATCH *", "NEAR(a b)", "*"])
def test_sql_and_fts_like_q_is_harmless(q, serve, rich_db, tmp_path):
    from urllib.parse import quote

    before = rich_db.read_bytes()
    status, body = serve(rich_db, tmp_path / "p").get(f"/api/nodes?q={quote(q)}")
    assert status == 200 and set(body) == {"total", "items"}
    assert rich_db.read_bytes() == before
    assert len(_all(rich_db)) == 117


def test_read_only_bytes_and_mtime_unchanged(serve, rich_db, tmp_path):
    before = (rich_db.read_bytes(), rich_db.stat().st_mtime_ns)
    client = serve(rich_db, tmp_path / "p")
    for path in ("/api/nodes", "/api/nodes?q=Tern", "/api/nodes?type=task&sort=confidence&order=desc",
                 "/api/nodes?offset=100&limit=200", "/api/nodes?limit=0"):
        for method in ("GET", "HEAD"):
            assert client.request(method, path)[0] in (200, 400)
        assert client.request("POST", path)[0] == 405
    assert (rich_db.read_bytes(), rich_db.stat().st_mtime_ns) == before


def test_503_without_a_database(serve, tmp_path):
    assert serve(tmp_path / "none" / "memory.db", tmp_path / "p").get("/api/nodes")[0] == 503


def test_no_secret_reaches_the_node_list(serve, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "hermes")
    monkeypatch.delenv("MEMORY_EXTRACTOR_FALLBACK", raising=False)
    db = tmp_path / "seeded" / "memory.db"
    db.parent.mkdir()
    corpus = _secret_corpus()
    assert len(corpus) == 45
    for i, (text, value) in enumerate(corpus):
        agent = Scripted(_hostile_plus_a_real_fact(value))
        messages = msgs(f"I'm building Harbor Log, a log viewer for Trail Tracker. {text}", sid=f"web-{i}")
        asyncio.run(ex.extract_session(messages[0].session_id, messages, lambda name: agent, store_path=db))
    client = serve(db, tmp_path / "p")
    from urllib.parse import quote

    paths = ["/api/nodes?limit=200", "/api/nodes?status=all&limit=200", "/api/nodes?sort=confidence&order=desc"]
    paths += [f"/api/nodes?q={quote(w)}" for _, v in corpus for w in _words(v)]
    bodies = [client.request("GET", p)[2].decode() for p in paths]
    assert '"items": [{' in bodies[0]  # the seeded memory holds nodes, so the scan means something
    blob = "\n".join(bodies)
    leaks = [v for _, v in corpus if v in blob]
    leaks += [w for _, v in corpus for w in _words(v) if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", blob)]
    assert leaks == []
