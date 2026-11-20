"""Extraction quality: pre-filter, grounding, low-value and secret filters, dedupe. Scripted models only."""

import asyncio
import json
import os

import pytest

from room.agents.base import Agent
from room.memory import extraction_eval as ev
from room.memory import extractor as ex
from room.memory import quality
from room.memory.store import MemoryStore, Provenance
from room.transcript import Message

FAKE = "gh" "p_" + "M3n4B5v6C7x8Z9l0K1j2H3g4F5d6S7a8Q9w0"


class Scripted(Agent):
    """Returns the given outputs in order; records prompts."""

    name = "hermes"

    def __init__(self, *outputs, delay=0.0):
        self.outputs, self.prompts, self.delay = list(outputs), [], delay

    async def complete(self, system, prompt, *, json_output=False, json_schema=None):
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        out = self.outputs.pop(0) if self.outputs else '{"nodes": [], "edges": []}'
        return out if isinstance(out, str) else json.dumps(out)

    async def reply(self, task, transcript):
        return ""

    async def healthcheck(self):
        return True, "scripted"


def msgs(*user, sid="q1"):
    return [Message(sid, "user", 0, "user", u) for u in user]


def node(name, summary, type="fact", confidence=0.9, evidence=None):
    n = {"type": type, "name": name, "summary": summary, "confidence": confidence}
    if evidence is not None:
        n["evidence"] = evidence
    return n


def run(messages, agent, tmp_path, **kw):
    db = tmp_path / "memory.db"
    result = asyncio.run(ex.extract_session(messages[0].session_id, messages, lambda name: agent, store_path=db, **kw))
    nodes = []
    if db.exists():
        with MemoryStore(db) as store:
            nodes = store.all_nodes()
    return result, nodes


@pytest.fixture(autouse=True)
def hermes_only(monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "hermes")
    monkeypatch.delenv("MEMORY_EXTRACTOR_FALLBACK", raising=False)


# -- pre-filter: what reaches the model -----------------------------------------------------------

@pytest.mark.parametrize("chatter", [
    ["hi"], ["Hello!", "thanks!"], ["Reply with just the word hello."], ["ok great, thanks!"],
    ["what is a hash map?"], ["   "], [""],
])
def test_chatter_or_empty_input_never_calls_the_model(tmp_path, chatter):
    agent = Scripted({"nodes": [node("Junk", "junk")], "edges": []})
    result, nodes = run(msgs(*chatter), agent, tmp_path)
    assert result.status == "empty" and agent.prompts == [] and nodes == []


def test_tool_noise_is_removed_before_the_model_sees_it(tmp_path):
    text = ("My deploy of Okapi Notes failed.\n```\nTraceback (most recent call last):\n  File \"/srv/main.py\", line 42\n"
            "RuntimeError: bad interpreter\n```\nTraceback (most recent call last):\n  File \"/srv/x.py\", line 9, in f\n"
            "ValueError: nope\n$ fly deploy --remote-only\nThe fix was pinning Python 3.11 in the Dockerfile.")
    agent = Scripted()
    run(msgs("hi there", text), agent, tmp_path)
    [prompt] = agent.prompts
    assert "Okapi Notes" in prompt and "pinning Python 3.11" in prompt
    for noise in ("Traceback", "line 42", "RuntimeError", "ValueError", "fly deploy", "hi there"):
        assert noise not in prompt, noise


def test_personal_question_is_kept(tmp_path):
    agent = Scripted()
    run(msgs("What should I name my budgeting app?"), agent, tmp_path)
    assert len(agent.prompts) == 1


# -- post-filter: what is stored ---------------------------------------------------------------------

def test_ungrounded_nodes_and_their_edges_are_dropped(tmp_path):
    out = {"nodes": [
        node("Trail Tracker", "A React Native app for hiking routes.", "project", evidence="I'm building Trail Tracker"),
        node("Northwind Labs", "A technology company.", "organization", evidence="I work at Northwind Labs"),
    ], "edges": [{"src": "Me", "relation": "works_on", "dst": "Trail Tracker"},
                 {"src": "Trail Tracker", "relation": "relates_to", "dst": "Northwind Labs"}]}
    result, nodes = run(msgs("I'm building Trail Tracker, a React Native app for hiking routes."), Scripted(out), tmp_path)
    assert result.status == "stored" and [n.name for n in nodes] == ["Trail Tracker"]


def test_paraphrased_evidence_with_the_same_words_is_kept(tmp_path):
    out = {"nodes": [node("Postgres Decision", "Chose Postgres over MongoDB.", "decision",
                          evidence="decided to use Postgres instead of MongoDB for Trail Tracker")], "edges": []}
    _, nodes = run(msgs("We decided to use  Postgres instead of MongoDB, for Trail Tracker!"), Scripted(out), tmp_path)
    assert [n.name for n in nodes] == ["Postgres Decision"]


def test_node_without_evidence_keeps_the_old_behaviour_but_bad_evidence_is_dropped(tmp_path):
    # Backends without structured output may omit evidence: those nodes are kept (as before this change);
    # Hermes always sends evidence because the Ollama schema requires it.
    out = {"nodes": [node("Trail Tracker", "Hiking app.", "project"),
                     node("PennyWise", "App name.", "project", evidence="call it PennyWise")], "edges": []}
    _, nodes = run(msgs("I'm building Trail Tracker for hikers."), Scripted(out), tmp_path)
    assert [n.name for n in nodes] == ["Trail Tracker"]


@pytest.mark.parametrize("bad", [
    node("Hash Map Question", "The user asked what a hash map is.", evidence="hash map"),
    node("Greeting", "The user greeted the assistant and said thanks.", evidence="thanks"),
    node("Hello Instruction", "The user provided the instruction to reply with just the word hello.", evidence="hello"),
    node("Trail Tracker", "A hiking app.", "project", confidence=0.3, evidence="Trail Tracker"),
    node("Okapi Notes", f"Uses token {FAKE}.", "project", evidence="Okapi Notes"),
    node(f"Key {FAKE}", "A key.", evidence="Okapi Notes"),
    node("Okapi Notes", "Note app.", "project", evidence=f"token {FAKE}"),
])
def test_low_value_and_secret_nodes_are_dropped(tmp_path, bad):
    source = (f"hash map? thanks hello. I'm building Trail Tracker. Okapi Notes is my project; my token was {FAKE} "
              "and my teammate asked about it.")
    _, nodes = run(msgs(source), Scripted({"nodes": [bad], "edges": []}), tmp_path)
    assert nodes == []


def test_me_node_with_only_a_meta_summary_keeps_no_summary(tmp_path):
    out = {"nodes": [node("Me", "The user who asked about hash maps.", "person", evidence="I'm building Trail Tracker"),
                     node("Trail Tracker", "Hiking app.", "project", evidence="I'm building Trail Tracker")],
           "edges": [{"src": "Me", "relation": "works_on", "dst": "Trail Tracker"}]}
    _, nodes = run(msgs("I'm building Trail Tracker."), Scripted(out), tmp_path)
    me = next(n for n in nodes if n.name == "Me")
    assert me.summary == ""
    with MemoryStore(tmp_path / "memory.db") as store:
        assert len(store.all_edges()) == 1


def test_same_name_twice_in_one_output_is_stored_once(tmp_path):
    out = {"nodes": [node("University of Toronto MSc application", "Applied in November.", "application", evidence="University of Toronto"),
                     node("University of Toronto MSc Application", "Applied.", "task", evidence="University of Toronto")],
           "edges": []}
    _, nodes = run(msgs("I applied to the University of Toronto MSc."), Scripted(out), tmp_path)
    assert len(nodes) == 1 and nodes[0].type == "application"


def test_fact_already_in_the_graph_under_another_name_is_not_added(tmp_path):
    db = tmp_path / "memory.db"
    with MemoryStore(db) as store:
        store.apply_extraction({"nodes": [node("Pytest Preference", "The user prefers pytest over unittest.", "preference")],
                                "edges": []}, Provenance("old", "hermes", "session"))
    out = {"nodes": [node("Testing Choice", "The user prefers pytest over unittest.", "preference",
                          evidence="I prefer pytest over unittest")], "edges": []}
    result, nodes = run(msgs("I prefer pytest over unittest."), Scripted(out), tmp_path)
    assert result.status == "stored" and [n.name for n in nodes] == ["Pytest Preference"]


def test_near_duplicate_sentence_is_not_appended_on_merge(tmp_path):
    db = tmp_path / "memory.db"
    with MemoryStore(db) as store:
        store.apply_extraction({"nodes": [node("Power Manager", "A macOS menu-bar tool that keeps the Mac awake while agents work.",
                                               "project")], "edges": []}, Provenance("old", "hermes", "session"))
    out = {"nodes": [node("Power Manager", "A macOS menu-bar tool that keeps the Mac awake while AI agents work. It reads presence files.",
                          "project", evidence="Power Manager")], "edges": []}
    _, nodes = run(msgs("Power Manager is my macOS menu-bar tool; it reads presence files."), Scripted(out), tmp_path)
    assert nodes[0].summary == ("A macOS menu-bar tool that keeps the Mac awake while agents work. It reads presence files.")


# -- failures ------------------------------------------------------------------------------------------

def test_malformed_output_twice_is_queued_and_nothing_stored(tmp_path):
    agent = Scripted("not json {", '{"nodes": [}')
    result, nodes = run(msgs("I'm building Trail Tracker."), agent, tmp_path)
    assert result.status == "queued" and nodes == [] and len(agent.prompts) == 2


def test_timeout_is_queued_and_nothing_stored(tmp_path):
    agent = Scripted({"nodes": [node("Trail Tracker", "x", "project", evidence="Trail Tracker")], "edges": []}, delay=1)
    result, nodes = run(msgs("I'm building Trail Tracker."), agent, tmp_path, timeout=0.05)
    assert result.status == "queued" and "timed out" in result.errors[-1] and nodes == []


def test_fake_secret_never_reaches_any_file(tmp_path, isolated_home):
    text = f"My env had API_TOKEN={FAKE}. Okapi Notes is my note-taking side project."
    out = {"nodes": [node("Okapi Notes", f"Note app; token {FAKE}.", "project", evidence="Okapi Notes"),
                     node("Okapi Notes Side Project", "My note-taking side project.", "project", evidence=f"API_TOKEN={FAKE}"),
                     node("Note Taking", "Okapi Notes is a note-taking side project.", "project", evidence="note-taking side project")],
           "edges": []}
    agent = Scripted(out)
    run(msgs(text), agent, tmp_path)
    assert FAKE not in agent.prompts[0]
    for root in (tmp_path, isolated_home):
        for dirpath, _, names in os.walk(root):
            for name in names:
                assert FAKE.encode() not in open(os.path.join(dirpath, name), "rb").read(), name


# -- model call ---------------------------------------------------------------------------------------------

def test_schema_asks_for_evidence_and_validation_keeps_it_optional():
    props = ex.EXTRACTION_SCHEMA["properties"]["nodes"]["items"]
    assert "evidence" in props["properties"] and "evidence" in props["required"]
    data, errors = ex.validate_extraction({"nodes": [node("A", "b")], "edges": []})
    assert errors == [] and "evidence" not in data["nodes"][0]
    data, errors = ex.validate_extraction({"nodes": [node("A", "b", evidence="q")], "edges": []})
    assert errors == [] and data["nodes"][0]["evidence"] == "q"
    _, errors = ex.validate_extraction({"nodes": [node("A", "b", evidence=5)], "edges": []})
    assert errors


def test_prompt_rules_cover_low_value_facts():
    p = ex.SYSTEM_PROMPT.lower()
    for phrase in ("evidence", "greeting", "question", "general knowledge"):
        assert phrase in p


def test_extraction_calls_use_temperature_zero(monkeypatch):
    from room.agents.hermes import HermesAgent

    sent = {}

    class FakeCompletions:
        async def create(self, **kw):
            sent.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]
                usage = None
            return R()

    agent = HermesAgent()
    monkeypatch.setattr(agent, "_get_client", lambda: type("Cl", (), {"chat": type("Ch", (), {"completions": FakeCompletions()})()})())
    asyncio.run(agent.complete("s", "p", json_output=True, json_schema={"type": "object"}))
    assert sent.get("temperature") == 0


# -- the evaluation scorer ------------------------------------------------------------------------------------

def test_scorer_counts_recall_precision_junk_and_duplicates():
    case = {"id": "c", "user": ["I'm building Trail Tracker, a hiking app, and ship in March."],
            "expected": [["trail tracker"], ["march"]], "allowed": [["hiking"]], "forbidden": ["hash map"],
            "existing": [{"type": "project", "name": "Trail Tracker", "summary": "x", "confidence": 1}]}
    nodes = [{"type": "project", "name": "Trail Tracker", "summary": "A hiking app. A hiking app."},
             {"type": "fact", "name": "Hash Maps", "summary": "The user asked about hash maps."},
             {"type": "organization", "name": "Northwind", "summary": "A technology company offering internships."}]
    r = ev.score_case(case, nodes)
    assert (r.expected, r.found, r.counted_nodes, r.useful_nodes) == (2, 1, 3, 1)
    assert r.forbidden == 1 and r.unsupported == 2 and r.duplicates == 1 and r.missed == [["march"]]


def test_eval_cases_are_well_formed():
    cases = ev.load_cases()
    assert 5 <= len(cases) <= 8
    assert any(not c["expected"] for c in cases) and any("{FAKE_SECRET}" in " ".join(c["user"]) for c in cases)
    assert any(c.get("existing") for c in cases) and any(c.get("answer") for c in cases)


@pytest.mark.parametrize("wrong_type", ["decision", "organization", "project"])
def test_me_is_always_stored_as_a_person(tmp_path, wrong_type):
    out = {"nodes": [node("Me", "Building Trail Tracker.", wrong_type, evidence="I'm building Trail Tracker")], "edges": []}
    _, nodes = run(msgs("I'm building Trail Tracker."), Scripted(out), tmp_path)
    assert [(n.name, n.type) for n in nodes] == [("Me", "person")]


# -- blank evidence -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_evidence_is_ungrounded_and_dropped(tmp_path, blank):
    made_up = node("Moon Base", "The user lives on the moon.", "fact", evidence=blank)
    data, errors = ex.validate_extraction({"nodes": [made_up], "edges": []})
    assert errors == []
    cleaned, dropped = quality.clean_extraction(data, "I'm building Trail Tracker.")
    assert cleaned["nodes"] == [] and dropped["ungrounded"] == 1
    _, nodes = run(msgs("I'm building Trail Tracker."), Scripted({"nodes": [made_up], "edges": []}), tmp_path)
    assert "Moon Base" not in [n.name for n in nodes]


def test_schema_requires_non_empty_evidence():
    props = ex.EXTRACTION_SCHEMA["properties"]["nodes"]["items"]["properties"]
    assert props["evidence"].get("minLength") == 1
