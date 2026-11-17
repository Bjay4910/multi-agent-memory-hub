import re

import pytest

from room.memory.brief import build_brief, write_brief
from room.memory.demo import load_demo
from room.memory.export import to_mermaid, write_graph
from room.memory.paths import RepoWriteError, project_root

from .conftest import add


def n(type, name, summary="", confidence=0.8):
    return {"type": type, "name": name, "summary": summary, "confidence": confidence}


@pytest.fixture
def filled(mem):
    add(mem, [
        n("person", "Jordan", "CS student."), n("skill", "Python", "4 years."),
        n("project", "Agent Room", "Multi-agent room; 165 tests."), n("organization", "TU Munich", "University."),
        n("application", "TU Munich MSc application", "Submitted."), n("application", "Freelance gig", "Pending."),
        n("decision", "Use SQLite", "Local-first storage."), n("task", "Email advisor", "This week."),
        n("preference", "City", "Berlin."), n("fact", "Thesis deadline", "1 March."),
        n("document", "Resume", "One page."),
    ], [{"src": "TU Munich MSc application", "relation": "applied_to", "dst": "TU Munich"},
        {"src": "Agent Room", "relation": "requires", "dst": "Python"}])
    add(mem, [n("fact", "Thesis deadline", "15 April.")])
    return mem


def test_brief_sections_and_no_ids(filled):
    text = build_brief(filled)
    for heading in ("## Profile", "## Skills", "## Projects and results", "## Decisions",
                    "## Applications (status per company or program)", "## Open tasks", "## Preferences", "## Key facts"):
        assert heading in text
    assert "- **Agent Room**: Multi-agent room; 165 tests.\n  - Uses: Python" in text
    assert "### TU Munich\n- TU Munich MSc application: Submitted." in text
    assert "### Other\n- Freelance gig: Pending." in text
    assert "15 April." in text and "1 March." not in text  # superseded fact excluded
    assert not re.search(r"\b(id|node)\s*[:#=]?\s*\d+\b", text, re.I)


def test_empty_brief(mem):
    assert "No memory recorded yet" in build_brief(mem)


def test_write_brief_default_location_and_repo_guard(filled, isolated_home):
    path = write_brief(filled)
    assert path.parent == isolated_home / "briefs" and path.name.startswith("applications-")
    assert oct(path.stat().st_mode)[-3:] == "600"
    with pytest.raises(RepoWriteError):
        write_brief(filled, out=project_root() / "brief.md")
    assert not (project_root() / "brief.md").exists()


def test_mermaid_and_html_export(filled, isolated_home):
    mermaid = to_mermaid(filled)
    assert mermaid.startswith("flowchart LR")
    assert "|applied_to|" in mermaid and ":::application" in mermaid
    assert "15 April" not in mermaid  # summaries are not in node labels
    md, html = write_graph(filled)
    assert md == isolated_home / "exports" / "graph.md" and html.suffix == ".html"
    assert "```mermaid" in md.read_text()
    page = html.read_text()
    assert page.startswith("<!doctype html>") and "<title>Memory Graph</title>" in page
    assert "mermaid@10.9.1" in page and "TU Munich" in page


def test_graph_export_of_real_memory_refuses_repo(filled):
    with pytest.raises(RepoWriteError):
        write_graph(filled, out=project_root() / "graph")


def test_html_escapes_content(mem):
    add(mem, [n("fact", "<script>alert(1)</script>", 'a "quote" & <b>')])
    _, html = write_graph(mem)
    page = html.read_text()
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_demo_database_is_separate_and_flagged(tmp_path, isolated_home):
    with load_demo(tmp_path / "demo") as demo:
        assert demo.is_demo and demo.path == tmp_path / "demo" / "memory.db"
        stats = demo.stats()
        assert stats["nodes_active"] >= 12 and stats["nodes_superseded"] == 2
        assert "Interview scheduled" in build_brief(demo)
    assert not (isolated_home / "memory.db").exists()
    with load_demo(tmp_path / "demo") as again:  # reloading starts fresh
        assert again.stats()["nodes_active"] == stats["nodes_active"]
