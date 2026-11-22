"""A client that hangs up early must not make the server print a traceback; other errors still report."""

import socket
import struct
import sys
import threading
import time

import pytest

from room.memory.store import MemoryStore, Provenance


@pytest.fixture
def big_db(tmp_path):
    db = tmp_path / "big" / "memory.db"
    db.parent.mkdir()
    with MemoryStore(db) as store:
        # names long enough that a full graph response (~2 MB) is bigger than the socket buffer
        store.apply_extraction({"nodes": [{"type": "fact", "name": f"A long fact name {i} " + "x" * 900, "summary": "x",
                                           "confidence": 0.5} for i in range(2000)], "edges": []},
                               Provenance("s", "claude", "session"))
    return db


def _start(db, tmp_path):
    from room.web import api

    server = api.make_server("127.0.0.1", 0, db_path=db, presence_dir=tmp_path / "p")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _hang_up(port, path, wait):
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    time.sleep(wait)  # with a wait, the server is mid-write (the response is bigger than the socket buffer)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with a reset
    s.close()


def test_hang_ups_print_nothing_and_the_server_keeps_working(big_db, tmp_path, capsys):
    server = _start(big_db, tmp_path)
    port = server.server_address[1]
    try:
        for i in range(12):
            _hang_up(port, "/api/graph?limit=2000&status=all", 0.15)  # mid-response
            _hang_up(port, "/api/nodes?limit=200", 0.0)  # before the response
        time.sleep(1.0)
        from web.conftest import Client

        assert Client(port).request("GET", "/api/health")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err and "BrokenPipe" not in captured.err and "ConnectionReset" not in captured.err
    assert captured.err == "" and captured.out == ""


@pytest.mark.parametrize("exc", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_handle_error_is_silent_only_for_hang_ups(exc, demo_db, tmp_path, capsys):
    from room.web import api

    server = api.make_server("127.0.0.1", 0, db_path=demo_db, presence_dir=tmp_path / "p")
    try:
        try:
            raise exc("client went away")
        except exc:
            server.handle_error(None, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        try:
            raise RuntimeError("a real bug")
        except RuntimeError:
            server.handle_error(None, ("127.0.0.1", 1))
        err = capsys.readouterr().err
        assert "Traceback" in err and "a real bug" in err
    finally:
        server.server_close()


def test_internal_errors_still_return_the_plain_500(serve, demo_db, tmp_path, monkeypatch):
    from room.memory.readonly import ReadOnlyMemory

    monkeypatch.setattr(ReadOnlyMemory, "stats", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    status, headers, body = serve(demo_db, tmp_path / "p").request("GET", "/api/stats")
    assert status == 500 and body == b'{"error": "internal error"}'
