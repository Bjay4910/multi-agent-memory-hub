"""The rich, fictional demo dataset for the dashboard (room.web.demo_data). load_demo and the small set are unchanged."""

from collections import defaultdict
from datetime import datetime

import pytest

from room.memory import quality
from room.memory.demo import load_demo
from room.memory.readonly import ReadOnlyMemory
from room.memory.redact import find_secrets, redact_secrets
from room.memory.store import NODE_TYPES, RELATIONS

LISTED_TYPES = {"project", "application", "person", "organization", "skill", "decision", "preference", "document", "task"}


@pytest.fixture(scope="module")
def rich(tmp_path_factory):
    from room.web import demo_data

    home = tmp_path_factory.mktemp("rich-home")
    demo_data.load_rich_demo(home).close()
    store = ReadOnlyMemory(home / "memory.db")
    yield store
    store.close()


def test_node_and_edge_counts(rich):
    nodes = rich.all_nodes(include_superseded=True)
    edges = rich.all_edges(include_superseded=True)
    assert 90 <= len(nodes) <= 140, len(nodes)
    assert 120 <= len(edges) <= 260, len(edges)


def test_some_nodes_changed_over_time(rich):
    superseded = [n for n in rich.all_nodes(include_superseded=True) if n.status == "superseded"]
    assert len(superseded) >= 8
    assert {n.type for n in superseded} >= {"decision", "preference"}
    supersedes = [e for e in rich.all_edges(include_superseded=True) if e.relation == "supersedes"]
    assert len(supersedes) == len(superseded)


def test_all_nine_listed_types_and_only_known_types(rich):
    types = {n.type for n in rich.all_nodes(include_superseded=True)}
    assert types >= LISTED_TYPES
    assert types <= set(NODE_TYPES)


def test_every_relation_is_allowed(rich):
    assert {e.relation for e in rich.all_edges(include_superseded=True)} <= set(RELATIONS)


def test_largest_component_holds_at_least_80_percent(rich):
    nodes = [n.id for n in rich.all_nodes(include_superseded=True)]
    adj = defaultdict(set)
    for e in rich.all_edges(include_superseded=True):
        adj[e.src].add(e.dst)
        adj[e.dst].add(e.src)
    seen, best = set(), 0
    for start in nodes:
        if start in seen:
            continue
        stack, size = [start], 0
        seen.add(start)
        while stack:
            cur = stack.pop()
            size += 1
            for nxt in adj[cur] - seen:
                seen.add(nxt)
                stack.append(nxt)
        best = max(best, size)
    assert best >= 0.8 * len(nodes), (best, len(nodes))


def test_provenance_spans_three_agents_many_sessions_and_about_three_months(rich):
    rows = [r for n in rich.all_nodes(include_superseded=True) for r in rich.provenance(n.id)]
    assert {r["agent"] for r in rows} >= {"claude", "gemini", "hermes"}
    assert len({r["session_id"] for r in rows}) >= 8
    days = [datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) for r in rows]
    assert 60 <= (max(days) - min(days)).days <= 100


def test_confidence_varies(rich):
    values = {round(n.confidence, 2) for n in rich.all_nodes(include_superseded=True)}
    assert len(values) >= 5 and min(values) < 0.75 and max(values) > 0.9


def test_marked_as_demo(rich):
    assert rich.is_demo


def test_no_secret_or_secret_like_text_anywhere(rich):
    """The existing filters, used read-only, find nothing in any name, summary or alias."""
    hits = []
    for n in rich.all_nodes(include_superseded=True):
        node = {"type": n.type, "name": n.name, "summary": n.summary, "evidence": n.summary}
        for text in (n.name, n.summary, n.aliases):
            if quality.has_credential(text) or find_secrets(text) or redact_secrets(text) != text:
                hits.append((n.name, text))
        if quality.has_secret(node) or quality.talks_about_secrets(node):
            hits.append((n.name, "secret talk"))
    assert hits == []


def test_names_are_unique_per_type_and_nothing_is_empty(rich):
    active = rich.all_nodes()
    assert len({(n.type, n.name) for n in active}) == len(active)
    assert all(n.name.strip() and n.summary.strip() for n in rich.all_nodes(include_superseded=True))


def test_the_small_demo_is_unchanged(tmp_path):
    s = load_demo(tmp_path).stats()
    assert (s["nodes_active"], s["nodes_superseded"], s["edges"], s["sessions"]) == (14, 2, 14, 2)


# -- the API's demo_set -------------------------------------------------------------------------------------------

def _node_count(server):
    import json
    import threading
    import urllib.request

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats") as r:
            stats = json.load(r)
        return stats["nodes_active"] + stats["nodes_superseded"]
    finally:
        server.shutdown()
        server.server_close()


def test_make_server_demo_defaults_to_the_small_set():
    from room.web import api

    assert _node_count(api.make_server("127.0.0.1", 0, mode="demo")) == 16


def test_make_server_can_serve_the_rich_set():
    from room.web import api

    assert 90 <= _node_count(api.make_server("127.0.0.1", 0, mode="demo", demo_set="rich")) <= 140


def test_make_server_rejects_an_unknown_demo_set():
    from room.web import api

    with pytest.raises(ValueError):
        api.make_server("127.0.0.1", 0, mode="demo", demo_set="huge")


def test_cli_demo_set_defaults_to_rich_and_is_passed_to_serve(monkeypatch):
    from room import cli
    from room.web import api

    assert cli.build_parser().parse_args(["dashboard-api"]).demo_set == "rich"
    calls = []
    monkeypatch.setattr(api, "serve", lambda port, demo, **kw: calls.append((port, demo, kw.get("demo_set", "rich"))) or 0)
    assert cli.main(["dashboard-api", "--demo"]) == 0
    assert cli.main(["dashboard-api", "--demo", "--demo-set", "small"]) == 0
    assert calls == [(8766, True, "rich"), (8766, True, "small")]


def test_serve_defaults_to_the_rich_set():
    import inspect

    from room.web import api

    assert inspect.signature(api.serve).parameters["demo_set"].default == "rich"
