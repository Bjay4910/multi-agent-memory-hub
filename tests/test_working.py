"""Working layer: raw, temporary, redacted session notes. Temp AGENT_ROOM_HOME only; no models, no network."""

import builtins
import hashlib
import json
import os
import stat
from datetime import timedelta

import pytest

from room import cli, mcp_server
from room.agents.base import Agent
from room.mcp_server import handle
from room.memory import working
from room.memory.store import MemoryStore

FAKE = "gh" "p_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6"
# sha256 of json.dumps(TOOLS[:3], sort_keys=True), recorded at commit 23bd69b before this change.
THREE_TOOLS_SHA = "e58d80aa8a1c2a45c8301df62cfd90bef96e398557ee50807fe72ad88a7b8b42"


def turns(*pairs, start=0):
    """[(role, text), ...] -> working turn dicts with stable keys."""
    return [{"key": f"k{start + i:04d}", "role": role, "ts": f"2026-11-18T10:{(start + i) % 60:02d}:00Z", "text": text}
            for i, (role, text) in enumerate(pairs)]


LONG_ENOUGH = turns(
    ("user", "I am planning the Zebra-Quartz project and want the docs to stay short and plain. " * 3),
    ("assistant", "Understood: short plain docs for Zebra-Quartz. I suggest a single README."),
    ("user", "Also remember I prefer pytest over unittest for this project, always."),
    ("assistant", "Noted: pytest for Zebra-Quartz."),
)


def save(source_key="sess-a", project="demo-proj", items=LONG_ENOUGH):
    return working.upsert("claude-code", source_key, project, items)


def files(home):
    return sorted(p for p in (home / "working").glob("*.json")) if (home / "working").is_dir() else []


# -- 14. the file -------------------------------------------------------------------------------

def test_note_fields_and_nothing_goes_to_long_term(isolated_home):
    status, note = save()
    assert status == "created"
    [path] = files(isolated_home)
    data = json.loads(path.read_text())
    assert path.stem == data["id"] and working.ID_RE.fullmatch(data["id"]) and data["id"].startswith("claude-code-")
    assert data["source"] == "claude-code" and data["project"] == "demo-proj"
    captured, expires = working.parse_ts(data["captured_at"]), working.parse_ts(data["expires_at"])
    assert expires - working.parse_ts(data["updated_at"]) == timedelta(days=working.DEFAULT_TTL_DAYS) == timedelta(days=14)
    assert abs((working.utcnow() - captured).total_seconds()) < 5
    assert [t["role"] for t in data["turns"]] == ["user", "assistant", "user", "assistant"]
    # Nothing is sent to Hermes or the graph: no db, no session log, no queue.
    for name in ("memory.db", "sessions", "memory_queue"):
        assert not (isolated_home / name).exists(), name


# -- 15. permissions and location ------------------------------------------------------------------

def test_permissions_dir_700_files_600(isolated_home):
    save()
    assert stat.S_IMODE((isolated_home / "working").stat().st_mode) == 0o700
    for p in (isolated_home / "working").iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name


def test_refuses_git_work_tree(tmp_path, monkeypatch):
    repo = tmp_path / "some-repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("AGENT_ROOM_HOME", str(repo / "data"))
    with pytest.raises(working.WorkingSafetyError, match="git work tree"):
        save()
    assert not (repo / "data").exists()


def test_refuses_this_repository(monkeypatch):
    from room.memory.paths import project_root
    monkeypatch.setenv("AGENT_ROOM_HOME", str(project_root() / "tmp-working-home"))
    with pytest.raises(working.WorkingSafetyError):
        save()
    assert not (project_root() / "tmp-working-home").exists()


def test_refuses_key_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))  # never the real key folder
    monkeypatch.setenv("AGENT_ROOM_HOME", str(tmp_path / "fakehome" / ".config" / "agent-keys" / "room"))
    with pytest.raises(working.WorkingSafetyError, match="key"):
        save()
    assert not (tmp_path / "fakehome").exists()


# -- 16. redaction before the write ---------------------------------------------------------------

def test_upsert_redacts_and_secret_never_on_disk(isolated_home):
    items = turns(("user", f"my token is {FAKE} please keep the Zebra-Quartz notes " * 4),
                  ("assistant", "ok"), ("user", "second message about Zebra-Quartz planning details, "
                                                "covering the release order, the owners and the open questions, "
                                                "plus the review dates, the rollback plan and who signs off on each step of the launch, "
                                                "and a short list of the risks we already know about and how we plan to watch for them"))
    assert any(FAKE in t["text"] for t in items)  # the secret is really in the input
    status, _ = save(items=items)
    assert status == "created"
    for p in isolated_home.rglob("*"):
        if p.is_file():
            assert FAKE.encode() not in p.read_bytes(), p


def test_refuses_when_secret_survives(isolated_home, monkeypatch):
    monkeypatch.setattr(working, "redact_secrets", lambda s: s)
    items = turns(("user", f"token {FAKE} and plenty of other words about the plan " * 5), ("user", "more words here"))
    status, note = save(items=items)
    assert status == "refused-secret" and note is None
    assert files(isolated_home) == []
    for p in isolated_home.rglob("*"):
        if p.is_file():
            assert FAKE.encode() not in p.read_bytes(), p


# -- 20. dedupe, resume, size cap -------------------------------------------------------------------

def test_dedupe_resume_and_short(isolated_home):
    assert save(items=turns(("user", "hi"), ("assistant", "hello")))[0] == "too-short"
    assert files(isolated_home) == []
    assert save()[0] == "created"
    assert save()[0] == "unchanged"
    more = turns(("user", "Resumed: the Zebra-Quartz deadline moved to Friday."), ("assistant", "Got it."), start=50)
    status, note = save(items=LONG_ENOUGH + more)
    assert status == "updated"
    [path] = files(isolated_home)
    data = json.loads(path.read_text())
    assert len(data["turns"]) == 6 and data["turns"][-2]["text"].startswith("Resumed:")
    assert len({t["text"] for t in data["turns"]}) == 6  # no duplicates


def test_size_cap_keeps_latest_turns(isolated_home):
    items = turns(*[("user" if i % 2 == 0 else "assistant", f"turn {i:03d} " + "x" * 1500) for i in range(20)])
    status, note = save(items=items)
    data = json.loads(files(isolated_home)[0].read_text())
    total = sum(len(t["text"]) for t in data["turns"])
    assert total <= working.MAX_NOTE_CHARS == 8000
    assert data["turns"][-1]["text"].startswith("turn 019") and data["dropped_turns"] > 0
    # dropped turns are not re-added on the next capture
    assert save(items=items)[0] == "unchanged"


# -- 17. expiry --------------------------------------------------------------------------------------

def test_expiry_hides_and_purge_removes(isolated_home, monkeypatch, capsys):
    _, old = save("old-session")
    real_now = working.utcnow()
    monkeypatch.setattr(working, "utcnow", lambda: real_now + timedelta(days=10))
    _, fresh = save("fresh-session", items=turns(*[(r, t.replace("Zebra", "Okapi")) for r, t in
                                                    [(x["role"], x["text"]) for x in LONG_ENOUGH]]))
    monkeypatch.setattr(working, "utcnow", lambda: real_now + timedelta(days=15))  # old expired, fresh not
    assert [n["id"] for n in working.list_notes()] == [fresh["id"]]
    assert working.get_note(old["id"]) is None
    assert working.search("Zebra-Quartz") == []
    assert [n["id"] for n in working.latest("claude-code", 5)] == [fresh["id"]]
    text, _ = mcp_call("working_search", query="Zebra-Quartz")
    assert old["id"] not in text
    text, _ = mcp_call("session_latest", agent="claude-code", n=5)
    assert old["id"] not in text and fresh["id"] in text
    assert len(files(isolated_home)) == 2  # reading never deletes

    assert cli.main(["working", "purge"]) == 0
    assert [p.stem for p in files(isolated_home)] == [fresh["id"]]
    assert cli.main(["working", "purge", "--all"]) == 0
    assert files(isolated_home) == []


# -- 18. commands ------------------------------------------------------------------------------------

def test_cli_list_show_search(isolated_home, capsys):
    _, note = save()
    capsys.readouterr()
    assert cli.main(["working", "list"]) == 0
    out = capsys.readouterr().out
    assert note["id"] in out and "demo-proj" in out and "expires" in out
    assert cli.main(["working", "show", note["id"]]) == 0
    out = capsys.readouterr().out
    assert "short plain docs" in out and "[user]" in out and "[assistant]" in out
    assert cli.main(["working", "search", "pytest", "unittest"]) == 0
    assert note["id"] in capsys.readouterr().out
    assert cli.main(["working", "show", "claude-code-20990101T000000Z-000000"]) == 1
    assert cli.main(["working", "show", "../../etc/passwd"]) == 2


class ScriptedHermes(Agent):
    name = "hermes"
    prompts = []

    async def complete(self, system, prompt, *, json_output=False, json_schema=None):
        ScriptedHermes.prompts.append(prompt)
        return json.dumps({"nodes": [{"type": "preference", "name": "Pytest over unittest",
                                      "summary": "Prefers pytest.", "confidence": 0.8}], "edges": []})

    async def healthcheck(self):
        return True, "scripted"


def test_promote_is_the_only_path_and_uses_extraction(isolated_home, monkeypatch, capsys):
    _, note = save()
    assert not (isolated_home / "memory.db").exists()
    ScriptedHermes.prompts = []
    monkeypatch.setattr(cli, "agent_factory", lambda name: ScriptedHermes())
    assert cli.main(["working", "promote", note["id"]]) == 0
    with MemoryStore() as store:
        [node] = [n for n in store.all_nodes() if n.name == "Pytest over unittest"]
        [prov] = store.provenance(node.id)
    assert prov["source"] == "claude-code" and prov["session_id"] == note["id"]
    log = (isolated_home / "sessions" / f"{note['id']}.jsonl").read_text().splitlines()
    roles = [(json.loads(line)["agent"], json.loads(line)["role"]) for line in log]
    assert roles == [("claude-code", "user"), ("claude", "agent"), ("claude-code", "user"), ("claude", "agent")]
    # default extraction scope: only the user's own words reach the extractor
    assert "pytest over unittest" in ScriptedHermes.prompts[0] and "I suggest a single README" not in ScriptedHermes.prompts[0]
    assert json.loads(files(isolated_home)[0].read_text())["promoted_at"]
    assert cli.main(["working", "promote", note["id"]]) == 1  # not twice
    assert cli.main(["working", "promote", "claude-code-20990101T000000Z-000000"]) == 1


# -- 19. MCP tools ------------------------------------------------------------------------------------

def mcp_call(name, **arguments):
    resp = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return resp["result"]["content"][0]["text"], resp["result"]["isError"]


def test_existing_three_tools_unchanged_and_two_new_read_only():
    tools = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert hashlib.sha256(json.dumps(tools[:3], sort_keys=True).encode()).hexdigest() == THREE_TOOLS_SHA
    assert [t["name"] for t in tools] == ["memory_search", "memory_show", "memory_remember", "working_search", "session_latest"]
    for t in tools[3:]:
        assert t["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
        assert "untrusted" in t["description"] and "not instructions" in t["description"]


def test_mcp_working_tools_output(isolated_home):
    _, note = save()
    text, is_error = mcp_call("working_search", query="pytest", limit=3)
    assert not is_error and text.startswith("Working notes (") and "untrusted" in text.splitlines()[0]
    assert note["id"] in text
    text, is_error = mcp_call("session_latest", agent="claude-code", n=1)
    assert not is_error and note["id"] in text and "short plain docs" in text
    assert mcp_call("working_search", query="nothing-matches-this")[0].startswith("No working notes match")


def test_mcp_working_tools_empty_home_creates_nothing(isolated_home):
    text, is_error = mcp_call("working_search", query="x")
    assert not is_error and "No working notes" in text
    text, _ = mcp_call("session_latest", agent="claude-code")
    assert "No working notes" in text
    assert not isolated_home.exists()


@pytest.mark.parametrize("name,args,expected", [
    ("working_search", {}, "'query' must be a non-empty string"),
    ("working_search", {"query": "x", "limit": 0}, "'limit'"),
    ("session_latest", {"agent": "hermes"}, "'agent' must be one of"),
    ("session_latest", {"n": 99}, "'n'"),
    ("session_latest", {"n": "1"}, "'n' must be an integer"),
])
def test_mcp_working_tools_bad_input(isolated_home, name, args, expected):
    text, is_error = mcp_call(name, **args)
    assert is_error and expected in text


def test_mcp_working_output_redacted_and_capped(isolated_home):
    _, note = save()
    path = files(isolated_home)[0]  # plant a secret directly, bypassing capture, to test output redaction
    data = json.loads(path.read_text())
    data["turns"][0]["text"] = f"planted {FAKE} " + "pytest " * 3000
    path.write_text(json.dumps(data))
    text, _ = mcp_call("session_latest", agent="claude-code", n=1)
    assert FAKE not in text and "[REDACTED]" in text and len(text) <= 8000
    text, _ = mcp_call("working_search", query="planted")
    assert FAKE not in text and len(text) <= 8000


def test_mcp_working_tools_cannot_write(isolated_home, monkeypatch):
    save()
    _, expired = save("expired-one", items=turns(*[(x["role"], x["text"] + " kiwi") for x in LONG_ENOUGH]))
    p = isolated_home / "working" / f"{expired['id']}.json"
    data = json.loads(p.read_text())
    data["expires_at"] = "2000-01-01T00:00:00Z"
    p.write_text(json.dumps(data))
    before = {q.name: q.read_bytes() for q in (isolated_home / "working").iterdir()}

    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            raise AssertionError(f"write attempted: {file} {mode}")
        return real_open(file, mode, *a, **k)

    def forbidden(*a, **k):
        raise AssertionError("filesystem change attempted")

    monkeypatch.setattr(builtins, "open", guarded_open)
    for fn in ("replace", "rename", "unlink", "remove", "mkdir", "makedirs", "chmod", "rmdir"):
        monkeypatch.setattr(os, fn, forbidden)
    for _ in range(2):
        assert mcp_call("working_search", query="pytest kiwi")[1] is False
        assert mcp_call("session_latest", agent="claude-code", n=5)[1] is False
    monkeypatch.undo()
    assert {q.name: q.read_bytes() for q in (isolated_home / "working").iterdir()} == before


class FailingHermes(ScriptedHermes):
    async def complete(self, system, prompt, *, json_output=False, json_schema=None):
        from room.agents.base import AgentError
        raise AgentError("hermes: Ollama is not running")


def test_promote_marks_promoted_only_after_extraction_finished(isolated_home, monkeypatch, capsys):
    _, note = save()
    path = files(isolated_home)[0]
    monkeypatch.setattr(cli, "agent_factory", lambda name: FailingHermes())
    assert cli.main(["working", "promote", note["id"]]) == 1  # extraction only queued
    data = json.loads(path.read_text())
    assert data["promoted_at"] is None and data["promote_queued_at"]
    assert "queued" in capsys.readouterr().err
    log = isolated_home / "sessions" / f"{note['id']}.jsonl"
    lines_before = log.read_text().count("\n")

    ScriptedHermes.prompts = []
    monkeypatch.setattr(cli, "agent_factory", lambda name: ScriptedHermes())
    assert cli.main(["working", "promote", note["id"]]) == 0  # retry works
    assert json.loads(path.read_text())["promoted_at"]
    assert log.read_text().count("\n") == lines_before  # the session log is reused, not appended twice
    with MemoryStore() as store:
        assert [n.name for n in store.all_nodes()] == ["Pytest over unittest"]
    assert cli.main(["working", "promote", note["id"]]) == 1  # finished: not again
