"""Stage 15: single-"=" base64 in one case (all lower-case, all upper-case, or capitalised only at the start).

Directly after a label ("my token enp6eno= for Trail Tracker") it is NOT masked: the word is read as a filler word
and its "=" as the connector. The rule that would fix it (7+ base64 characters + "=" directly after a label) also
captures real filler words such as "staging=" in "api key staging= x" and broke the stage 9 fuzz (64 leaks), so it
was not applied. Those cases are strict xfails (a known gap); the shapes after a real connector and the controls
pass. Fake values only."""

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
    asyncio.run(ex.extract_session("s15", [Message("s15", "user", 0, "user", text)], lambda name: agent,
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


# Valid single-"=" base64 values (length a multiple of 4).
REVIEWER = ["enp6eno=", "enp6enp6eno=", "QUFBQUE=", "Abcdefg="]
LOWER = ["bmz4dnc=", "bmz4dnz6eqk=", "bmz4dnz6eqp1cnt3eho="]   # 8, 12, 20 characters
UPPER = ["NA7CKPA=", "QUJDREVGR0I=", "WRZQWONWUOT60O305F0="]   # 8, 12, 20 characters

# (shape, input, secret)
CASES = (
    [("my-token", f"my token {s} for Trail Tracker", s) for s in REVIEWER + LOWER + UPPER]
    + [("my-api-key", f"my api key {s} for Trail Tracker", s) for s in ("dmvyexc=", "DJXPHV0=", "Mnbvcxw=")]
    + [("api-key-is", f"api key is {s} for Trail Tracker", s) for s in ("bmz4dnc=", "QUJDREVGR0I=", "Abcdefg=")]
    + [("secret-colon", f"secret: {s} for Trail Tracker", s) for s in ("enp6enp6eno=", "NA7CKPA=", "Qwertyo=")]
    + [("password-is", f"password is {s} for Trail Tracker", s) for s in ("bmz4dnz6eqp1cnt3eho=", "QUFBQUE=", "Zxcvbnk=")]
    + [("end-of-line", f"my token {s}\nTrail Tracker deploys nightly.", s) for s in ("enp6eno=", "WRZQWONWUOT60O305F0=", "Poiuyts=")]
)
IDS = [f"{shape}-{i}" for i, (shape, *_) in enumerate(CASES)]
GAP_SHAPES = {"my-token", "my-api-key", "end-of-line"}  # the value directly after the label
GAP = pytest.mark.xfail(strict=True, reason="known gap: one-case single-'=' base64 directly after a label")
PARAMS = [pytest.param(*c, id=i, marks=[GAP] if c[0] in GAP_SHAPES else []) for c, i in zip(CASES, IDS)]


@pytest.mark.parametrize("shape, text, secret", PARAMS)
def test_secret_is_masked_and_withheld(shape, text, secret):
    prompt = redact_secrets(quality.clean_message(text))
    for value in (secret, secret.rstrip("=")):  # with and without the trailing "="
        assert value not in prompt, prompt
        assert value not in quality.mask_credentials(text)
    assert secret in quality.withheld_values(text)


@pytest.mark.parametrize("shape, text, secret", PARAMS)
def test_secret_never_leaks(shape, text, secret, tmp_path, monkeypatch):
    """Prompt, every stored node from a hostile model, and memory_queue, valid and queued, with and without "="."""
    message = text if "\n" in text else f"{text}\nTrail Tracker deploys nightly."
    for n, value in enumerate((secret, secret.rstrip("="))):
        queued = assert_never_leaks(message, value, tmp_path / f"v{n}", monkeypatch)
        assert queued


@GAP
def test_the_words_after_the_secret_are_kept():
    assert quality.mask_credentials("my token enp6eno= for Trail Tracker") == "my token [REDACTED] for Trail Tracker"


# Controls: unchanged by the fix ("Harbor=" still acts as a connector).
CONTROLS = [
    ("Harbor= value for Trail Tracker", "Harbor= value for Trail Tracker"),
    ("x=5 for Trail Tracker", "x=5 for Trail Tracker"),
    ("Set timeout=30 for Trail Tracker", "Set timeout=30 for Trail Tracker"),
    ("my token for Harbor= value", "my token for Harbor= [REDACTED]"),
    ("password for Harbor= maple42", "password for Harbor= [REDACTED]"),
]


@pytest.mark.parametrize("text, masked", CONTROLS)
def test_control_is_unchanged(text, masked):
    assert quality.mask_credentials(text) == masked
