"""Stage 9: findings from the independent review of round 3 + stage 8. Scripted models only, fake values only.

A. withheld values and the prompt mask share one set of rules.
B. matching runs on a normalized copy (NFKC, zero-width removed, Unicode spaces, a few Cyrillic lookalikes).
C. new shapes: JSON / dict, URL userinfo, URL query, base64 candidates, more labels; plus false-positive guards.
D. a fuzz matrix that also writes memory_queue (invalid model output) and scans the whole temporary home.
"""

import asyncio
import itertools
import json
import os
import random
import re
import shutil
from pathlib import Path

import pytest

from room.agents.base import Agent
from room.memory import extractor as ex
from room.memory import quality
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
    """Valid output that tries every way to store ``value``."""
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
    """Invalid output (twice: first try and the repair), so the session is queued in memory_queue;
    the invalid field quotes the value, so it lands in the queued error message unless masked."""
    bad = json.dumps({"nodes": [{"type": value, "name": f"Key {value}", "summary": value, "confidence": 0.9}],
                      "edges": []})
    return [bad, bad]


def _words_in(value, text):
    return [w for w in re.split(r"[\s:]+", value)
            if len(w) >= 3 and re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text)]


def run_one(text, value, case_dir, monkeypatch, bad_output=False):
    """One extraction in its own home and database; returns (leaks, queued?)."""
    monkeypatch.setenv("AGENT_ROOM_HOME", str(case_dir / "home"))
    agent = Scripted(*(invalid(value) if bad_output else [hostile(value)]))
    db = case_dir / "memory.db"
    asyncio.run(ex.extract_session("s9", [Message("s9", "user", 0, "user", text)], lambda name: agent, store_path=db))
    leaks = []
    if any(value in p or _words_in(value, p) for p in agent.prompts):
        leaks.append("prompt")
    if db.exists():
        with MemoryStore(db) as store:
            for n in store.all_nodes():
                if value in f"{n.name} {n.summary}" or _words_in(value, f"{n.name} {n.summary}"):
                    leaks.append(f"node {n.name}")
    files = [Path(d, f) for d, _, names in os.walk(case_dir) for f in names]
    for path in files:
        data = path.read_bytes()
        if value.encode() in data or any(w.encode() in data for w in _words_in(value, data.decode("utf-8", "replace"))):
            leaks.append(f"file {path.relative_to(case_dir)}")
    queued = any("memory_queue" in str(p) for p in files)
    return leaks, queued


# -- A. withheld values and the mask share one set of rules ------------------------------------------------------

NEXT_LINE = {
    "colon-next-line": ("Trail Tracker password:\nmagnolia77", "magnolia77"),
    "equals-next-line": ("API_KEY=\nfirsthalf99", "firsthalf99"),
}


@pytest.mark.parametrize("case", list(NEXT_LINE))
def test_value_on_the_next_line_is_masked_and_withheld(case):
    text, value = NEXT_LINE[case]
    assert value not in quality.clean_message(text)
    assert value in quality.withheld_values(text)


@pytest.mark.parametrize("case", list(NEXT_LINE))
def test_value_on_the_next_line_never_leaks(case, tmp_path, monkeypatch):
    text, value = NEXT_LINE[case]
    assert run_one(text, value, tmp_path, monkeypatch)[0] == []


def test_url_query_value_ends_at_ampersand():
    text = "https://api.example.com/v1?api_key=hibiscus44&v=1"
    assert quality.mask_credentials(text) == "https://api.example.com/v1?api_key=[REDACTED]&v=1"
    withheld = quality.withheld_values(text)
    assert "hibiscus44" in withheld and "hibiscus44&v=1" not in withheld


# -- A. property: whatever the prompt masks, the withheld set holds exactly -------------------------------------

def property_corpus_a():
    """The A inputs plus every label x connector (incl. a next-line value) x value shape x message shape."""
    rng = random.Random(91)
    corpus = [NEXT_LINE[k] for k in NEXT_LINE] + [("https://api.example.com/v1?api_key=hibiscus44&v=1", "hibiscus44")]
    shapes = ["{P}", "> {P}\n> Trail Tracker deploys nightly.", "Is {P} safe for Trail Tracker?",
              "We noted that {P} in Trail Tracker.", "https://x.example.com/?{K}={V}&page=2"]
    for label, conn, shape in itertools.product(quality.SECRET_LABELS, ["is", ":", "=", ":\n", "=\n", "set to"],
                                                shapes):
        value = rng.choice(["marmalade", "driftwood7731", "copper-leaf-42", "quiet river 12x"])
        phrase = f"{label}{conn}{value}" if conn.endswith("\n") else (
            f"{label}{conn} {value}" if conn in (":", "=") else f"{label} {conn} {value}")
        text = shape.replace("{K}", label.replace(" ", "_")).replace("{V}", value.replace(" ", "")).replace("{P}", phrase)
        corpus.append((text, value.replace(" ", "") if "{K}" in shape else value))
    return corpus


def test_property_every_masked_value_is_withheld_exactly():
    misses = []
    corpus = property_corpus_a()
    for text, value in corpus:
        masked = quality.mask_credentials(quality.premask_text(text))
        withheld = quality.withheld_values(text)
        spans = quality.credential_values(quality.premask_text(text))
        covered = value in withheld or (any(value in s for s in spans)  # over-masked: inside a withheld span,
                                        and all(w in withheld for w in value.split() if len(w) >= 3))  # words too
        if value not in masked and not covered:
            misses.append((text, value))
        for span in quality.credential_values(quality.premask_text(text)):
            if span not in withheld:
                misses.append((text, span))
    assert len(corpus) > 300
    assert not misses, misses[:20]


# -- B. normalize before matching ---------------------------------------------------------------------------------

NORMALIZED = {
    "nbsp": ("Trail Tracker password\u00a0is\u00a0garnet44", "garnet44"),
    "fullwidth-colon": ("password\uff1a violet66", "violet66"),
    "fullwidth-equals": ("API_KEY\uff1d topaz99", "topaz99"),
    "zero-width": ("PASS\u200bWORD: indigo44", "indigo44"),
    "cyrillic-a": ("p\u0430ssword is orchid55", "orchid55"),
    "em-dash": ("password \u2014 quartz55", "quartz55"),
    "arrow": ("My password for Trail Tracker -> marigold55.", "marigold55"),
    "will-be": ("My password for Trail Tracker will be juniper66.", "juniper66"),
}


@pytest.mark.parametrize("case", list(NORMALIZED))
def test_normalized_shapes_are_masked_and_withheld(case):
    text, value = NORMALIZED[case]
    assert value not in quality.clean_message(text)
    assert value in quality.withheld_values(text)


@pytest.mark.parametrize("case", list(NORMALIZED))
def test_normalized_shapes_never_leak(case, tmp_path, monkeypatch):
    text, value = NORMALIZED[case]
    assert run_one(text, value, tmp_path, monkeypatch)[0] == []


def test_normalizing_never_changes_the_text_outside_the_value():
    text = "PASS\u200bWORD: indigo44, for Trail\u00a0Tracker"
    assert quality.mask_credentials(text) == "PASS\u200bWORD: [REDACTED], for Trail\u00a0Tracker"


# -- C. new shapes ------------------------------------------------------------------------------------------------

STRUCTURED = {
    "json": ('{"password": "dahlia77"}', ["dahlia77"], '{"password": "[REDACTED]"}'),
    "python-dict": ("{'password': 'anise55'}", ["anise55"], "{'password': '[REDACTED]'}"),
    "nested-json": ('{"auth": {"pwd": "tulip66"}}', ["tulip66"], '{"auth": {"pwd": "[REDACTED]"}}'),
    "two-keys": ('{"api_key": "peony8899", "token": "iris4455"}', ["peony8899", "iris4455"],
                 '{"api_key": "[REDACTED]", "token": "[REDACTED]"}'),
}


@pytest.mark.parametrize("case", list(STRUCTURED))
def test_json_and_dict_values_are_masked_and_the_rest_kept(case):
    text, values, masked = STRUCTURED[case]
    assert quality.mask_credentials(text) == masked
    for value in values:
        assert value in quality.withheld_values(text)


USERINFO = {
    "postgres": ("postgres://admin:sycamore99@db.example.com:5432/prod", "admin:sycamore99", "sycamore99",
                 "postgres://[REDACTED]@db.example.com:5432/prod"),
    "https": ("https://alice:buttercup77@git.internal.local/repo.git", "alice:buttercup77", "buttercup77",
              "https://[REDACTED]@git.internal.local/repo.git"),
    "redis": ("redis://:verbena55@cache.internal:6379/0", ":verbena55", "verbena55",
              "redis://[REDACTED]@cache.internal:6379/0"),
    "mongodb-srv": ("mongodb+srv://ops:tarragon88@cluster0.example.net/db", "ops:tarragon88", "tarragon88",
                    "mongodb+srv://[REDACTED]@cluster0.example.net/db"),
}


@pytest.mark.parametrize("case", list(USERINFO))
def test_url_userinfo_is_masked_whole(case):
    text, userinfo, password, masked = USERINFO[case]
    assert quality.mask_credentials(text) == masked
    withheld = quality.withheld_values(text)
    assert userinfo in withheld and password in withheld


@pytest.mark.parametrize("case", list(USERINFO))
def test_url_userinfo_never_leaks(case, tmp_path, monkeypatch):
    text, _, password, _ = USERINFO[case]
    assert run_one(f"Trail Tracker connects to {text} at startup.", password, tmp_path, monkeypatch)[0] == []


@pytest.mark.parametrize("param", ["api_key", "auth", "token", "password", "secret", "key"])
def test_url_query_secrets_are_masked(param):
    text = f"https://api.example.com/v2/items?{param}=lilac{len(param)}x9&page=2"
    assert quality.mask_credentials(text) == f"https://api.example.com/v2/items?{param}=[REDACTED]&page=2"
    assert f"lilac{len(param)}x9" in quality.withheld_values(text)


BASE64 = {
    "long-candidate": ("Trail Tracker uses Ab3dEf7gHi9jKl1m/No3pQr5sTu7vWx9yZa1bCd3eFg= for sync.",
                       "Ab3dEf7gHi9jKl1m/No3pQr5sTu7vWx9yZa1bCd3eFg="),
    "short-padded-after-label": ("my token QWxhZGRpbjpvcGVu== for Trail Tracker", "QWxhZGRpbjpvcGVu=="),
    "basic-auth-header": ("auth header: Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw=="),
}


@pytest.mark.parametrize("case", list(BASE64))
def test_base64_candidates_are_masked(case):
    text, value = BASE64[case]
    assert value not in quality.clean_message(text)
    assert value in quality.withheld_values(text)


MORE_LABELS = {
    "pass": "pass: almond42", "pw": "pw = almond43", "pwd": "pwd is almond44", "pin": "the PIN is 4821",
    "otp": "otp: 739201", "2fa-code": "my 2fa code is 551903", "recovery-code": "recovery code: wx7k-q2m9",
    "session-cookie": "session cookie = sid88almond", "cookie": "cookie: almond45sid", "psk": "psk is almond46",
    "secret-code": "the secret-code is almond47", "seed-phrase": "seed phrase: tiger ocean almond lamp",
    "auth-header": "auth header: Bearer almond48almond48", "credentials": "credentials: admin / almond49",
    "credentials-are": "the credentials are almond50", "french": "mot de passe : lavande42",
    "spanish": "contraseña: girasol42", "german": "Passwort: kornblume42", "chinese": "密码: mudan42",
    "japanese": "パスワード: sakura42",
}
MORE_LABEL_VALUES = {
    "pass": "almond42", "pw": "almond43", "pwd": "almond44", "pin": "4821", "otp": "739201",
    "2fa-code": "551903", "recovery-code": "wx7k-q2m9", "session-cookie": "sid88almond", "cookie": "almond45sid",
    "psk": "almond46", "secret-code": "almond47", "seed-phrase": "tiger ocean almond lamp",
    "auth-header": "almond48almond48", "credentials": "admin / almond49", "credentials-are": "almond50",
    "french": "lavande42", "spanish": "girasol42", "german": "kornblume42", "chinese": "mudan42",
    "japanese": "sakura42",
}


@pytest.mark.parametrize("case", list(MORE_LABELS))
def test_more_labels_are_masked_and_withheld(case):
    text, value = MORE_LABELS[case], MORE_LABEL_VALUES[case]
    cleaned = quality.clean_message(f"{text}\nTrail Tracker deploys nightly.")
    assert value not in cleaned and not _words_in(value, cleaned)
    assert value in quality.withheld_values(text)


@pytest.mark.parametrize("text", [
    "The password policy requires 12 characters.",
    "Reset your password from the settings page.",
    "I use a pin in my map.",
    "The Basic plan is cheap.",
    "Trail Tracker uses JWT tokens for mobile auth.",
    "I'm building PassVault, an offline password manager in Rust.",
    "Muse has a daily limit of 200,000 tokens.",
    "We renamed PRIMARY_KEY to USER_ID in Harbor Log.",
    "Git commit 3f9c2a1b7d4e6f8091a2b3c4d5e6f708192a3b4c fixed the build.",
])
def test_false_positive_guard_ordinary_sentences_are_not_masked(text):
    assert quality.mask_credentials(text) == text


def property_corpus_bc():
    corpus = [NORMALIZED[k] for k in NORMALIZED]
    corpus += [(STRUCTURED[k][0], v) for k in STRUCTURED for v in STRUCTURED[k][1]]
    corpus += [(USERINFO[k][0], USERINFO[k][1]) for k in USERINFO]
    corpus += [(BASE64[k][0], BASE64[k][1]) for k in BASE64]
    corpus += [(MORE_LABELS[k], MORE_LABEL_VALUES[k]) for k in MORE_LABELS]
    return corpus


def test_property_holds_for_normalized_and_new_shapes():
    misses = []
    for text, value in property_corpus_bc():
        prepared = quality.premask_text(text)
        spans = quality.credential_values(prepared)
        withheld = quality.withheld_values(text)
        if not spans:
            misses.append((text, "nothing masked"))
        covered = value in withheld or (any(value in s for s in spans)
                                        and all(w in withheld for w in value.split() if len(w) >= 3))
        if not covered:
            misses.append((text, value))
        misses += [(text, s) for s in spans if s not in withheld]
    assert not misses, misses[:20]
