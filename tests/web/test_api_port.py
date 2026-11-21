"""Dashboard API port: default 8766, and a clear refusal when the port is already in use on 127.0.0.1."""

import socket

import pytest

from room import cli
from room.web import api


def _listener(family=socket.AF_INET, host="127.0.0.1", dual_stack=False):
    sock = socket.socket(family, socket.SOCK_STREAM)
    if dual_stack:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind((host, 0))
    sock.listen(1)
    return sock


def test_default_port_is_8766():
    assert api.DEFAULT_PORT == 8766
    assert cli.build_parser().parse_args(["dashboard-api"]).port == 8766


def test_refuses_a_port_already_in_use_on_127_0_0_1(demo_db, tmp_path):
    busy = _listener()
    port = busy.getsockname()[1]
    try:
        with pytest.raises(api.PortInUse) as err:
            api.make_server("127.0.0.1", port, db_path=demo_db, presence_dir=tmp_path / "p")
        message = str(err.value)
        assert f"127.0.0.1:{port}" in message and "--port" in message
        assert "other interfaces" in message
    finally:
        busy.close()


def test_refuses_a_port_held_by_a_dual_stack_ipv6_wildcard_listener(demo_db, tmp_path):
    """The case seen in F1: `python3 -m http.server` on *:PORT (IPv6, dual stack) also answers 127.0.0.1:PORT."""
    try:
        busy = _listener(socket.AF_INET6, "::", dual_stack=True)
    except OSError:
        pytest.skip("no dual-stack IPv6 on this machine")
    port = busy.getsockname()[1]
    try:
        with pytest.raises(api.PortInUse):
            api.make_server("127.0.0.1", port, db_path=demo_db, presence_dir=tmp_path / "p")
    finally:
        busy.close()


def test_cli_reports_a_busy_port_and_exits_nonzero(capsys):
    busy = _listener()
    port = busy.getsockname()[1]
    try:
        code = cli.main(["dashboard-api", "--demo", "--port", str(port)])
    finally:
        busy.close()
    err = capsys.readouterr().err
    assert code == 1
    assert f"127.0.0.1:{port}" in err and "already in use" in err and "Traceback" not in err


def test_a_free_port_still_works(demo_db, tmp_path):
    probe = _listener()
    port = probe.getsockname()[1]
    probe.close()
    server = api.make_server("127.0.0.1", port, db_path=demo_db, presence_dir=tmp_path / "p")
    try:
        assert server.server_address == ("127.0.0.1", port)
    finally:
        server.server_close()
