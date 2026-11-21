"""Stage 13: the fifth review's leaks in verified forms. Scripted models only, fake values only.

A1. "=" padding of a base64 token after a label is not a connector ("my token YWJjZGU= for Trail Tracker").
A2. withheld pieces of 3+ characters when they mix letters and digits (k9x4 in 我的密码是k9x4别告诉别人).
"""

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from room.agents.base import Agent
from room.memory import extractor as ex
from room.memory import quality
from room.memory.redact import redact_secrets
from room.memory.store import MemoryStore
from room.transcript import Message


class Scripted(Agent):
    name = "hermes"

    def __init__(self, *outputs):
        self.outputs, self.prompts = list(outputs), []

    async def complete(self, system, prompt, *, json_output=False, json_schema=None):
        self.prompts.append(prompt)
        out = self.outputs.pop(0) if self.outputs else '{"nodes": [], "edges": []}'
        return out(prompt) if callable(out) else (out if isinstance(out, str) else json.dumps(out))

    async def reply(self, task, transcript):
        return ""

    async def healthcheck(self):
        return True, "scripted"


@pytest.fixture(autouse=True)
def hermes_only(monkeypatch):
    monkeypatch.setenv("MEMORY_EXTRACTOR", "hermes")
    monkeypatch.delenv("MEMORY_EXTRACTOR_FALLBACK", raising=False)


def node(name, summary, type="fact", confidence=0.9, evidence=None):
    n = {"type": type, "name": name, "summary": summary, "confidence": confidence}
    if evidence is not None:
        n["evidence"] = evidence
    return n


def hostile(value):
    def answer(prompt):
        return json.dumps({"nodes": [
            node("Trail Tracker", f"Uses {value}.", "project", evidence="Trail Tracker"),
            node(f"Key {value}", "A key.", "fact", evidence="Trail Tracker"),
            node("Trail Tracker Config", "Config.", "project", evidence=value),
            node("Prompt Echo", prompt[-400:], "fact", evidence="Trail Tracker"),
            node("Trail Tracker Access", f"The password for Trail Tracker is {value}.", "fact",
                 evidence="Trail Tracker"),
        ], "edges": []})
    return answer


def invalid(value):
    """Invalid output twice (first try and repair), quoting the value, so the session is queued."""
    bad = json.dumps({"nodes": [{"type": value, "name": f"Key {value}", "summary": value, "confidence": 0.9}],
                      "edges": []})
    return [bad, bad]


def _words_in(value, text):
    return [w for w in re.split(r"[\s:/,;()<>\"\\]+", value)
            if len(w) >= 3 and re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text)]


def run_one(text, value, case_dir, monkeypatch, bad_output=False):
    """One extraction in its own home and database; returns (leaks, queued?)."""
    monkeypatch.setenv("AGENT_ROOM_HOME", str(case_dir / "home"))
    agent = Scripted(*(invalid(value) if bad_output else [hostile(value)]))
    db = case_dir / "memory.db"
    asyncio.run(ex.extract_session("s13", [Message("s13", "user", 0, "user", text)], lambda name: agent,
                                   store_path=db))
    leaks = []
    # The extraction prompt only: a repair prompt quotes the model's own previous output back to it.
    if agent.prompts and (value in agent.prompts[0] or _words_in(value, agent.prompts[0])):
        leaks.append("prompt")
    if db.exists():
        with MemoryStore(db) as store:
            for n in store.all_nodes():
                if value in f"{n.name} {n.summary}" or _words_in(value, f"{n.name} {n.summary}"):
                    leaks.append(f"node {n.name}")
    files = [Path(d, f) for d, _, names in os.walk(case_dir) for f in names]
    for path in files:
        data = path.read_bytes()
        text_data = data.decode("utf-8", "replace")
        if value.encode() in data or _words_in(value, text_data):
            leaks.append(f"file {path.relative_to(case_dir)}")
    queued = any("memory_queue" in str(p) for p in files)
    return leaks, queued


def assert_never_leaks(text, value, tmp_path, monkeypatch):
    leaks, _ = run_one(text, value, tmp_path / "valid", monkeypatch)
    assert leaks == [], leaks
    leaks, queued = run_one(text, value, tmp_path / "invalid", monkeypatch, bad_output=True)
    assert leaks == [], leaks
    return queued


# -- A. (case, input, fake secret, expected masked text) ----------------------------------------------------------

A_CASES = [
    # A1 padded base64 after a label, under 32 characters: the token is masked, not the words after it
    ("a1-single-reviewer", "my token YWRtaW46cGFzc3dvcmQ= for Trail Tracker", "YWRtaW46cGFzc3dvcmQ=",
     "my token [REDACTED] for Trail Tracker"),
    ("a1-short-reviewer", "my token YWJjZGU= for Trail Tracker", "YWJjZGU=", "my token [REDACTED] for Trail Tracker"),
    ("a1-single-own", "my api key Zm9vYmFyOTk= for Trail Tracker", "Zm9vYmFyOTk=",
     "my api key [REDACTED] for Trail Tracker"),
    ("a1-double-own", "my secret c2VjcmV0S2V5MQ== for Trail Tracker", "c2VjcmV0S2V5MQ==",
     "my secret [REDACTED] for Trail Tracker"),
    ("a1-single-reviewer-end", "my token YWRtaW46cGFzc3dvcmQ=", "YWRtaW46cGFzc3dvcmQ=", "my token [REDACTED]"),
    ("a1-short-reviewer-end", "my token YWJjZGU=", "YWJjZGU=", "my token [REDACTED]"),
    ("a1-single-own-end", "my api key Zm9vYmFyOTk=", "Zm9vYmFyOTk=", "my api key [REDACTED]"),
    ("a1-double-own-end", "my secret c2VjcmV0S2V5MQ==", "c2VjcmV0S2V5MQ==", "my secret [REDACTED]"),
    # A2 4-character secrets split from CJK text or "#"
    ("a2-chinese", "我的密码是k9x4别告诉别人", "k9x4", "我的密码是[REDACTED]"),
    ("a2-japanese", "私のパスワードはm7q2です", "m7q2", "私のパスワードは[REDACTED]"),
    ("a2-hash", "https://svc.example.com?access_key=j8k3#frag", "j8k3", "https://svc.example.com?access_key=[REDACTED]"),
]

A_FALSE_POSITIVES = [
    "Basic Authentication is enabled.",
    "Set x=1 and y=2 in the config.",
    "The build flag DEBUG=0 is off.",
    "Trail Tracker keeps a frag counter.",
]


@pytest.mark.parametrize("case, text, secret, masked", A_CASES, ids=[c for c, *_ in A_CASES])
def test_a_case_is_masked_and_withheld(case, text, secret, masked):
    assert quality.mask_credentials(text) == masked
    assert secret in quality.withheld_values(text)
    assert secret not in redact_secrets(quality.clean_message(text))


@pytest.mark.parametrize("text", A_FALSE_POSITIVES)
def test_a_false_positive_is_kept(text):
    assert quality.mask_credentials(text) == text


@pytest.mark.parametrize("case, text, secret, masked", A_CASES, ids=[c for c, *_ in A_CASES])
def test_a_case_never_leaks(case, text, secret, masked, tmp_path, monkeypatch):
    """Prompt, every stored node from a hostile model, and every file (memory.db, memory_queue)."""
    queued = assert_never_leaks(f"{text}\nTrail Tracker deploys nightly.", secret, tmp_path, monkeypatch)
    assert queued


def test_short_letter_pieces_are_not_withheld_so_an_ordinary_node_is_kept():
    text = "https://svc.example.com?access_key=j8k3#frag\nTrail Tracker keeps a frag counter."
    withheld = quality.withheld_values(text)
    assert "j8k3" in withheld and "frag" not in withheld
    n = node("Trail Tracker", "Trail Tracker keeps a frag counter.", "project",
             evidence="Trail Tracker keeps a frag counter")
    cleaned, _ = quality.clean_extraction({"nodes": [n], "edges": []}, text, None, withheld)
    assert [x["name"] for x in cleaned["nodes"]] == ["Trail Tracker"]


def check_property(corpus):
    misses = []
    for text, value in corpus:
        withheld = quality.withheld_values(text)
        prepared = quality.premask_text(text)
        ours = quality.credential_values(prepared) | quality.credential_values(text)
        theirs = set()
        for source in (quality.mask_credentials(prepared), quality.clean_message(text)):
            theirs |= quality.redact_masked_substrings(source)
        misses += [(text, v) for v in ours | theirs if v not in withheld]
        if value in prepared and value not in redact_secrets(quality.mask_credentials(prepared)) and not (
                value in withheld or any(value in w for w in withheld)):
            misses.append((text, value))
    return misses


def test_property_holds_for_the_a_cases():
    assert check_property([(t, s) for _, t, s, _ in A_CASES]) == []
