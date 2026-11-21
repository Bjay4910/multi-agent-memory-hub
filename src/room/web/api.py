"""A read-only JSON API over local memory, for the dashboard. Standard library only.

Safety, by design:

- It binds 127.0.0.1 only and refuses any other address.
- Only GET and HEAD are served; every other method gets 405 and touches nothing.
- The Host header must be ``localhost`` or ``127.0.0.1`` (optionally with this server's port), so a page on
  another site cannot reach it through DNS rebinding. No CORS headers are ever sent.
- The database is opened through :class:`ReadOnlyMemory` (SQLite ``mode=ro`` plus ``query_only``) for each
  request and closed after it.
- Input is validated (ids, limits, types, status, query length); errors are short JSON messages, never stack
  traces. Request lines are not logged, so a query never lands in a log.
- The graph list carries no summaries, and presence entries carry no task text.

``room dashboard-api --demo`` serves the made-up demo dataset from a new temp home; in demo mode nothing is
read from the real ``~/.agent-room``.
"""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import stat
import sys
import tempfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..memory.readonly import MemoryUnavailable, ReadOnlyMemory
from ..presence import read_presence

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_GRAPH_LIMIT = 500
MAX_GRAPH_LIMIT = 2000
SEARCH_LIMIT = 25
MAX_QUERY_CHARS = 200
_MAX_ID = 2**63 - 1  # SQLite's largest integer
_ALLOWED_HOSTS = ("localhost", "127.0.0.1")
_STATUSES = ("active", "superseded", "all")
_TYPE = re.compile(r"^[a-z][a-z_]{0,31}$")
_DIGITS = re.compile(r"^[0-9]{1,19}$")


class BadRequest(ValueError):
    """Invalid input; the message is shown to the client."""


class PortInUse(OSError):
    """The port already answers on 127.0.0.1; the message is meant for people."""

    def __init__(self, port: int) -> None:
        super().__init__(
            f"port {port} is already in use on {BIND_HOST}:{port}; another process answers there, so requests "
            f"could reach it instead of this API. Stop that process or pick another port with --port. (Other "
            f"processes may still listen on this port on other interfaces; this API only checks {BIND_HOST}.)")


def _answers(port: int) -> bool:
    """True if something accepts connections on 127.0.0.1:``port`` (including an IPv6 dual-stack wildcard)."""
    try:
        with socket.create_connection((BIND_HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


class NotFound(LookupError):
    """No such node or path."""


@dataclass(frozen=True)
class Config:
    db_path: Path
    presence_dir: Path
    mode: str  # "local" or "demo"
    port: int


def open_memory(db_path: Path) -> ReadOnlyMemory:
    """The only way this API opens the database: read-only."""
    return ReadOnlyMemory(db_path)


# -- input ---------------------------------------------------------------------------------------------------------

def _positive_int(text: str, what: str) -> int:
    if not _DIGITS.match(text or ""):
        raise BadRequest(f"{what} must be a positive integer")
    value = int(text)
    if value < 1 or value > _MAX_ID:
        raise BadRequest(f"{what} must be a positive integer")
    return value


def _one(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    if len(values) > 1:
        raise BadRequest(f"'{key}' may be given only once")
    return values[0]


# -- endpoints -----------------------------------------------------------------------------------------------------

def health(cfg: Config, params: dict) -> dict:
    with open_memory(cfg.db_path) as store:
        return {"ok": True, "mode": cfg.mode, "schema_version": store.schema_version()}


def graph(cfg: Config, params: dict) -> dict:
    limit_text = _one(params, "limit")
    limit = DEFAULT_GRAPH_LIMIT if limit_text is None else _positive_int(limit_text, "limit")
    if limit > 10**9:
        raise BadRequest("limit is too large")
    limit = min(limit, MAX_GRAPH_LIMIT)
    status = _one(params, "status") or "active"
    if status not in _STATUSES:
        raise BadRequest("status must be one of: active, superseded, all")
    types_text = _one(params, "types")
    types = None
    if types_text:
        types = set(types_text.split(","))
        if not all(_TYPE.match(t) for t in types):
            raise BadRequest("types must be a comma-separated list of node types")
    with open_memory(cfg.db_path) as store:
        nodes = store.all_nodes(include_superseded=status != "active")
        edges = store.all_edges(include_superseded=status != "active")
    nodes = [n for n in nodes if (status == "all" or n.status == status) and (types is None or n.type in types)]
    nodes = nodes[:limit]
    ids = {n.id for n in nodes}
    edges = [e for e in edges if e.src in ids and e.dst in ids]
    degree = dict.fromkeys(ids, 0)
    for e in edges:
        degree[e.src] += 1
        degree[e.dst] += 1
    return {
        "nodes": [{"id": n.id, "type": n.type, "name": n.name, "status": n.status, "confidence": n.confidence,
                   "degree": degree[n.id]} for n in nodes],
        "edges": [{"id": e.id, "src": e.src, "dst": e.dst, "relation": e.relation} for e in edges],
    }


def node(cfg: Config, params: dict, node_id: int) -> dict:
    with open_memory(cfg.db_path) as store:
        n = store.get_node(node_id)
        if n is None:
            raise NotFound("no node with that id")
        provenance = [{"agent": r["agent"], "session_id": r["session_id"], "source": r["source"], "ts": r["ts"]}
                      for r in store.provenance(node_id)]
        neighbors = [{"id": other.id, "name": other.name, "type": other.type, "relation": edge.relation,
                      "direction": "out" if edge.src == node_id else "in"} for edge, other in store.edges_of(node_id)]
    return {
        "id": n.id, "type": n.type, "name": n.name, "summary": n.summary,
        "aliases": [a for a in n.aliases.split("|") if a], "status": n.status, "confidence": n.confidence,
        "created_at": n.created_at, "updated_at": n.updated_at, "provenance": provenance, "neighbors": neighbors,
    }


def search(cfg: Config, params: dict) -> dict:
    q = _one(params, "q")
    if q is None or not q.strip():
        raise BadRequest("q is required")
    if len(q) > MAX_QUERY_CHARS:
        raise BadRequest(f"q may be at most {MAX_QUERY_CHARS} characters")
    with open_memory(cfg.db_path) as store:
        results = store.search(q, SEARCH_LIMIT)
    return {"results": [{"id": n.id, "type": n.type, "name": n.name, "status": n.status,
                         "confidence": n.confidence, "relevance": round(score, 4)} for n, score in results]}


def presence(cfg: Config, params: dict) -> dict:
    return {"agents": [{"agent": e.agent, "effective_status": e.effective_status(),
                        "age_seconds": round(e.age_seconds(), 1), "updated_at": e.updated_at.isoformat()}
                       for e in read_presence(cfg.presence_dir)]}


def stats(cfg: Config, params: dict) -> dict:
    from ..memory.extractor import DEFAULT_EXTRACTOR

    with open_memory(cfg.db_path) as store:
        s = store.stats()
        read_only = store.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        demo = store.is_demo
    return {
        "nodes_by_type": s["by_type"],
        "nodes_active": s["nodes_active"],
        "nodes_superseded": s["nodes_superseded"],
        "edges": s["edges"],
        "edges_by_relation": s["by_relation"],
        "sessions": s["sessions"],
        "privacy": {  # each read from the running system, none written in
            "database_read_only": read_only,
            "database_file_mode": oct(stat.S_IMODE(os.stat(cfg.db_path).st_mode)),
            "extractor": (os.environ.get("MEMORY_EXTRACTOR") or DEFAULT_EXTRACTOR).strip().lower(),
            "demo_data": demo,
            "bind_address": BIND_HOST,
        },
    }


_ROUTES = {"/api/health": health, "/api/graph": graph, "/api/search": search, "/api/presence": presence,
           "/api/stats": stats}


# -- HTTP ----------------------------------------------------------------------------------------------------------

def _host_ok(header: str | None, port: int) -> bool:
    if not header:
        return False
    host = header.strip().lower()
    for name in _ALLOWED_HOSTS:
        if host == name or host == f"{name}:{port}":
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "room-dashboard-api"
    sys_version = ""
    config: Config  # set by make_server

    def log_message(self, format, *args):  # noqa: A002 - no request logging: a query could hold anything
        pass

    def _send(self, status: int, payload: dict, extra: dict[str, str] | None = None, body: bool = True) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):  # malformed requests: JSON, no HTML, no details
        self.close_connection = True
        try:
            self._send(code, {"error": "bad request" if code < 500 else "server error"})
        except OSError:
            pass

    def _refuse_method(self) -> None:
        self.close_connection = True
        if not _host_ok(self.headers.get("Host"), self.config.port):
            self._send(403, {"error": "forbidden host"})
            return
        self._send(405, {"error": "method not allowed"}, {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse_method

    def do_GET(self) -> None:
        self._handle(body=True)

    def do_HEAD(self) -> None:
        self._handle(body=False)

    def _handle(self, body: bool) -> None:
        if not _host_ok(self.headers.get("Host"), self.config.port):
            self._send(403, {"error": "forbidden host"}, body=body)
            return
        try:
            status, payload = 200, self._route()
        except BadRequest as exc:
            status, payload = 400, {"error": str(exc)}
        except NotFound as exc:
            status, payload = 404, {"error": str(exc)}
        except MemoryUnavailable as exc:
            status, payload = 503, {"error": str(exc)}
            if self.path.split("?", 1)[0] == "/api/health":
                payload = {"ok": False, "mode": self.config.mode, "error": str(exc)}
        except Exception:  # noqa: BLE001 - never a stack trace in a response
            status, payload = 500, {"error": "internal error"}
        self._send(status, payload, body=body)

    def _route(self) -> dict:
        parts = urlsplit(self.path)
        try:
            params = parse_qs(parts.query, keep_blank_values=True, strict_parsing=False, max_num_fields=20)
        except ValueError:
            raise BadRequest("too many query parameters") from None
        path = parts.path
        if path.startswith("/api/node/"):
            rest = path[len("/api/node/"):]
            return node(self.config, params, _positive_int(rest, "id"))
        handler = _ROUTES.get(path)
        if handler is None:
            raise NotFound("no such endpoint")
        return handler(self.config, params)


def make_server(host: str, port: int, *, db_path: Path, presence_dir: Path, mode: str = "local") -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 (any other ``host`` is refused); ``port`` 0 picks a free one."""
    if host != BIND_HOST:
        raise ValueError(f"the dashboard API binds {BIND_HOST} only, not {host!r}")
    if mode not in ("local", "demo"):
        raise ValueError(f"unknown mode {mode!r}")
    if port and _answers(port):
        raise PortInUse(port)
    try:
        server = ThreadingHTTPServer((BIND_HOST, port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise PortInUse(port) from None
        raise
    server.daemon_threads = True
    config = Config(Path(db_path), Path(presence_dir), mode, server.server_address[1])
    server.RequestHandlerClass = type("BoundHandler", (Handler,), {"config": config})
    return server


def demo_home() -> Path:
    """A new temp home holding the made-up demo dataset."""
    from ..memory.demo import load_demo

    home = Path(tempfile.mkdtemp(prefix="agent-room-dashboard-demo-"))
    load_demo(home).close()
    return home


def serve(port: int = DEFAULT_PORT, demo: bool = False, host: str = BIND_HOST) -> int:
    """Run until interrupted. Demo mode serves a temp copy of the demo dataset and never reads the real home."""
    if host != BIND_HOST:
        raise ValueError(f"the dashboard API binds {BIND_HOST} only, not {host!r}")
    if port and _answers(port):  # before a demo home is created
        raise PortInUse(port)
    if demo:
        home = demo_home()
        os.environ["AGENT_ROOM_HOME"] = str(home)  # anything that asks for the home gets the temp one
        db_path, presence_dir, mode = home / "memory.db", home / "presence", "demo"
    else:
        from .. import paths
        from ..memory.paths import memory_db_path

        db_path, presence_dir, mode = memory_db_path(), paths.presence_dir(), "local"
    server = make_server(host, port, db_path=db_path, presence_dir=presence_dir, mode=mode)
    print(f"room dashboard API ({mode}) on http://{BIND_HOST}:{server.server_address[1]}/api/health "
          "(read-only; Ctrl-C to stop)", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
