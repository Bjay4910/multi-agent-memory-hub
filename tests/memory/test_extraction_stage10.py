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


# -- B. common shapes: only the secret part is masked, the rest of the sentence is kept ------------------------------

# (shape, input, fake secret, expected masked text)
B_SHAPES = [
    ("markdown", "**Password**: sycamore38", "sycamore38", "**Password**: [REDACTED]"),
    ("markdown", "`api_key`: chestnut29", "chestnut29", "`api_key`: [REDACTED]"),
    ("markdown", '"password" is walnut64.', "walnut64", '"password" is [REDACTED].'),
    ("curl", "curl -u deploy:hazel71 https://api.example.com/v1", "hazel71",
     "curl -u deploy:[REDACTED] https://api.example.com/v1"),
    ("curl", "curl --user ops:maple82 https://api.example.com/v1", "maple82",
     "curl --user ops:[REDACTED] https://api.example.com/v1"),
    ("curl", "curl -b 'sid=cedar93' https://api.example.com/v1", "cedar93",
     "curl -b 'sid=[REDACTED]' https://api.example.com/v1"),
    ("curl", 'curl --cookie "session=birch44" https://api.example.com/v1', "birch44",
     'curl --cookie "session=[REDACTED]" https://api.example.com/v1'),
    ("cli-flags", "redis-cli -a quince55 -h cache.internal", "quince55", "redis-cli -a [REDACTED] -h cache.internal"),
    ("cli-flags", "mysql -u admin -prowan66 shop", "rowan66", "mysql -u admin -p[REDACTED] shop"),
    ("cli-flags", "mysql -u admin --password=aspen77 shop", "aspen77", "mysql -u admin --password=[REDACTED] shop"),
    ("cli-flags", "sshpass -p 'larch88' ssh ops@db.internal", "larch88",
     "sshpass -p '[REDACTED]' ssh ops@db.internal"),
    ("yaml-block", "password: |\n  spruce99", "spruce99", "password: |\n  [REDACTED]"),
    ("yaml-block", "api_key: >-\n  poplar11", "poplar11", "api_key: >-\n  [REDACTED]"),
    ("token-shapes", "Trail Tracker CI uses glpat-AbCdEfGhIjKlMnOpQrSt12 for pushes", "glpat-AbCdEfGhIjKlMnOpQrSt12",
     "Trail Tracker CI uses [REDACTED] for pushes"),
    ("token-shapes", "Webhook signing uses whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw in prod",
     "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "Webhook signing uses [REDACTED] in prod"),
    ("token-shapes", "https://acct.blob.core.windows.net/c/f.txt?sv=2021-08-06&sig=Ab3dEf%2BgHi9jKl%3D&se=2026",
     "Ab3dEf%2BgHi9jKl%3D", "https://acct.blob.core.windows.net/c/f.txt?sv=2021-08-06&sig=[REDACTED]&se=2026"),
    ("token-shapes", "Upload to https://acct.blob.core.windows.net/c?sig=Q1w2E3r4T5y6%3D", "Q1w2E3r4T5y6%3D",
     "Upload to https://acct.blob.core.windows.net/c?sig=[REDACTED]"),
    ("with-using", "Connect with password juniper89 to the staging box.", "juniper89",
     "Connect with password [REDACTED] to the staging box."),
    ("with-using", "Log in using token oak-77-ash today.", "oak-77-ash", "Log in using token [REDACTED] today."),
    ("labels", "login: admin/teak42", "teak42", "login: admin/[REDACTED]"),
    ("labels", "the login is ops/sequoia7 for staging", "sequoia7", "the login is ops/[REDACTED] for staging"),
    ("labels", "jwt: Ab3dEf7gHi9jKl", "Ab3dEf7gHi9jKl", "jwt: [REDACTED]"),
    ("labels", "totp = 482913", "482913", "totp = [REDACTED]"),
    ("labels", "hmac: f00dbabe99", "f00dbabe99", "hmac: [REDACTED]"),
    ("labels", "license key: ABCD-1234-EFGH", "ABCD-1234-EFGH", "license key: [REDACTED]"),
]

B_FALSE_POSITIVES = [
    ("markdown", "**Note**: the build is green."),
    ("markdown", "`token_count`: 42"),
    ("curl", "curl -u deploy https://api.example.com/v1"),
    ("curl", "sort -u names.txt"),
    ("cli-flags", "grep -a pattern notes.txt"),
    ("cli-flags", "redis-cli -h cache.internal ping"),
    ("cli-flags", "mysql -u admin -p shop"),
    ("yaml-block", "description: |\n  A log viewer for Harbor Log."),
    ("token-shapes", "GitLab tokens start with glpat-short"),
    ("token-shapes", "https://example.com/feed?signal=1&page=2"),
    ("token-shapes", "~~struck~~ text stays"),
    ("with-using", "Sign in with password protection enabled."),
    ("with-using", "We ship with token limits of 4096."),
    ("labels", "Our login page has a dark theme."),
    ("labels", "We added HMAC signing to webhooks."),
    ("labels", "Trail Tracker uses JWT tokens for mobile auth."),
]


@pytest.mark.parametrize("shape, text, secret, masked", B_SHAPES,
                         ids=[f"{s}-{i}" for i, (s, *_) in enumerate(B_SHAPES)])
def test_common_shape_masks_only_the_secret(shape, text, secret, masked):
    assert quality.mask_credentials(text) == masked
    assert secret in quality.withheld_values(text)


@pytest.mark.parametrize("shape, text", B_FALSE_POSITIVES, ids=[f"{s}-{i}" for i, (s, _) in enumerate(B_FALSE_POSITIVES)])
def test_common_shape_false_positive_is_not_masked(shape, text):
    assert quality.mask_credentials(text) == text


def test_every_b_shape_has_two_inputs_and_a_false_positive():
    shapes = {s for s, *_ in B_SHAPES}
    for shape in shapes:
        assert sum(s == shape for s, *_ in B_SHAPES) >= 2, shape
        assert any(s == shape for s, _ in B_FALSE_POSITIVES), shape


def test_tilde_code_fence_is_stripped_like_backticks():
    text = "My config:\n~~~\npassword = fir23\n~~~\nTrail Tracker deploys nightly."
    cleaned = quality.clean_message(text)
    assert "fir23" not in cleaned and "Trail Tracker deploys nightly." in cleaned


@pytest.mark.parametrize("shape, text, secret, masked", B_SHAPES[::3],
                         ids=[f"{s}-{i}" for i, (s, *_) in enumerate(B_SHAPES[::3])])
def test_common_shape_never_leaks(shape, text, secret, masked, tmp_path, monkeypatch):
    assert_never_leaks(f"{text}\nTrail Tracker deploys nightly.", secret, tmp_path, monkeypatch)


def test_property_holds_for_the_b_shapes():
    corpus = [(text, secret) for _, text, secret, _ in B_SHAPES]
    assert check_property(corpus) == []


@pytest.mark.parametrize("text, token", [
    ("Webhook secret whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw is set", "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"),
    ("the token glpat-AbCdEfGhIjKlMnOpQrSt12 was rotated", "glpat-AbCdEfGhIjKlMnOpQrSt12"),
    (f"my key {FAKE_SG} is active", FAKE_SG),
])
def test_a_token_inside_the_filler_of_a_label_match_is_still_masked(text, token):
    assert token not in quality.mask_credentials(text)
    assert token in quality.withheld_values(text)
