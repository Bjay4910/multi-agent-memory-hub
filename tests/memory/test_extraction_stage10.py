"""Stage 10: findings from the second independent review. Scripted models only, fake values only.

A. claimed-handled leaks: quoted and wrapped values, one rule set with redact_secrets, URL query
   boundary, SendGrid tokens.
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
    asyncio.run(ex.extract_session("s10", [Message("s10", "user", 0, "user", text)], lambda name: agent,
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


# -- A1. quoted values: mask through the matching closing quote ---------------------------------------------------

QUOTED = {
    "comma": ('{"password": "alpha,beta99", "env": "prod"}', "alpha,beta99",
              '{"password": "[REDACTED]", "env": "prod"}'),
    "semicolon-env": ('DB_PASSWORD="p@ss;word88"', "p@ss;word88", 'DB_PASSWORD="[REDACTED]"'),
    "paren": ('{"password": "ab)cd99", "env": "prod"}', "ab)cd99", '{"password": "[REDACTED]", "env": "prod"}'),
    "escaped-quote": ('{"password": "pre\\"post77", "env": "prod"}', 'pre\\"post77',
                      '{"password": "[REDACTED]", "env": "prod"}'),
}


@pytest.mark.parametrize("case", list(QUOTED))
def test_quoted_value_is_masked_through_the_closing_quote(case):
    text, value, masked = QUOTED[case]
    assert quality.mask_credentials(text) == masked
    assert value in quality.withheld_values(text)


@pytest.mark.parametrize("case", list(QUOTED))
def test_quoted_value_never_leaks(case, tmp_path, monkeypatch):
    text, value, _ = QUOTED[case]
    secret = value.split('\\"')[-1]  # for pre\\"post77 the scan looks for "post77"
    assert_never_leaks(f"Trail Tracker config: {text}", secret, tmp_path, monkeypatch)


# -- A2. wrapped or prefixed values: the wrapper is masked with the value ----------------------------------------

WRAPPED = {
    "parentheses": ("password is (orchid77) in prod.", "orchid77", "password is [REDACTED] in prod."),
    "angle": ("password: <topaz66>", "topaz66", "password: [REDACTED]"),
    "equals-prefix": ("api_key: ==garnet55", "garnet55", "api_key: [REDACTED]"),
}


@pytest.mark.parametrize("case", list(WRAPPED))
def test_wrapped_value_is_masked_with_its_wrapper(case):
    text, value, masked = WRAPPED[case]
    assert quality.mask_credentials(text) == masked
    assert any(value in w for w in quality.withheld_values(text)) and value in quality.withheld_values(text)


@pytest.mark.parametrize("case", list(WRAPPED))
def test_wrapped_value_never_leaks(case, tmp_path, monkeypatch):
    text, value, _ = WRAPPED[case]
    assert_never_leaks(f"Trail Tracker notes: {text}", value, tmp_path, monkeypatch)


# -- A3. one rule set: whatever redact_secrets masks in the prompt is withheld too -------------------------------

def redact_masked(text):
    return quality.redact_masked_substrings(text)


def test_yaml_value_after_blank_lines_is_masked_by_both_layers_and_withheld():
    text = "password:\n\n  amber22"
    assert "amber22" not in quality.clean_message(text)
    assert "amber22" in quality.withheld_values(text)


def test_redact_masked_substrings_are_found_by_comparing_before_and_after():
    raw = "Trail Tracker uses token=violet94&filter[a]=1 and Xq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4 too"
    found = quality.redact_masked_substrings(raw)
    assert "violet94&filter[a]=1" in found and "Xq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4" in found
    # what it masks in the extraction prompt (after our own mask) is withheld
    for value in quality.redact_masked_substrings(quality.clean_message(raw)):
        assert value in quality.withheld_values(raw)
    assert "Xq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4" in quality.withheld_values(raw)


@pytest.mark.parametrize("text", [
    "Trail Tracker uses token=violet94&filter[a]=1 for search",
    "password:\n\n  amber22",
    "Trail Tracker key Xq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4 rotates weekly",
    'secret: "hunter22x"',
])
def test_every_value_redact_secrets_masks_in_the_prompt_is_withheld(text):
    prompt = redact_secrets(quality.clean_message(text))
    withheld = quality.withheld_values(text)
    for value in quality.redact_masked_substrings(quality.clean_message(text)):
        assert value in withheld, value
    assert "[REDACTED]" in prompt


# -- A4. URL query boundary ---------------------------------------------------------------------------------------

def test_query_value_ends_at_ampersand_before_a_bracketed_parameter():
    text = "https://api.example.com/search?token=violet94&filter[a]=1"
    assert quality.mask_credentials(text) == "https://api.example.com/search?token=[REDACTED]&filter[a]=1"
    withheld = quality.withheld_values(text)
    assert "violet94" in withheld and "violet94&filter[a]=1" not in withheld


# -- A5. SendGrid ---------------------------------------------------------------------------------------------------

FAKE_SG = "SG." + "abcdefghijABCDEFGHIJ12" + "." + "0123456789abcdefghijABCDEFGHIJklmnopqrstu12"


def test_sendgrid_token_is_masked_whole():
    text = f"Trail Tracker mails through SendGrid with {FAKE_SG} in prod."
    assert quality.mask_credentials(text) == "Trail Tracker mails through SendGrid with [REDACTED] in prod."
    assert FAKE_SG in quality.withheld_values(text)


def test_sendgrid_token_never_leaks(tmp_path, monkeypatch):
    assert_never_leaks(f"Trail Tracker mails with {FAKE_SG} in prod.", "abcdefghijABCDEFGHIJ12", tmp_path, monkeypatch)


# -- A3 property: every value either layer masks is withheld --------------------------------------------------------

def a_corpus():
    corpus = [(t, v) for t, v, _ in QUOTED.values()] + [(t, v) for t, v, _ in WRAPPED.values()]
    corpus += [("password:\n\n  amber22", "amber22"), ("https://api.example.com/search?token=violet94&filter[a]=1",
                                                       "violet94"), (f"mail via {FAKE_SG} now", FAKE_SG)]
    return corpus


def check_property(corpus):
    misses = []
    for text, value in corpus:
        withheld = quality.withheld_values(text)
        prepared = quality.premask_text(text)
        ours = quality.credential_values(prepared) | quality.credential_values(text)
        theirs = set()
        for source in (quality.mask_credentials(prepared), quality.clean_message(text)):  # the prompt forms
            theirs |= quality.redact_masked_substrings(source)
        misses += [(text, v) for v in ours | theirs if v not in withheld]
        masked_anywhere = value not in redact_secrets(quality.mask_credentials(prepared))
        if value in prepared and masked_anywhere and not (
                value in withheld or any(value in w for w in withheld)):
            misses.append((text, value))
    return misses


def test_property_every_value_masked_by_either_layer_is_withheld():
    assert check_property(a_corpus()) == []
