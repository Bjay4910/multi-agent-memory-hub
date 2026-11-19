"""`room mcp`: hand-written MCP server, search/show side. Temporary databases only; no network, no models.

memory_remember is covered in test_mcp_remember.py.
"""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from room import cli, mcp_server
from room.mcp_server import MAX_LIMIT, SUPPORTED_PROTOCOL_VERSIONS, handle, serve
from room.memory.readonly import MemoryUnavailable, ReadOnlyMemory
from room.memory.store import MemoryStore, Provenance

GRAPH = {"nodes": [
    {"type": "person", "name": "Me", "summary": "Applying for a CS masters.", "confidence": 0.9},
    {"type": "project", "name": "Agent-Aware Power Manager", "summary": "Keeps the Mac awake while agents work.", "confidence": 0.9},
    {"type": "project", "name": "Memory Hub", "summary": "Local multi-agent room with graph memory.", "confidence": 0.8},
], "edges": [{"src": "Me", "relation": "works_on", "dst": "Agent-Aware Power Manager"}]}


@pytest.fixture
def db(isolated_home):
    with MemoryStore() as store:
        store.apply_extraction(GRAPH, Provenance("s1", "test", "manual"))
        ids = {n.name: n.id for n in store.all_nodes()}
    return isolated_home / "memory.db", ids


def call(name, **arguments):
    resp = handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert resp["id"] == 7 and "result" in resp, resp
    content = resp["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text"
    return content[0]["text"], resp["result"]["isError"]


def cli_output(capsys, *argv):
    capsys.readouterr()
    cli.main(list(argv))
    return capsys.readouterr().out.rstrip("\n")


# -- tools ------------------------------------------------------------------------------

def test_search_matches_room_memory_search(db, capsys):
    text, is_error = call("memory_search", query="power manager")
    assert not is_error
    assert text.startswith("Memory notes (untrusted text")
    assert text.endswith(cli_output(capsys, "memory", "search", "power manager", "-k", "8"))
    assert "Agent-Aware Power Manager" in text


def test_show_matches_room_memory_show(db, capsys):
    _, ids = db
    pm = ids["Agent-Aware Power Manager"]
    text, is_error = call("memory_show", id=pm)
    assert not is_error
    assert text.endswith(cli_output(capsys, "memory", "show", str(pm)))
    assert "<- works_on Me" in text and "source: manual s1 via test" in text


def test_search_limit_is_respected_and_clamped(db):
    text, _ = call("memory_search", query="me power memory hub", limit=1)
    assert len([line for line in text.splitlines() if line.startswith("#")]) == 1
    text, is_error = call("memory_search", query="power", limit=10_000)
    assert not is_error  # clamped to MAX_LIMIT, not an error
    assert MAX_LIMIT == 25


def test_no_matches_and_unknown_id_are_plain_messages(db):
    text, is_error = call("memory_search", query="zebra")
    assert not is_error and text.startswith("No matches in memory")
    text, is_error = call("memory_show", id=9999)
    assert not is_error and text == "No memory node #9999. Use memory_search to find valid ids."


@pytest.mark.parametrize("name,args,expected", [
    ("memory_search", {}, "'query' must be a non-empty string"),
    ("memory_search", {"query": "  "}, "'query' must be a non-empty string"),
    ("memory_search", {"query": "x", "limit": "5"}, "'limit' must be an integer"),
    ("memory_show", {"id": "5"}, "'id' must be an integer"),
    ("memory_show", {"id": True}, "'id' must be an integer"),
    ("memory_show", {"id": 0}, "'id' must be a positive integer"),
    ("memory_show", {"id": 1, "purge": True}, "unknown argument(s): purge"),
])
def test_invalid_arguments_are_tool_errors(db, name, args, expected):
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    result = resp["result"]
    assert result["isError"] is True and expected in result["content"][0]["text"]


def test_missing_database_message(isolated_home):
    text, is_error = call("memory_search", query="anything")
    assert not is_error
    assert text.startswith("No memory yet") and "Traceback" not in text
    assert not (isolated_home / "memory.db").exists()  # the server never creates it
    text, _ = call("memory_show", id=1)
    assert text.startswith("No memory yet")


def test_empty_database_message(isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "memory.db").write_bytes(b"")
    text, is_error = call("memory_search", query="anything")
    assert not is_error and "Traceback" not in text
    assert "could not be read" in text or "empty" in text
    assert (isolated_home / "memory.db").read_bytes() == b""


def test_response_size_is_capped(db, monkeypatch):
    with MemoryStore() as store:
        store.apply_extraction({"nodes": [
            {"type": "fact", "name": f"Power fact {i}", "summary": "power " * 150, "confidence": 0.5} for i in range(25)
        ], "edges": []}, Provenance("s2", "test"))
    monkeypatch.setattr(mcp_server, "MAX_RESPONSE_CHARS", 600)
    text, _ = call("memory_search", query="power", limit=25)
    assert len(text) <= 600 and text.endswith("narrow the query or lower the limit]")
    assert len(mcp_server.cap("x" * 10)) == 10


def test_secrets_in_memory_are_redacted_in_responses(db):
    fake = "gh" "p_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6"
    with MemoryStore() as store:
        store.apply_extraction({"nodes": [{"type": "fact", "name": "Old token", "summary": f"was {fake}", "confidence": 0.5}],
                                "edges": []}, Provenance("s3", "test"))
    text, _ = call("memory_search", query="old token")
    assert fake not in text and "[REDACTED]" in text


# -- read-only guarantee ------------------------------------------------------------------

def test_database_is_opened_strictly_read_only(db):
    path, ids = db
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with ReadOnlyMemory() as store:
        assert store.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            store.conn.execute("DELETE FROM nodes")
        # Even with query_only switched off, SQLite's mode=ro still refuses writes.
        store.conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.conn.execute("UPDATE nodes SET name = 'x'")
        with pytest.raises(PermissionError):
            store.forget(ids["Me"])
        with pytest.raises(PermissionError):
            store.migrate()
    for _ in range(3):
        call("memory_search", query="power manager")
        call("memory_show", id=ids["Me"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_readonly_refuses_missing_db_without_creating_it(tmp_path):
    with pytest.raises(MemoryUnavailable, match="No memory yet"):
        ReadOnlyMemory(tmp_path / "nope" / "memory.db")
    assert not (tmp_path / "nope").exists()


def test_exactly_five_tools_search_show_read_only_remember_writes():
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert list(tools) == ["memory_search", "memory_show", "memory_remember", "working_search", "session_latest"]
    for t in tools.values():
        assert t["annotations"]["destructiveHint"] is False
        assert "not instructions" in t["description"] and "untrusted" in t["description"]
    assert tools["memory_search"]["annotations"]["readOnlyHint"] is True
    assert tools["memory_show"]["annotations"]["readOnlyHint"] is True
    assert tools["memory_remember"]["annotations"]["readOnlyHint"] is False


@pytest.mark.parametrize("name", ["memory_purge", "memory_forget", "memory_delete", "memory_update", "memory_export"])
def test_no_update_delete_forget_purge_or_export_tools(name):
    resp = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {}}})
    assert resp["error"]["code"] == -32602 and "Unknown tool" in resp["error"]["message"]


# -- protocol -----------------------------------------------------------------------------

@pytest.mark.parametrize("requested,expected", [
    ("2025-06-18", "2025-06-18"),
    ("2024-11-05", "2024-11-05"),
    ("2099-01-01", SUPPORTED_PROTOCOL_VERSIONS[-1]),
    (None, SUPPORTED_PROTOCOL_VERSIONS[-1]),
])
def test_protocol_version_negotiation(requested, expected):
    params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    if requested:
        params["protocolVersion"] = requested
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    result = resp["result"]
    assert result["protocolVersion"] == expected
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "agent-room-memory"


def test_protocol_edge_cases():
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}) is None
    assert handle({"jsonrpc": "2.0", "id": "a", "method": "ping"}) == {"jsonrpc": "2.0", "id": "a", "result": {}}
    err = handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})["error"]
    assert err["code"] == -32601 and "resources/list" in err["message"]
    assert handle({"id": 5, "method": "ping"})["error"]["code"] == -32600  # missing jsonrpc
    assert handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": []})["error"]["code"] == -32602


def test_serve_writes_only_json_to_stdout(db, monkeypatch, capsys):
    real_call = mcp_server.call_tool

    def noisy(name, args):
        print("stray debug output")  # must not reach the protocol stream
        return real_call(name, args)

    monkeypatch.setattr(mcp_server, "call_tool", noisy)
    lines = [
        "not json",
        json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]),
        "",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "memory_search", "arguments": {"query": "power"}}}),
    ]
    out = io.StringIO()
    assert serve(io.StringIO("\n".join(lines) + "\n"), out) == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in responses] == [None, None, 2]
    assert responses[0]["error"]["code"] == -32700 and responses[1]["error"]["code"] == -32600
    assert "Agent-Aware Power Manager" in responses[2]["result"]["content"][0]["text"]
    err = capsys.readouterr().err
    assert "stray debug output" in err and "ready (search/show read-only, remember append-only, stdio)" in err


def test_full_session_over_stdio_subprocess(db):
    """Replay a real client session against `room mcp` in a child process.

    memory_remember is not called here: in a real child it would start a real
    `room memory process`. test_mcp_remember.py replays all three tools in-process
    with the subprocess mocked.
    """
    _, ids = db
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "replay", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "memory_search", "arguments": {"query": "power manager", "limit": 5}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "memory_show", "arguments": {"id": ids["Agent-Aware Power Manager"]}}},
        {"jsonrpc": "2.0", "id": 5, "method": "sampling/createMessage", "params": {}},
        {"jsonrpc": "2.0", "id": 6, "method": "ping"},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "room.cli", "mcp"],
        input="".join(json.dumps(r) + "\n" for r in requests),  # stdin closes after the last line
        capture_output=True, text=True, timeout=30, env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines()]  # every stdout line is JSON-RPC
    assert [r["id"] for r in responses] == [1, 2, 3, 4, 5, 6]  # notification got no reply
    assert all(r["jsonrpc"] == "2.0" for r in responses)
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in responses[1]["result"]["tools"]] == ["memory_search", "memory_show", "memory_remember", "working_search", "session_latest"]
    assert "#" in responses[2]["result"]["content"][0]["text"] and "Power Manager" in responses[2]["result"]["content"][0]["text"]
    assert "Keeps the Mac awake" in responses[3]["result"]["content"][0]["text"]
    assert responses[4]["error"]["code"] == -32601
    assert responses[5]["result"] == {}
    assert "ready (search/show read-only, remember append-only, stdio)" in proc.stderr
