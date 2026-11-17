import json
import os
from pathlib import Path

import pytest

from conftest import FakeAgent
from room import cli
from room.memory.paths import project_root

from .conftest import ScriptedExtractor

EXTRACTION = {"nodes": [
    {"type": "person", "name": "Jordan", "summary": "Applying for a CS masters and jobs.", "confidence": 0.9},
    {"type": "project", "name": "Agent Room", "summary": "Local multi-agent room with graph memory.", "confidence": 0.9},
    {"type": "project", "name": "Power Manager", "summary": "Keeps the Mac awake while agents work.", "confidence": 0.9},
], "edges": [{"src": "Jordan", "relation": "works_on", "dst": "Agent Room"},
             {"src": "Jordan", "relation": "works_on", "dst": "Power Manager"}]}


@pytest.fixture
def fakes(monkeypatch):
    """Room agents are FakeAgents; the extractor (hermes) returns EXTRACTION."""
    calls = []
    built = {}

    def factory(name):
        if name == "hermes":
            built.setdefault("extractor", ScriptedExtractor("hermes", [EXTRACTION] * 10))
            return built["extractor"]
        return built.setdefault(name, FakeAgent(name, calls=calls))

    monkeypatch.setattr(cli, "agent_factory", factory)
    factory.calls = calls
    factory.built = built
    return factory


def room(*argv):
    return cli.main(list(argv))


def test_ask_extracts_then_recalls_memory(fakes, capsys, isolated_home):
    assert room("ask", "I'm applying for masters; projects: agent room and power manager", "--rounds", "1") == 0
    out = capsys.readouterr().out
    assert "(memory) stored by hermes" in out and "3 new" in out
    assert (isolated_home / "memory.db").exists()

    fakes.calls.clear()
    assert room("ask", "Which projects should I list on my power manager application?", "--rounds", "1", "--show-memory") == 0
    out = capsys.readouterr().out
    assert "--- injected memory ---\nRelevant memory (may be outdated, verify):" in out
    assert "Power Manager (project" in out
    # every called agent got the block at the start of its transcript view
    assert fakes.calls and all(seen[0][1] == "memory" and "Power Manager" in seen[0][2] for _, _, seen in fakes.calls)


def test_ask_no_memory_skips_recall_and_extraction(fakes, capsys, isolated_home):
    assert room("ask", "hello", "--rounds", "1", "--no-memory", "--show-memory") == 0
    out = capsys.readouterr().out
    assert "disabled with --no-memory" in out and "(memory) extracting" not in out
    assert not (isolated_home / "memory.db").exists()
    assert "extractor" not in fakes.built


def test_opt_in_agent_gets_memory_only_when_called(fakes, capsys):
    room("remember", "Jordan works on Agent Room")
    fakes.calls.clear()
    room("ask", "agent room", "--rounds", "1")
    assert {c[0] for c in fakes.calls} == {"claude", "gemini"}  # muse not called, so it saw nothing
    fakes.calls.clear()
    room("ask", "agent room", "--rounds", "1", "--with", "muse")
    muse_calls = [c for c in fakes.calls if c[0] == "muse"]
    assert muse_calls and muse_calls[0][2][0][1] == "memory"


def test_extraction_failure_never_fails_the_room(monkeypatch, capsys, isolated_home):
    def factory(name):
        if name == "hermes":
            return ScriptedExtractor("hermes", ["garbage", "more garbage"])
        return FakeAgent(name)

    monkeypatch.setattr(cli, "agent_factory", factory)
    assert room("ask", "q", "--rounds", "1") == 0
    out = capsys.readouterr().out
    assert "queued for `room memory process`" in out
    assert len(list((isolated_home / "memory_queue").glob("*.json"))) == 1


def test_remember_text_file_tags_and_redaction(fakes, capsys, isolated_home, tmp_path):
    note = tmp_path / "resume.md"
    note.write_text("Resume: built Agent Room. api_" "key=supersecretvalue123")
    assert room("remember", "Summary of a chat", "--tag", "resume", "--tag", "jobs", "--file", str(note)) == 0
    out = capsys.readouterr().out
    assert "Saved note manual-" in out and "(memory) stored by hermes" in out
    session_file = next((isolated_home / "sessions").glob("manual-*.jsonl"))
    line = json.loads(session_file.read_text())
    assert line["agent"] == "manual" and line["content"].startswith("Tags: resume, jobs")
    assert "supersecretvalue123" not in session_file.read_text()
    prompt = fakes.built["extractor"].prompts[0][1]
    assert "supersecretvalue123" not in prompt and "built Agent Room" in prompt
    room("memory", "show", "Agent Room")
    assert "source: manual manual-" in capsys.readouterr().out


def test_remember_requires_content(capsys, tmp_path):
    assert room("remember") == 2
    assert room("remember", "--file", str(tmp_path / "missing.md")) == 2


def test_memory_commands(fakes, capsys):
    assert room("memory", "stats") == 0
    assert "No memory yet" in capsys.readouterr().out
    room("remember", "note")
    capsys.readouterr()

    assert room("memory", "stats") == 0
    out = capsys.readouterr().out
    assert "3 active, 0 superseded" in out and "1 manual notes" in out and "by type:  person 1, project 2" in out

    assert room("memory", "search", "power manager") == 0
    out = capsys.readouterr().out
    assert "project      Power Manager" in out
    node_id = int(out.split()[0].lstrip("#"))

    assert room("memory", "show", str(node_id)) == 0
    out = capsys.readouterr().out
    assert "Power Manager  [project, active" in out and "<- works_on Jordan" in out

    assert room("memory", "show", "nobody") == 1
    assert room("memory", "forget", str(node_id)) == 0
    assert "Forgot" in capsys.readouterr().out
    assert room("memory", "forget", str(node_id)) == 1
    room("memory", "search", "power manager")
    assert "No matches" in capsys.readouterr().out


def test_memory_process_retries_queue(monkeypatch, capsys, isolated_home):
    def failing(name):
        return ScriptedExtractor("hermes", ["bad", "bad"]) if name == "hermes" else FakeAgent(name)

    monkeypatch.setattr(cli, "agent_factory", failing)
    room("ask", "q", "--rounds", "1")
    capsys.readouterr()
    assert room("memory", "process") == 1  # still failing
    monkeypatch.setattr(cli, "agent_factory", lambda n: ScriptedExtractor("hermes", [EXTRACTION]))
    assert room("memory", "process") == 0
    assert "stored by hermes" in capsys.readouterr().out
    assert room("memory", "process") == 0 and "Nothing queued" in capsys.readouterr().out


def test_purge_needs_confirmation(fakes, capsys, monkeypatch, isolated_home):
    room("remember", "note")
    room("brief")
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert room("memory", "purge") == 1
    assert (isolated_home / "memory.db").exists()

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert room("memory", "purge") == 1
    monkeypatch.setattr("builtins.input", lambda prompt: "purge")
    assert room("memory", "purge") == 0
    assert not (isolated_home / "memory.db").exists() and not (isolated_home / "briefs").exists()
    assert room("memory", "purge", "--yes") == 0 and "No memory to purge" in capsys.readouterr().out


def test_brief_and_graph_commands(fakes, capsys, isolated_home):
    room("remember", "note")
    capsys.readouterr()
    assert room("brief", "--for", "applications") == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.parent == isolated_home / "briefs" and "## Projects and results" in path.read_text()
    assert room("graph") == 0
    out = capsys.readouterr().out
    assert str(isolated_home / "exports" / "graph.md") in out
    assert room("brief", "--out", str(project_root() / "leak.md")) == 2
    assert room("graph", "--out", str(project_root() / "leak")) == 2
    assert "refusing to write memory data inside the repository" in capsys.readouterr().err


def test_demo_uses_separate_home(capsys, tmp_path, isolated_home):
    assert room("memory", "demo", "--home", str(tmp_path / "demo")) == 0
    out = capsys.readouterr().out
    assert f"export AGENT_ROOM_HOME={tmp_path / 'demo'}" in out and "made-up demo memory" in out
    assert (tmp_path / "demo" / "memory.db").exists() and not (isolated_home / "memory.db").exists()


def test_eval_command(capsys, tmp_path):
    assert room("memory", "eval") == 0
    out = capsys.readouterr().out
    assert "recall@3" in out and "recall@8" in out and "precision@3" in out and "PASS" in out
    assert room("memory", "eval", "--dir", str(tmp_path)) == 2


def test_cli_parses_memory_commands():
    p = cli.build_parser()
    assert p.parse_args(["memory", "search", "x", "--all", "-k", "3"]).k == 3
    assert p.parse_args(["memory", "forget", "7"]).node_id == 7
    assert p.parse_args(["brief"]).purpose == "applications"
    assert p.parse_args(["ask", "q", "--show-memory", "--no-memory"]).no_memory
    with pytest.raises(SystemExit):
        p.parse_args(["memory", "forget", "abc"])
    with pytest.raises(SystemExit):
        p.parse_args(["brief", "--for", "dating"])


def _repo_snapshot():
    root = project_root()
    skip = {".git", ".venv", "__pycache__", ".pytest_cache"}
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.endswith(".egg-info")]
        for f in filenames:
            p = Path(dirpath) / f
            files[str(p.relative_to(root))] = p.stat().st_mtime_ns
    return files


def test_nothing_is_written_inside_the_repo(fakes, capsys, monkeypatch, tmp_path):
    before = _repo_snapshot()
    monkeypatch.chdir(project_root())  # even when run from the repo directory
    room("ask", "I'm applying for masters; projects: agent room and power manager", "--rounds", "1", "--show-memory")
    room("remember", "note", "--tag", "x")
    for argv in (["memory", "stats"], ["memory", "search", "agent"], ["memory", "show", "Agent Room"],
                 ["memory", "process"], ["brief"], ["graph"], ["memory", "eval"], ["status"],
                 ["memory", "demo", "--home", str(tmp_path / "demo")], ["memory", "purge", "--yes"]):
        room(*argv)
    assert _repo_snapshot() == before
