import asyncio
import json

import pytest

from room.agents.base import AgentError
from room.memory import extractor as ex
from room.memory.store import MemoryStore
from room.presence import read_presence
from room.transcript import Message

GOOD = {"nodes": [
    {"type": "project", "name": "Agent Room", "summary": "Multi-agent room.", "confidence": 0.9},
    {"type": "organization", "name": "TU Munich", "summary": "University.", "confidence": 0.8},
    {"type": "application", "name": "TU Munich MSc application", "summary": "Submitted.", "confidence": 0.8},
], "edges": [{"src": "TU Munich MSc application", "relation": "applied_to", "dst": "TU Munich"}]}


def session(task="I applied to TU Munich. My project is Agent Room.", final="Good luck with TU Munich."):
    msgs = [Message("s1", "user", 0, "user", task), Message("s1", "claude", 1, "agent", "round reply"),
            Message("s1", "room", 1, "note", "gemini skipped"), Message("s1", "claude", 2, "final", final)]
    return msgs


def run(msgs, factory, sid="s1", **kw):
    return asyncio.run(ex.extract_session(sid, msgs, factory, **kw))


# -- validation ------------------------------------------------------------

def test_valid_extraction_passes():
    data, errors = ex.validate_extraction(GOOD)
    assert errors == [] and len(data["nodes"]) == 3


@pytest.mark.parametrize("bad,fragment", [
    ([], "top level"),
    ({"nodes": []}, '"edges" must be a list'),
    ({"nodes": [], "edges": [], "extra": 1}, "unexpected top-level"),
    ({"nodes": [{"type": "planet", "name": "X", "summary": "", "confidence": 0.5}], "edges": []}, "nodes[0].type"),
    ({"nodes": [{"type": "fact", "name": "", "summary": "", "confidence": 0.5}], "edges": []}, "nodes[0].name"),
    ({"nodes": [{"type": "fact", "name": "X", "summary": 3, "confidence": 0.5}], "edges": []}, "nodes[0].summary"),
    ({"nodes": [{"type": "fact", "name": "X", "summary": "", "confidence": 1.5}], "edges": []}, "confidence"),
    ({"nodes": [{"type": "fact", "name": "X", "summary": "", "confidence": True}], "edges": []}, "confidence"),
    ({"nodes": [{"type": "fact", "name": "X", "summary": ""}], "edges": []}, "confidence"),
    ({"nodes": [], "edges": [{"src": "a", "relation": "loves", "dst": "b"}]}, "edges[0].relation"),
    ({"nodes": [], "edges": [{"src": "", "relation": "mentions", "dst": "b"}]}, "edges[0].src"),
    ({"nodes": ["x"], "edges": []}, "nodes[0] must be an object"),
])
def test_invalid_extractions_are_rejected(bad, fragment):
    data, errors = ex.validate_extraction(bad)
    assert data is None and any(fragment in e for e in errors), errors


def test_parse_tolerates_fences_and_surrounding_text():
    assert ex.parse_and_validate("```json\n" + json.dumps(GOOD) + "\n```")[0] is not None
    assert ex.parse_and_validate("Sure! " + json.dumps(GOOD) + " Hope that helps.")[0] is not None
    data, errors = ex.parse_and_validate("not json at all")
    assert data is None and "not valid JSON" in errors[0]


def test_validation_normalizes_and_redacts():
    raw = {"nodes": [{"type": "Fact", "name": "Deploy key", "summary": "key is s" "k-abcdefghijklmnopqrstuvwx", "confidence": 1},
                     {"type": "fact", "name": "AI" "zaSyA1234567890abcdefghijklmnopqrstu", "summary": "", "confidence": 1}],
           "edges": [{"src": "a", "relation": "Mentions", "dst": "b"}]}
    data, _ = ex.validate_extraction(raw)
    assert [x["name"] for x in data["nodes"]] == ["Deploy key"]  # redacted-name node dropped
    assert data["nodes"][0]["type"] == "fact" and "s" "k-" not in data["nodes"][0]["summary"]
    assert data["edges"][0]["relation"] == "mentions"


# -- source text -------------------------------------------------------------

def test_source_text_defaults_to_user_words_only():
    text = ex.source_text(session())
    assert "I applied to TU Munich" in text and "Good luck" not in text and "ANSWER" not in text


def test_source_text_session_scope_uses_task_and_final_and_redacts():
    msgs = session(task="My key is AI" "zaSyA1234567890abcdefghijklmnopqrstu and I like Rust.")
    text = ex.source_text(msgs, scope="session")
    assert "AI" "za" not in text and "[REDACTED]" in text and "I like Rust" in text
    assert "Good luck with TU Munich." in text
    assert "round reply" not in text and "gemini skipped" not in text


def test_source_text_falls_back_to_last_round():
    msgs = [Message("s", "user", 0, "user", "Q"), Message("s", "a", 1, "agent", "old"), Message("s", "b", 2, "agent", "latest")]
    text = ex.source_text(msgs, scope="session")
    assert "b: latest" in text and "old" not in text


# -- flow ------------------------------------------------------------------------

def test_successful_extraction_is_stored_with_provenance(isolated_home, factory):
    build = factory({"hermes": [GOOD]})
    res = run(session(), build)
    assert res.status == "stored" and res.agent == "hermes"
    assert res.counts["created"] == 3 and res.counts["edges"] == 1
    system, prompt, json_output = build.created["hermes"].prompts[0]
    assert json_output and "STRICT" not in system and "TU Munich" in prompt
    with MemoryStore() as s:
        node = s.find_nodes("Agent Room")[0]
        prov = s.provenance(node.id)[0]
        assert (prov["session_id"], prov["agent"], prov["source"]) == ("s1", "hermes", "session")
    assert ex.queued_sessions() == []


def test_secrets_never_reach_the_extractor(isolated_home, factory):
    build = factory({"hermes": [GOOD]})
    run(session(task="token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 please remember Rust"), build)
    prompt = build.created["hermes"].prompts[0][1]
    assert "ghp_" not in prompt and "[REDACTED]" in prompt


def test_repair_prompt_fixes_bad_output(isolated_home, factory):
    build = factory({"hermes": ["{ not json", GOOD]})
    res = run(session(), build)
    assert res.status == "stored"
    repair = build.created["hermes"].prompts[1][1]
    assert "not valid" in repair and "{ not json" in repair


def test_still_invalid_without_fallback_is_queued(isolated_home, factory):
    build = factory({"hermes": ["nope", '{"nodes": "x", "edges": []}']})
    res = run(session(), build)
    assert res.status == "queued" and "invalid JSON after repair" in res.errors[-1]
    q = ex.queued_sessions()
    assert len(q) == 1 and q[0]["session_id"] == "s1" and q[0]["attempts"] == 1
    assert not (isolated_home / "memory.db").exists()  # nothing stored


def test_fallback_agent_used_after_failed_repair(isolated_home, factory, monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR_FALLBACK", "claude")
    build = factory({"hermes": ["bad", "still bad"], "claude": [GOOD]})
    res = run(session(), build)
    assert res.status == "stored" and res.agent == "claude"
    assert len(build.created["hermes"].prompts) == 2


def test_unreachable_extractor_queues(isolated_home, factory):
    build = factory({"hermes": [AgentError("hermes: cannot connect to http://localhost:11434/v1")]})
    res = run(session(), build)
    assert res.status == "queued" and "cannot connect" in res.errors[-1]


def test_cloud_extractor_only_when_configured(isolated_home, factory, monkeypatch):
    build = factory({"hermes": [GOOD], "claude": [GOOD], "gemini": [GOOD]})
    run(session(), build)
    assert set(build.created) == {"hermes"}
    monkeypatch.setenv("MEMORY_EXTRACTOR", "gemini")
    run(session(), build, sid="s2")
    assert set(build.created) == {"hermes", "gemini"}


def test_invalid_extractor_setting_queues(isolated_home, factory, monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "muse")
    res = run(session(), factory({}))
    assert res.status == "queued" and "invalid MEMORY_EXTRACTOR" in res.errors[0]


def test_timeout_queues(isolated_home, monkeypatch):
    from .conftest import ScriptedExtractor

    class Slow(ScriptedExtractor):
        async def complete(self, *a, **k):
            await asyncio.sleep(5)

    res = run(session(), lambda name: Slow(name, []), timeout=0.2)
    assert res.status == "queued" and "timed out" in res.errors[-1]


def test_presence_memory_working_then_idle(isolated_home):
    seen = []

    from .conftest import ScriptedExtractor

    class Peek(ScriptedExtractor):
        async def complete(self, *a, **k):
            seen.extend((e.agent, e.status) for e in read_presence())
            return json.dumps(GOOD)

    run(session(), lambda name: Peek(name, []))
    assert ("memory", "working") in seen
    assert [(e.agent, e.status) for e in read_presence()] == [("memory", "idle")]
    task_label = json.loads((isolated_home / "presence" / "memory.json").read_text())["task"]
    assert task_label == ""


def test_empty_session_is_not_queued(isolated_home, factory):
    res = run([Message("s", "room", 0, "note", "only a note")], factory({}))
    assert res.status == "empty" and ex.queued_sessions() == []


def test_process_queue_retries_and_clears(isolated_home, factory):
    from room.transcript import SessionLog

    log = SessionLog("s1")
    for m in session():
        log.append(m)
    run(session(), factory({"hermes": [AgentError("hermes: down")]}))
    assert len(ex.queued_sessions()) == 1
    run(session(), factory({"hermes": [AgentError("hermes: down")]}))
    assert ex.queued_sessions()[0]["attempts"] == 2
    results = asyncio.run(ex.process_queue(factory({"hermes": [GOOD]})))
    assert [r.status for r in results] == ["stored"]
    assert ex.queued_sessions() == []


def test_process_queue_drops_missing_logs(isolated_home):
    ex.enqueue("ghost", "session", "boom")
    results = asyncio.run(ex.process_queue(lambda n: None))
    assert results[0].status == "empty" and ex.queued_sessions() == []


def test_extractor_passes_schema_and_schema_matches_validator(isolated_home, factory):
    build = factory({"hermes": [GOOD]})
    run(session(), build)
    schema = build.created["hermes"].schemas[0]
    assert schema is ex.EXTRACTION_SCHEMA
    node_props = schema["properties"]["nodes"]["items"]["properties"]
    assert node_props["type"]["enum"] == list(ex.NODE_TYPES)
    assert schema["properties"]["edges"]["items"]["properties"]["relation"]["enum"] == list(ex.RELATIONS)


def test_openai_compat_sends_structured_output_format(monkeypatch):
    from types import SimpleNamespace

    from room.agents.hermes import HermesAgent

    sent = {}

    class FakeCompletions:
        async def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))], usage=None)

    agent = HermesAgent()
    agent._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    asyncio.run(agent.complete("s", "p", json_output=True, json_schema={"type": "object"}))
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"] == {"type": "object"}
    asyncio.run(agent.complete("s", "p", json_output=True))
    assert sent["response_format"] == {"type": "json_object"}


def test_prompt_has_no_real_world_example_names_and_labels_sections():
    assert "Munich" not in ex.SYSTEM_PROMPT and "never record its" in ex.SYSTEM_PROMPT
    text = ex.source_text(session(), scope="session")
    assert text.startswith("USER (the user speaking about themselves):")
    assert "ANSWER (advice from AI assistants; may contain guesses):" in text


def test_extract_scope_from_env(monkeypatch):
    assert ex.extract_scope() == "user"
    monkeypatch.setenv("MEMORY_EXTRACT_FROM", "session")
    assert ex.extract_scope() == "session"
    monkeypatch.setenv("MEMORY_EXTRACT_FROM", "everything")
    assert ex.extract_scope() == "user"
