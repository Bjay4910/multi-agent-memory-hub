"""Stage 12: generic bugs from the fourth independent review. Scripted models only, fake values only.

C1. withheld values are also split at "#", curly quotes and CJK / Latin boundaries.
C2. matching folds curly quotes, drops the soft hyphen and composes n + U+0303.
C3. a quoted value after a filler word; an apostrophe between letters does not end a value.
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
    asyncio.run(ex.extract_session("s12", [Message("s12", "user", 0, "user", text)], lambda name: agent,
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


# -- C. (case, input, fake secret, expected masked text) ----------------------------------------------------------

C_CASES = [
    # C1 pieces split at "#", curly quotes and CJK / Latin boundaries
    ("c1-chinese", "我的密码是plum994别告诉别人", "plum994", "我的密码是[REDACTED]"),
    ("c1-japanese", "私のパスワードはsakura105です", "sakura105", "私のパスワードは[REDACTED]"),
    ("c1-curly-quotes", "My password is “mango993” for staging", "mango993",
     "My password is “[REDACTED]” for staging"),
    ("c1-hash", "https://svc.example.com?access_key=jay213#frag", "jay213",
     "https://svc.example.com?access_key=[REDACTED]"),
    # C2 matching normalization
    ("c2-curly-apostrophe", "The password’s acorn329 for staging.", "acorn329",
     "The password’s [REDACTED] for staging."),
    ("c2-soft-hyphen", "pass­word: papaya104", "papaya104", "pass­word: [REDACTED]"),
    ("c2-decomposed", "La contraseña: canela883.", "canela883", "La contraseña: [REDACTED]."),
    ("c2-combining-mark-in-value", "pwd: roséx77", "roséx77", "pwd: [REDACTED]"),
    # C3 a quoted value after a filler word; an apostrophe inside an unquoted value
    ("c3-filler-quote", 'My password is really "cranberry769" for CI.', "cranberry769",
     "My password is [REDACTED] for CI."),
    ("c3-to-be-quote", 'I set my password to be "cranberry769" for CI.', "cranberry769",
     "I set my password to [REDACTED] for CI."),
    ("c3-apostrophe", "My password is o'brien882 for prod.", "o'brien882", "My password is [REDACTED]."),
]

# Text that must stay as it is (earlier false-positive guards keep passing in their own files).
C_FALSE_POSITIVES = [
    "The token’s expiry is hourly.",
    "O'Brien joined Trail Tracker last week.",
    'Our plan is really "simple" for CI.',
    "Café menu: espresso, tea",
    "Pass­port renewal is due in June.",
    "Sign in with password protection enabled.",
]


@pytest.mark.parametrize("case, text, secret, masked", C_CASES, ids=[c for c, *_ in C_CASES])
def test_c_case_is_masked_and_withheld(case, text, secret, masked):
    assert quality.mask_credentials(text) == masked
    assert secret in quality.withheld_values(text)
    prompt = redact_secrets(quality.clean_message(text))
    assert secret not in prompt and secret.split("'")[-1] not in prompt


@pytest.mark.parametrize("text", C_FALSE_POSITIVES)
def test_c_false_positive_is_kept(text):
    assert quality.mask_credentials(text) == text


@pytest.mark.parametrize("case, text, secret, masked", C_CASES, ids=[c for c, *_ in C_CASES])
def test_c_case_never_leaks(case, text, secret, masked, tmp_path, monkeypatch):
    """Prompt, every stored node from a hostile model, and every file (memory.db, memory_queue)."""
    queued = assert_never_leaks(f"{text}\nTrail Tracker deploys nightly.", secret, tmp_path, monkeypatch)
    assert queued


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


def test_property_holds_for_the_c_cases():
    assert check_property([(t, s) for _, t, s, _ in C_CASES]) == []
