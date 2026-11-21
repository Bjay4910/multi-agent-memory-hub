"""Helpers for the dashboard API tests: a server on an ephemeral 127.0.0.1 port and a raw HTTP client."""

import http.client
import json
import threading

import pytest

from room.memory.demo import load_demo


class Client:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, host=None, headers=None, body=None):
        """(status, headers dict with lower-case keys, raw body bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not False:
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        data = resp.read()
        result = resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        conn.close()
        return result

    def get(self, path, **kw):
        status, headers, body = self.request("GET", path, **kw)
        return status, (json.loads(body) if body else None)


@pytest.fixture
def serve():
    """serve(db_path, presence_dir, mode="local") -> Client; every server is shut down after the test."""
    from room.web import api

    servers = []

    def start(db_path, presence_dir, mode="local"):
        server = api.make_server("127.0.0.1", 0, db_path=db_path, presence_dir=presence_dir, mode=mode)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return Client(server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def demo_db(tmp_path):
    """The made-up demo dataset in its own temp home."""
    home = tmp_path / "demo-home"
    home.mkdir()
    load_demo(home).close()
    return home / "memory.db"
