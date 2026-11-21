"""Synthetic presence for the demo: opt-in (make_server(demo_presence=True), used by `room dashboard-api --demo`),
demo mode only. An existing test pins demo-mode presence to [] by default, so the default is unchanged."""

import json
import threading

import pytest

from room.presence import write_presence
from web.conftest import Client


@pytest.fixture
def serve_with():
    """serve_with(**make_server kwargs) -> Client, for demo-mode servers that build their own temp home."""
    from room.web import api

    servers = []

    def start(**kwargs):
        server = api.make_server("127.0.0.1", 0, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return Client(server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_demo_presence_is_a_fixed_synthetic_list(serve_with):
    client = serve_with(mode="demo", demo_presence=True)
    status, body = client.get("/api/presence")
    assert status == 200 and body["synthetic"] is True
    agents = {a["agent"]: a for a in body["agents"]}
    assert agents["claude"]["effective_status"] == "working" and agents["claude"]["age_seconds"] == 12
    assert agents["gemini"]["effective_status"] == "idle" and agents["gemini"]["age_seconds"] == 140
    assert agents["hermes"]["effective_status"] == "offline"
    assert client.get("/api/health")[1]["mode"] == "demo"
    assert all("task" not in a for a in body["agents"])


def test_demo_presence_is_off_by_default(serve_with):
    _, body = serve_with(mode="demo").get("/api/presence")
    assert body == {"agents": []}


def test_local_mode_never_returns_the_synthetic_list(demo_db, tmp_path):
    from room.web import api

    with pytest.raises(ValueError):
        api.make_server("127.0.0.1", 0, db_path=demo_db, presence_dir=tmp_path / "p", mode="local", demo_presence=True)


def test_local_presence_is_real_and_never_shows_task_text(serve, demo_db, tmp_path):
    pdir = tmp_path / "presence"
    write_presence("claude", "working", "made-up private task words", pdir)
    _, body = serve(demo_db, pdir).get("/api/presence")
    assert "synthetic" not in body
    assert [a["agent"] for a in body["agents"]] == ["claude"]
    assert body["agents"][0]["age_seconds"] < 60  # real, not the synthetic 12
    assert "made-up private task words" not in json.dumps(body)


def test_cli_demo_turns_demo_presence_on(monkeypatch):
    from room.web import api

    seen = {}
    real = api.make_server

    def fake_make_server(host, port, **kw):
        seen.update(kw)
        raise KeyboardInterrupt  # stop before serving

    monkeypatch.setattr(api, "make_server", fake_make_server)
    try:
        api.serve(port=0, demo=True)
    except KeyboardInterrupt:
        pass
    assert seen.get("demo_presence") is True and seen.get("mode") == "demo"
    assert real is not fake_make_server
