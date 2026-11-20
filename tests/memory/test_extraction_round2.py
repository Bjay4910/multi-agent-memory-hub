"""Extraction round 2: held-out set plumbing, pre-filter recall fixes, restated questions. Scripted models only."""

import json
from pathlib import Path

from room.memory import extraction_eval as ev

HELDOUT = Path(__file__).resolve().parents[1] / "eval" / "heldout_cases.json"


# -- held-out set and eval plumbing ----------------------------------------------------------------

def test_heldout_cases_are_well_formed_and_separate():
    cases = ev.load_cases(HELDOUT)
    assert len(cases) >= 8
    groups = {c["group"] for c in cases}
    assert {"ab", "control", "independent"} <= groups
    assert sum(c["group"] == "independent" for c in cases) >= 4
    assert sum(not c["expected"] for c in cases) >= 2
    assert any("{FAKE_SECRET}" in " ".join(c["user"]) for c in cases)
    original = {c["id"] for c in ev.load_cases()}
    assert not original & {c["id"] for c in cases}
    for c in cases:
        assert set(c) <= {"id", "group", "user", "answer", "expected", "allowed", "forbidden", "existing"}


def test_eval_main_takes_a_cases_file_and_prints_group_totals(tmp_path, capsys):
    cases = [{"id": "a", "group": "g1", "user": ["I'm building Trail Tracker for hikers"], "expected": [["trail tracker"]],
              "allowed": [], "forbidden": []},
             {"id": "b", "group": "g2", "user": ["hi"], "expected": [], "allowed": [], "forbidden": ["hi"]}]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases))
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps([[
        {"case": "a", "nodes": [{"type": "project", "name": "Trail Tracker", "summary": ""}], "secret_leaks": 0, "status": "stored"},
        {"case": "b", "nodes": [{"type": "fact", "name": "Hi", "summary": "said hi"}], "secret_leaks": 0, "status": "stored"},
    ]]))
    assert ev.main(["--cases", str(cases_file), "--rescore", str(saved)]) == 0
    out = capsys.readouterr().out
    assert '"recall": 1.0' in out and '"precision": 0.5' in out
    assert "group g1" in out and "group g2" in out
    g1 = next(line for line in out.splitlines() if "group g1" in line)
    assert '"precision": 1.0' in g1


# -- shared helpers (copies of the ones in test_extraction_quality.py; that file is left untouched) ---------

import asyncio  # noqa: E402

import pytest  # noqa: E402

from room.agents.base import Agent  # noqa: E402
from room.memory import extractor as ex  # noqa: E402
from room.memory import quality  # noqa: E402
from room.memory.store import MemoryStore  # noqa: E402
from room.transcript import Message  # noqa: E402


class Scripted(Agent):
    name = "hermes"

    def __init__(self, *outputs):
        self.outputs, self.prompts = list(outputs), []

    async def complete(self, system, prompt, *, json_output=False, json_schema=None):
        self.prompts.append(prompt)
        out = self.outputs.pop(0) if self.outputs else '{"nodes": [], "edges": []}'
        return out if isinstance(out, str) else json.dumps(out)

    async def reply(self, task, transcript):
        return ""

    async def healthcheck(self):
        return True, "scripted"


@pytest.fixture(autouse=True)
def hermes_only(monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "hermes")
    monkeypatch.delenv("MEMORY_EXTRACTOR_FALLBACK", raising=False)


def msgs(*user, sid="r2"):
    return [Message(sid, "user", 0, "user", u) for u in user]


def node(name, summary, type="fact", confidence=0.9, evidence=None):
    n = {"type": type, "name": name, "summary": summary, "confidence": confidence}
    if evidence is not None:
        n["evidence"] = evidence
    return n


def run(messages, agent, tmp_path):
    db = tmp_path / "memory.db"
    result = asyncio.run(ex.extract_session(messages[0].session_id, messages, lambda name: agent, store_path=db))
    nodes = []
    if db.exists():
        with MemoryStore(db) as store:
            nodes = store.all_nodes()
    return result, nodes


# -- A: the pre-filter must keep real facts -------------------------------------------------------------------

@pytest.mark.parametrize("text, kept", [
    ("> I prefer tabs over spaces", "I prefer tabs over spaces"),
    ("Warning: I am moving Trail Tracker to Rust", "Warning: I am moving Trail Tracker to Rust"),
    ("Is Postgres right for Trail Tracker?", "Is Postgres right for Trail Tracker?"),
    ("$ npm test\n  I switched Trail Tracker to Vitest", "I switched Trail Tracker to Vitest"),
])
def test_prefilter_keeps_real_facts(text, kept):
    assert kept in quality.clean_message(text)


@pytest.mark.parametrize("noise", [
    ">>> import os",
    "RuntimeError: bad interpreter",
    "DeprecationWarning: pkg_resources is deprecated",
    "Error: ENOENT: no such file or directory, open 'config.json'",
    "$ npm test\n  PASS src/app.test.js\n  Tests: 3 passed, 3 total",
    "Traceback (most recent call last):\n  File \"/srv/main.py\", line 42, in <module>\n    connect(API_TOKEN)",
    "what is a hash map?",
    "What is Python?",
])
def test_prefilter_still_drops_tool_output_traces_and_impersonal_questions(noise):
    assert quality.clean_message(noise) == ""


@pytest.mark.parametrize("text, evidence, name", [
    ("> I prefer tabs over spaces", "I prefer tabs over spaces", "Tabs Over Spaces"),
    ("Warning: I am moving Trail Tracker to Rust", "moving Trail Tracker to Rust", "Trail Tracker"),
    ("Is Postgres right for Trail Tracker?", "Trail Tracker", "Trail Tracker"),
    ("$ npm test\n  I switched Trail Tracker to Vitest", "I switched Trail Tracker to Vitest", "Trail Tracker"),
])
def test_issue_a_messages_keep_their_fact_end_to_end(tmp_path, text, evidence, name):
    out = {"nodes": [node(name, "The user's fact.", "project", evidence=evidence)], "edges": []}
    agent = Scripted(out)
    result, nodes = run(msgs(text), agent, tmp_path)
    assert result.status == "stored" and [n.name for n in nodes] == [name]


# -- B and A3: a question restated is not a fact; the project it names is ----------------------------------------

def test_restated_question_keeps_the_project_but_not_the_question(tmp_path):
    out = {"nodes": [
        node("Name Budgeting App", "The user wants to name their budgeting app.", "task", evidence="name my budgeting app"),
        node("Budgeting App", "The user's budgeting app.", "project", evidence="my budgeting app"),
        node("Me", "The user is making a decision about the name of their budgeting app.", "person",
             evidence="What should I name my budgeting app"),
    ], "edges": [{"src": "Me", "relation": "works_on", "dst": "Budgeting App"},
                 {"src": "Me", "relation": "works_on", "dst": "Name Budgeting App"}]}
    _, nodes = run(msgs("What should I name my budgeting app?"), Scripted(out), tmp_path)
    by_name = {n.name: n for n in nodes}
    assert set(by_name) == {"Budgeting App", "Me"}
    assert by_name["Me"].summary == ""
    with MemoryStore(tmp_path / "memory.db") as store:
        assert len(store.all_edges()) == 1


def test_question_about_a_project_stores_the_project_not_a_decision(tmp_path):
    out = {"nodes": [
        node("Trail Tracker", "The user's project.", "project", evidence="Trail Tracker"),
        node("Use Postgres", "Postgres for Trail Tracker.", "decision", evidence="Is Postgres right for Trail Tracker"),
    ], "edges": []}
    _, nodes = run(msgs("Is Postgres right for Trail Tracker?"), Scripted(out), tmp_path)
    assert [n.name for n in nodes] == ["Trail Tracker"]


def test_a_decision_stated_outside_the_question_is_kept():
    source = "We decided to use Postgres for Trail Tracker. Is Postgres right for Trail Tracker?"
    data = {"nodes": [node("Use Postgres", "Decided to use Postgres.", "decision", evidence="decided to use Postgres")],
            "edges": []}
    cleaned, _ = quality.clean_extraction(data, source)
    assert [n["name"] for n in cleaned["nodes"]] == ["Use Postgres"]


@pytest.mark.parametrize("summary", [
    "The user is making a decision about which database to use.",
    "The user is deciding on a name.",
    "The user is wondering what to call the app.",
    "The user is unsure which database to pick.",
])
def test_me_summary_that_only_describes_the_question_is_blanked(summary):
    data = {"nodes": [node("Me", summary, "person", evidence="I'm building Trail Tracker")], "edges": []}
    cleaned, _ = quality.clean_extraction(data, "I'm building Trail Tracker. Which database should I use?")
    assert cleaned["nodes"][0]["summary"] == ""
