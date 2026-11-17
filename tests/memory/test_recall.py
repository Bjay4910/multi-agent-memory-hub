from datetime import datetime, timezone

from room.memory.recall import CHARS_PER_TOKEN, HEADER, rank, recall

from .conftest import add

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def n(type, name, summary="", confidence=0.8):
    return {"type": type, "name": name, "summary": summary, "confidence": confidence}


def test_empty_store_gives_empty_block(mem):
    assert recall(mem, "anything at all").block == ""
    assert recall(mem, "").block == ""


def test_hits_neighbours_and_links(mem):
    add(mem, [n("project", "Power Manager", "Keeps the Mac awake while agents work."),
              n("skill", "Rust", "Systems language."),
              n("person", "Jordan", "The user.")],
        [{"src": "Power Manager", "relation": "requires", "dst": "Rust"},
         {"src": "Jordan", "relation": "works_on", "dst": "Power Manager"}], ts="2026-11-10T00:00:00Z")
    ranked = rank(mem, "power manager status", now=NOW)
    assert ranked[0].node.name == "Power Manager" and ranked[0].via is None
    assert {s.node.name for s in ranked[1:]} == {"Rust", "Jordan"}
    assert all(s.via is not None for s in ranked[1:])
    block = recall(mem, "power manager status", now=NOW).block
    assert block.startswith(HEADER + "\n- Power Manager (project, 2026-11-10): Keeps the Mac awake")
    assert "Links:" in block and "- Power Manager requires Rust" in block and "- Jordan works_on Power Manager" in block


def test_superseded_nodes_never_recalled(mem):
    add(mem, [n("fact", "Thesis deadline", "Due 1 March.")])
    add(mem, [n("fact", "Thesis deadline", "Due 15 April.")])
    block = recall(mem, "thesis deadline", now=NOW).block
    assert "15 April" in block and "1 March" not in block


def test_recency_and_confidence_order_equal_matches(mem):
    add(mem, [n("task", "Email advisor old", "email advisor", 0.9)], ts="2025-01-01T00:00:00Z")
    add(mem, [n("task", "Email advisor new", "email advisor", 0.9)], ts="2026-11-16T00:00:00Z")
    add(mem, [n("task", "Email advisor unsure", "email advisor", 0.1)], ts="2026-11-16T00:00:00Z")
    names = [s.node.name for s in rank(mem, "email advisor", now=NOW)]
    assert names.index("Email advisor new") < names.index("Email advisor old")
    assert names.index("Email advisor new") < names.index("Email advisor unsure")


def test_top_k_limits_direct_hits(mem):
    add(mem, [n("skill", f"Graph skill {i}", "graph work") for i in range(20)])
    assert len(rank(mem, "graph", k=8, now=NOW)) == 8
    assert len(rank(mem, "graph", k=3, now=NOW)) == 3


def test_token_budget_caps_block(mem):
    add(mem, [n("fact", f"Graph fact {i}", "graph " + "x" * 200) for i in range(20)])
    for budget in (40, 100, 800):
        block = recall(mem, "graph", k=20, token_budget=budget, now=NOW).block
        assert len(block) <= budget * CHARS_PER_TOKEN
    assert recall(mem, "graph", token_budget=5, now=NOW).block == ""  # nothing fits


def test_budget_from_env(mem, monkeypatch):
    add(mem, [n("fact", f"Graph fact {i}", "graph " + "y" * 100) for i in range(10)])
    monkeypatch.setenv("MEMORY_TOKEN_BUDGET", "60")
    assert len(recall(mem, "graph", now=NOW).block) <= 240
    monkeypatch.setenv("MEMORY_TOKEN_BUDGET", "nonsense")
    assert len(recall(mem, "graph", now=NOW).block) > 240  # falls back to 800
