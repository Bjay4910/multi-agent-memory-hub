"""Stage 11: findings from the third independent review, fixed by family. Scripted models only, fake values only.

A. claimed-handled leaks (reviewer inputs plus 2+ own variants per family).
B. cheap shapes, each with 2+ inputs and a false positive.
D. a fuzz over the A and B shapes.
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
    asyncio.run(ex.extract_session("s11", [Message("s11", "user", 0, "user", text)], lambda name: agent,
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


# -- A. claimed-handled leaks, by family. (family, input, fake secret, expected masked text) ------------------------

A_CASES = [
    # A1 nested brackets: the whole balanced span is masked, for ( ) < > [ ] { }
    ("nested", "password is (outer(inner746)) in prod.", "inner746", "password is [REDACTED] in prod."),
    ("nested", "token: [alpha[beta857]]", "beta857", "token: [REDACTED]"),
    ("nested", "pwd: <x<y179>> for prod", "y179", "pwd: [REDACTED] for prod"),
    ("nested", "api_key = {gamma{delta968}} here", "delta968", "api_key = [REDACTED] here"),
    # A2 a wrapped label followed by filler words
    ("wrapped-label", "**Password** for Trail Tracker is camellia981.", "camellia981",
     "**Password** for Trail Tracker is [REDACTED]."),
    ("wrapped-label", "`api_key` for Trail Tracker = azalea192", "azalea192", "`api_key` for Trail Tracker = [REDACTED]"),
    ("wrapped-label", '"password" for Trail Tracker is begonia203.', "begonia203",
     '"password" for Trail Tracker is [REDACTED].'),
    ("wrapped-label", "'secret' for prod is dahlia284", "dahlia284", "'secret' for prod is [REDACTED]"),
    ("wrapped-label", "*token* on staging = elder395", "elder395", "*token* on staging = [REDACTED]"),
    # A3 curl user and cookie values: empty user, quoted with a space, attached
    ("curl", "curl -u :marigold303 https://api.example.com", "marigold303", "curl -u :[REDACTED] https://api.example.com"),
    ("curl", 'curl -u "bot:blue marigold414" https://api.example.com', "blue marigold414",
     'curl -u "bot:[REDACTED]" https://api.example.com'),
    ("curl", "curl -ubot:petunia525 https://api.example.com", "petunia525", "curl -ubot:[REDACTED] https://api.example.com"),
    ("curl", "curl --user 'ci:violet 828' https://api.example.com", "violet 828",
     "curl --user 'ci:[REDACTED]' https://api.example.com"),
    ("curl", "curl -b 'sid=blue cedar606' https://api.example.com", "blue cedar606",
     "curl -b 'sid=[REDACTED]' https://api.example.com"),
    ("curl", 'curl --cookie "session=lilac717" https://api.example.com', "lilac717",
     'curl --cookie "session=[REDACTED]" https://api.example.com'),
    # A4 CLI flags with quoted and attached values
    ("cli", "mysql -u admin -p'foxglove969' shop", "foxglove969", "mysql -u admin -p'[REDACTED]' shop"),
    ("cli", 'mysql -u admin -p"bluebell170" shop', "bluebell170", 'mysql -u admin -p"[REDACTED]" shop'),
    ("cli", 'tool --password="red poppy281" --verbose', "red poppy281", 'tool --password="[REDACTED]" --verbose'),
    ("cli", "sshpass -p'primrose392' ssh ops@db.internal", "primrose392", "sshpass -p'[REDACTED]' ssh ops@db.internal"),
    ("cli", "sshpass -p 'tansy503' ssh ops@db.internal", "tansy503", "sshpass -p '[REDACTED]' ssh ops@db.internal"),
    ("cli", "docker login -u ops -p parsley999 registry.example.com", "parsley999",
     "docker login -u ops -p [REDACTED] registry.example.com"),
    ("cli", 'redis-cli -a "ivy 614" ping', "ivy 614", 'redis-cli -a "[REDACTED]" ping'),
    ("cli", "docker login -u ops --password=sage725 registry.example.com", "sage725",
     "docker login -u ops --password=[REDACTED] registry.example.com"),
    # A5 with / using followed by a quoted value
    ("with-using", 'Connect with password "lotus625" to staging.', "lotus625",
     'Connect with password "[REDACTED]" to staging.'),
    ("with-using", "Log in using token 'clover736' today.", "clover736", "Log in using token '[REDACTED]' today."),
    ("with-using", 'Sign in with key "quiet harbor words" now.', "quiet harbor words",
     'Sign in with key "[REDACTED]" now.'),
    ("with-using", "Deploy using password `fern847` today.", "fern847", "Deploy using password `[REDACTED]` today."),
    # A6 Basic tokens of 12+ base64 characters, even without digits or "="
    ("basic", "curl sends Basic YWFhOmFhYWFhYWFh to the API", "YWFhOmFhYWFhYWFh",
     "curl sends Basic [REDACTED] to the API"),
    ("basic", "header: Basic dXNlclBhc3NXb3Jk", "dXNlclBhc3NXb3Jk", "header: Basic [REDACTED]"),
    ("basic", "send basic QWxhZGRpbkJvYmJ5 now", "QWxhZGRpbkJvYmJ5", "send basic [REDACTED] now"),
]

A_FALSE_POSITIVES = [
    ("nested", '{"auth": {"pwd": "tulip66"}}', '{"auth": {"pwd": "[REDACTED]"}}'),  # JSON objects keep their keys
    ("nested", "password: [REDACTED]", "password: [REDACTED]"),  # masking twice changes nothing
    ("wrapped-label", "**Note** for Trail Tracker is ready.", "**Note** for Trail Tracker is ready."),
    ("curl", "curl -u deploy https://api.example.com", "curl -u deploy https://api.example.com"),
    ("cli", "mysql -u admin -p shop", "mysql -u admin -p shop"),
    ("with-using", "Sign in with password protection enabled.", "Sign in with password protection enabled."),
    ("basic", "Basic Authentication is enabled.", "Basic Authentication is enabled."),
    ("basic", "The Basic plan is cheap.", "The Basic plan is cheap."),
]


@pytest.mark.parametrize("family, text, secret, masked", A_CASES,
                         ids=[f"{f}-{i}" for i, (f, *_) in enumerate(A_CASES)])
def test_a_family_masks_the_value(family, text, secret, masked):
    assert quality.mask_credentials(text) == masked
    assert secret in quality.withheld_values(text)


@pytest.mark.parametrize("family, text, masked", A_FALSE_POSITIVES,
                         ids=[f"{f}-{i}" for i, (f, *_) in enumerate(A_FALSE_POSITIVES)])
def test_a_family_false_positive(family, text, masked):
    assert quality.mask_credentials(text) == masked


def test_every_a_family_has_the_reviewer_input_plus_two_variants():
    for family in {f for f, *_ in A_CASES}:
        assert sum(f == family for f, *_ in A_CASES) >= 3, family


@pytest.mark.parametrize("family, text, secret, masked", A_CASES,
                         ids=[f"{f}-{i}" for i, (f, *_) in enumerate(A_CASES)])
def test_a_family_never_leaks(family, text, secret, masked, tmp_path, monkeypatch):
    assert_never_leaks(f"{text}\nTrail Tracker deploys nightly.", secret, tmp_path, monkeypatch)


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
