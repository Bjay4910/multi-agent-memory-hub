"""Dashboard API safety: localhost only, read-only, validated input, no CORS, no secrets, clean errors.

Fake values only: the secrets are the stage 8 and 9 test strings, stored through the normal extraction path.
"""

import asyncio
import re
import socket
import sqlite3

import pytest

from memory.test_extraction_stage8 import GAP1, GAP2, Scripted, hostile, msgs
from memory.test_extraction_stage9 import MORE_LABEL_VALUES, MORE_LABELS, NORMALIZED, USERINFO
from room.memory import extractor as ex
from room.memory.readonly import MemoryUnavailable, ReadOnlyMemory


def _all_paths(client):
    """Every endpoint the API serves, with real ids from the graph."""
    _, graph = client.get("/api/graph?status=all&limit=2000")
    ids = [n["id"] for n in graph["nodes"]]
    return (["/api/health", "/api/graph", "/api/graph?status=all&limit=2000", "/api/presence", "/api/stats",
             "/api/search?q=Harbor", "/api/search?q=Trail%20Tracker", "/api/node/1"]
            + [f"/api/node/{i}" for i in ids])


# -- binding -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "192.168.1.10", "localhost"])
def test_refuses_to_bind_anything_but_127_0_0_1(host, demo_db, tmp_path):
    from room.web import api

    with pytest.raises(ValueError):
        api.make_server(host, 0, db_path=demo_db, presence_dir=tmp_path / "p")


def test_binds_127_0_0_1(demo_db, tmp_path):
    from room.web import api

    server = api.make_server("127.0.0.1", 0, db_path=demo_db, presence_dir=tmp_path / "p")
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_serve_refuses_a_non_local_host(monkeypatch):
    from room.web import api

    with pytest.raises(ValueError):
        api.serve(port=0, demo=True, host="0.0.0.0")


# -- methods -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_write_methods_get_405_and_change_nothing(method, serve, demo_db, tmp_path):
    before = (demo_db.read_bytes(), demo_db.stat().st_mtime_ns)
    client = serve(demo_db, tmp_path / "presence")
    for path in ("/api/graph", "/api/node/1", "/api/stats"):
        status, headers, _ = client.request(method, path, body=b'{"name": "x"}')
        assert status == 405 and headers.get("allow") == "GET, HEAD"
    assert (demo_db.read_bytes(), demo_db.stat().st_mtime_ns) == before


# -- Host header and CORS -----------------------------------------------------------------------------------------

def test_host_header_must_be_localhost_on_this_port(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    port = client.port
    for good in (f"127.0.0.1:{port}", f"localhost:{port}", "localhost", "127.0.0.1", f"LOCALHOST:{port}"):
        assert client.request("GET", "/api/health", host=good)[0] == 200, good
    for bad in ("evil.example.com", f"evil.example.com:{port}", f"127.0.0.1:{port + 1}", "localhost.evil.com",
                f"127.0.0.1.nip.io:{port}", "[::1]", f"localhost:{port}x"):
        status, _, body = client.request("GET", "/api/health", host=bad)
        assert status == 403, bad
        assert b"schema_version" not in body


def test_missing_host_header_is_403(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    with socket.create_connection(("127.0.0.1", client.port), timeout=5) as sock:
        sock.sendall(b"GET /api/health HTTP/1.0\r\n\r\n")
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    assert data.startswith(b"HTTP/1.0 403")


def test_no_cors_headers_ever(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    origin = {"Origin": "https://evil.example.com"}
    for path in _all_paths(client) + ["/api/nope", "/api/node/abc"]:
        for method in ("GET", "HEAD", "OPTIONS", "POST"):
            _, headers, _ = client.request(method, path, headers=origin)
            assert not [h for h in headers if h.startswith("access-control-")], (method, path)
    _, headers, _ = client.request("GET", "/api/health", host="evil.example.com", headers=origin)
    assert not [h for h in headers if h.startswith("access-control-")]


# -- read-only database --------------------------------------------------------------------------------------------

def test_the_api_connection_cannot_write(demo_db):
    from room.web import api

    store = api.open_memory(demo_db)
    try:
        assert isinstance(store, ReadOnlyMemory)
        with pytest.raises(sqlite3.OperationalError):
            store.conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
        with pytest.raises(sqlite3.OperationalError):
            store.conn.execute("DELETE FROM nodes")
        with pytest.raises(PermissionError):
            store.tx()
    finally:
        store.close()


def test_every_endpoint_leaves_the_database_bytes_and_mtime_unchanged(serve, demo_db, tmp_path):
    before = (demo_db.read_bytes(), demo_db.stat().st_mtime_ns)
    client = serve(demo_db, tmp_path / "presence")
    for path in _all_paths(client):
        for method in ("GET", "HEAD"):
            assert client.request(method, path)[0] in (200, 404), path
    assert (demo_db.read_bytes(), demo_db.stat().st_mtime_ns) == before


# -- input validation ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/node/abc", "/api/node/-1", "/api/node/0", "/api/node/1.5", "/api/node/99999999999999999999999",
    "/api/node/1%20OR%201=1", "/api/node/", "/api/node/1/2",
    "/api/graph?limit=abc", "/api/graph?limit=-5", "/api/graph?limit=0", "/api/graph?limit=1e3",
    "/api/graph?limit=" + "9" * 30, "/api/graph?status=bogus", "/api/graph?types=DROP%20TABLE",
    "/api/graph?types=project;skill", "/api/search", "/api/search?q=", "/api/search?q=%20%20",
    "/api/search?q=" + "a" * 201,
])
def test_bad_input_is_400_with_a_plain_message(path, serve, demo_db, tmp_path):
    status, body = serve(demo_db, tmp_path / "presence").get(path)
    assert status == 400 and set(body) == {"error"}
    assert "Traceback" not in body["error"] and "File \"" not in body["error"]


def test_q_of_200_characters_is_accepted(serve, demo_db, tmp_path):
    assert serve(demo_db, tmp_path / "presence").get("/api/search?q=" + "a" * 200)[0] == 200


def test_an_internal_error_gives_500_without_details(serve, demo_db, tmp_path, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("boom internal detail")

    monkeypatch.setattr(ReadOnlyMemory, "stats", boom)
    status, headers, body = serve(demo_db, tmp_path / "presence").request("GET", "/api/stats")
    assert status == 500
    assert b"boom" not in body and b"Traceback" not in body and b"File" not in body
    assert headers["content-type"].startswith("application/json")


# -- missing database ----------------------------------------------------------------------------------------------

def test_missing_database_is_a_clean_503(serve, tmp_path):
    missing = tmp_path / "nowhere" / "memory.db"
    try:
        ReadOnlyMemory(missing)
    except MemoryUnavailable as exc:
        message = str(exc)
    client = serve(missing, tmp_path / "presence")
    for path in ("/api/health", "/api/graph", "/api/node/1", "/api/search?q=x", "/api/stats"):
        status, body = client.get(path)
        assert status == 503 and body["error"] == message, path
    assert client.get("/api/health")[1]["ok"] is False
    assert client.get("/api/presence")[0] == 200  # presence does not need the database
    assert not missing.exists() and not missing.parent.exists()


# -- secrets -------------------------------------------------------------------------------------------------------

def _secret_corpus():
    corpus = [GAP1[k] for k in GAP1] + [GAP2[k] for k in GAP2] + [NORMALIZED[k] for k in NORMALIZED]
    corpus += [(MORE_LABELS[k], MORE_LABEL_VALUES[k]) for k in MORE_LABELS]
    corpus += [(USERINFO[k][0], USERINFO[k][2]) for k in USERINFO]
    return corpus


def _hostile_plus_a_real_fact(value):
    """The stage 8 hostile model, plus one ordinary grounded fact so the seeded memory is not empty."""
    import json

    def answer(prompt):
        data = json.loads(hostile(value)(prompt))
        data["nodes"].append({"type": "project", "name": "Trail Tracker", "confidence": 0.9,
                              "summary": "Trail Tracker has a log viewer called Harbor Log.",
                              "evidence": "Harbor Log, a log viewer for Trail Tracker"})
        return json.dumps(data)
    return answer


def _words(value):
    return [w for w in re.split(r"[\s:/,;()<>\"\\]+", value) if len(w) >= 3]


def test_no_secret_value_appears_in_any_response(serve, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "hermes")
    monkeypatch.delenv("MEMORY_EXTRACTOR_FALLBACK", raising=False)
    db = tmp_path / "seeded" / "memory.db"
    db.parent.mkdir()
    corpus = _secret_corpus()
    for i, (text, value) in enumerate(corpus):
        agent = Scripted(_hostile_plus_a_real_fact(value))
        messages = msgs(f"I'm building Harbor Log, a log viewer for Trail Tracker. {text}", sid=f"web-{i}")
        asyncio.run(ex.extract_session(messages[0].session_id, messages, lambda name: agent, store_path=db))
    client = serve(db, tmp_path / "presence")
    _, graph = client.get("/api/graph?status=all&limit=2000")
    assert graph["nodes"], "the seeded memory must hold something, or this test proves nothing"

    paths = _all_paths(client) + [f"/api/search?q={w}" for _, v in corpus for w in _words(v)][:200]
    bodies = []
    for path in paths:
        status, _, body = client.request("GET", path)
        assert status in (200, 400, 404), path
        bodies.append(body.decode("utf-8"))
    blob = "\n".join(bodies)
    leaks = [v for _, v in corpus if v in blob]
    leaks += [w for _, v in corpus for w in _words(v) if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", blob)]
    assert leaks == []
