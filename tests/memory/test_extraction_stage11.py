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


# -- B. cheap shapes: 2+ inputs and a false positive each ------------------------------------------------------------

B_CASES = [
    ("changed-to", "I changed the password to hawthorn41.", "hawthorn41", "I changed the password to [REDACTED]."),
    ("changed-to", "We reset the api key to rowan52", "rowan52", "We reset the api key to [REDACTED]"),
    ("changed-to", "They updated our token to sorrel63 today.", "sorrel63", "They updated our token to [REDACTED]."),
    ("possessive", "The password's hazel74.", "hazel74", "The password's [REDACTED]."),
    ("possessive", "Trail Tracker token's linden85x now", "linden85x", "Trail Tracker token's [REDACTED] now"),
    ("as", "Use the token as willow96.", "willow96", "Use the token as [REDACTED]."),
    ("as", "Set the password as aspen-107 for staging", "aspen-107", "Set the password as [REDACTED] for staging"),
    ("env-glued", "PGPASSWORD=spruce118 psql -h db.internal", "spruce118", "PGPASSWORD=[REDACTED] psql -h db.internal"),
    ("env-glued", "export MYPASSWORD='cedar129'", "cedar129", "export MYPASSWORD='[REDACTED]'"),
    ("env-glued", "GITHUB_TOKEN=ghxfake130", "ghxfake130", "GITHUB_TOKEN=[REDACTED]"),
    ("login-spaced", "login: admin / teak141x", "teak141x", "login: admin / [REDACTED]"),
    ("login-spaced", "the login is ops / sequoia152 for staging", "sequoia152",
     "the login is ops / [REDACTED] for staging"),
    ("tokens", "publish with pypi-AgEIcHlwaS5vcmcCJGQ4Yjc5ZTAw tonight", "pypi-AgEIcHlwaS5vcmcCJGQ4Yjc5ZTAw",
     "publish with [REDACTED] tonight"),
    ("tokens", "vault gave hvs.CAESIJlU9XnE2Kq7Wn5bF3dP0aQ1 to CI", "hvs.CAESIJlU9XnE2Kq7Wn5bF3dP0aQ1",
     "vault gave [REDACTED] to CI"),
    ("tokens", "bot uses MTA0NjE4NzQ5MjM0NTY3ODkw.GaBcDe.abcdefghijklmnopqrstuvwxyz0123 here",
     "MTA0NjE4NzQ5MjM0NTY3ODkw.GaBcDe.abcdefghijklmnopqrstuvwxyz0123", "bot uses [REDACTED] here"),
    ("tokens", "Stripe sends whsec_test_MfKQ9r8GKYqrTwjUPD8I in dev", "whsec_test_MfKQ9r8GKYqrTwjUPD8I",
     "Stripe sends [REDACTED] in dev"),
    ("json", '{"secrets": ["alder163", "birch174"]}', "birch174", '{"secrets": ["[REDACTED]", "[REDACTED]"]}'),
    ("json", '{"api_keys": ["cedar185"]}', "cedar185", '{"api_keys": ["[REDACTED]"]}'),
    ("json", '{"Authorization": "Token dogwood196"}', "dogwood196", '{"Authorization": "Token [REDACTED]"}'),
    ("xml", "<password>elm207</password>", "elm207", "<password>[REDACTED]</password>"),
    ("xml", "<api_key>fir218</api_key>", "fir218", "<api_key>[REDACTED]</api_key>"),
    ("yaml-indent", "password: |2\n  yew229", "yew229", "password: |2\n  [REDACTED]"),
    ("yaml-indent", "api_key: >2-\n  zelkova230", "zelkova230", "api_key: >2-\n  [REDACTED]"),
]

B_FALSE_POSITIVES = [
    ("changed-to", "We reset the password policy to strict."),
    ("possessive", "The token's expiry is short."),
    ("as", "Use the key as a reference."),
    ("env-glued", "MAX_TOKENS=4096"),
    ("login-spaced", "login: admin"),
    ("tokens", "the pypi-project page and hvs.short notes for release 1.2.3.4"),
    ("json", '{"tags": ["alpha", "beta"]}'),
    ("xml", "<passwordPolicy>strict</passwordPolicy>"),
    ("yaml-indent", "description: |2\n  A log viewer for Harbor Log."),
]


@pytest.mark.parametrize("shape, text, secret, masked", B_CASES, ids=[f"{s}-{i}" for i, (s, *_) in enumerate(B_CASES)])
def test_b_shape_masks_the_value(shape, text, secret, masked):
    assert quality.mask_credentials(text) == masked
    assert secret in quality.withheld_values(text)


@pytest.mark.parametrize("shape, text", B_FALSE_POSITIVES, ids=[f"{s}-{i}" for i, (s, _) in enumerate(B_FALSE_POSITIVES)])
def test_b_shape_false_positive_is_not_masked(shape, text):
    assert quality.mask_credentials(text) == text


def test_every_b_shape_has_two_inputs_and_a_false_positive():
    for shape in {s for s, *_ in B_CASES}:
        assert sum(s == shape for s, *_ in B_CASES) >= 2, shape
        assert any(s == shape for s, _ in B_FALSE_POSITIVES), shape


@pytest.mark.parametrize("shape, text, secret, masked", B_CASES[::2],
                         ids=[f"{s}-{i}" for i, (s, *_) in enumerate(B_CASES[::2])])
def test_b_shape_never_leaks(shape, text, secret, masked, tmp_path, monkeypatch):
    assert_never_leaks(f"{text}\nTrail Tracker deploys nightly.", secret, tmp_path, monkeypatch)


def test_property_holds_for_the_b_cases():
    assert check_property([(t, s) for _, t, s, _ in B_CASES]) == []


# -- D. fuzz over the A and B shapes: invalid output in every third combination, the whole home scanned ---------------

import itertools  # noqa: E402
import random  # noqa: E402
import shutil  # noqa: E402

MSG_SHAPES = {
    "quote": "> {P}\n> Trail Tracker deploys nightly.",
    "warning": "Warning: {P} for Trail Tracker",
    "question": "Is {P} safe for Trail Tracker?",
    "after-prompt": "$ {P}\nTrail Tracker reads it at startup.",
    "indented-output": "$ cat notes.txt\n  {P}\nTrail Tracker reads that file.",
    "code-block": "My config:\n```\n{P}\n```\nTrail Tracker deploys nightly.",
    "tilde-block": "My config:\n~~~\n{P}\n~~~\nTrail Tracker deploys nightly.",
    "restated-question": "What should I do now that {P} for my Trail Tracker app?",
    "we-line": "We noted that {P} in Trail Tracker.\nTrail Tracker deploys nightly.",
}
# {L} label, {LK} label as a key, {V} value; LBR / RBR stand for { and }. Value kinds: "any" (incl. spaces),
# "token" (no spaces), "token9" (a digit appended, for rules that need one).
LABEL_FAMILIES = {
    "nested-paren": ("{L} is (outer({V})) in prod", "any"),
    "nested-square": ("{L}: [a[{V}]]", "any"),
    "nested-angle": ("{L}: <x<{V}>> for prod", "any"),
    "nested-brace": ("{L} = LBRgLBR{V}RBRRBR here", "any"),
    "bold-label-filler": ("**{L}** for Trail Tracker is {V}.", "any"),
    "code-label-filler": ("`{LK}` for Trail Tracker = {V}", "any"),
    "quoted-label-filler": ('"{L}" for Trail Tracker is {V}.', "any"),
    "with-quoted": ('Connect with {L} "{V}" to staging.', "any"),
    "using-single": ("Log in using {L} '{V}' today.", "any"),
    "changed-to": ("I changed the {L} to {V}.", "any"),
    "possessive": ("The {L}'s {V}9.", "token9"),
    "as": ("Use the {L} as {V}9.", "token9"),
    "xml": ("<{LK}>{V}</{LK}>", "any"),
    "yaml-indent": ("{L}: |2\n  {V}", "any"),
}
FREE_FAMILIES = {
    "curl-empty-user": ("curl -u :{V} https://api.example.com", "token"),
    "curl-quoted-space": ('curl -u "bot:{V} extra" https://api.example.com', "token"),
    "curl-attached": ("curl -ubot:{V} https://api.example.com", "token"),
    "curl-b-quoted": ("curl -b 'sid=blue {V}' https://api.example.com", "token"),
    "mysql-single": ("mysql -u admin -p'{V}' shop", "any"),
    "mysql-double": ('mysql -u admin -p"{V}" shop', "any"),
    "password-flag-quoted": ('tool --password="red {V}" --verbose', "any"),
    "sshpass-attached": ("sshpass -p'{V}' ssh ops@db.internal", "any"),
    "docker-login": ("docker login -u ops -p {V} registry.example.com", "token"),
    "env-glued": ("PGPASSWORD={V} psql -h db.internal", "token"),
    "env-glued-quoted": ("export MYPASSWORD='{V}'", "any"),
    "login-spaced": ("login: admin / {V}", "token"),
    "json-list": ('{"secrets": ["{V}", "spare1x"]}', "any"),
    "json-authorization": ('{"Authorization": "Token {V}"}', "token"),
    "basic": ("curl sends Basic {S} to the API", "token"),
    "pypi": ("publish with {S} tonight", "token"),
    "hvs": ("vault gave {S} to CI", "token"),
    "discord": ("bot uses {S} here", "token"),
    "whsec-test": ("Stripe sends {S} in dev", "token"),
}
# For token shapes the secret is the whole token as written.
TOKEN_SECRETS = {
    "basic": "YW{W}FhYWFh",
    "pypi": "pypi-AgEI{W}cHlwaS5vcmc",
    "hvs": "hvs.CAESIJ{W}lU9XnE2Kq7",
    "discord": "MTA0NjE4NzQ5MjM0NTY3ODkw.GaBcDe.{W}abcdefghijklmnopqrstuvwxyz0123",
    "whsec-test": "whsec_test_{W}MfKQ9r8GKYqr",
}
_WORDS = ["marmalade", "driftwood", "copperleaf", "saltmarsh", "bluegill", "ironbark", "foxglove"]
VALUE_SHAPES = ["plain", "word-digits", "hyphenated", "spaces", "long"]


def _fuzz_value(rng, kind, n):
    w, x, d = rng.choice(_WORDS), rng.choice(_WORDS), rng.randint(10, 99)
    shape = VALUE_SHAPES[n % 5]
    if kind in ("token", "token9") and shape == "spaces":
        shape = "word-digits"
    return {
        "plain": w, "word-digits": f"{w}{rng.randint(100, 9999)}", "hyphenated": f"{w}-{x}-{d}",
        "spaces": f"{w} {x} {d}x",
        "long": "".join(rng.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(14)),
    }[shape]


def fuzz11_cases():
    """(index, key, text, secret, bad_output). Fixed seed; every third combination gets invalid model output."""
    rng = random.Random(11)
    cases = []

    def add(key, phrase, secret, shape):
        i = len(cases)
        cases.append((i, f"{key}|{shape}", MSG_SHAPES[shape].replace("{P}", phrase), secret, i % 3 == 0))

    for n, (label, family, shape) in enumerate(itertools.product(quality.SECRET_LABELS, LABEL_FAMILIES, MSG_SHAPES)):
        template, kind = LABEL_FAMILIES[family]
        value = _fuzz_value(rng, kind, n)
        phrase = (template.replace("{LK}", label.replace(" ", "_")).replace("{L}", label).replace("{V}", value)
                  .replace("LBR", "{").replace("RBR", "}"))
        add(f"{family}|{label}", phrase, value + "9" if kind == "token9" else value, shape)
    for n, (family, vshape, shape) in enumerate(itertools.product(FREE_FAMILIES, range(5), MSG_SHAPES)):
        template, kind = FREE_FAMILIES[family]
        value = _fuzz_value(rng, kind, vshape)
        if family in TOKEN_SECRETS:
            value = TOKEN_SECRETS[family].replace("{W}", re.sub(r"[^A-Za-z0-9]", "", value))
            add(f"{family}|v{vshape}", template.replace("{S}", value), value, shape)
        else:
            add(f"{family}|v{vshape}", template.replace("{V}", value), value, shape)
    return cases


def run_fuzz11(root, monkeypatch, cases=None):
    cases = cases if cases is not None else fuzz11_cases()
    leaks, queued = [], 0
    for i, key, text, secret, bad in cases:
        case_dir = root / f"c{i}"
        found, was_queued = run_one(text, secret, case_dir, monkeypatch, bad_output=bad)
        queued += was_queued
        if found:
            leaks.append((key, found))
        shutil.rmtree(case_dir, ignore_errors=True)
    return len(cases), leaks, queued


def test_fuzz11_matrix_size():
    expected = (len(quality.SECRET_LABELS) * len(LABEL_FAMILIES) * len(MSG_SHAPES)
                + len(FREE_FAMILIES) * 5 * len(MSG_SHAPES))
    assert len(fuzz11_cases()) == expected


def test_fuzz11_matrix_never_leaks_and_really_writes_the_queue(tmp_path, monkeypatch):
    total, leaks, queued = run_fuzz11(tmp_path, monkeypatch)
    print(f"fuzz11: {total} combinations, {len(leaks)} leaks, {queued} wrote memory_queue")
    assert queued > 0, "no combination wrote memory_queue: the queue path is not exercised"
    assert not leaks, f"{len(leaks)} leaks, first: {leaks[:20]}"


def test_property_holds_on_the_fuzz11_matrix():
    misses = check_property([(text, secret) for _, _, text, secret, _ in fuzz11_cases()[::4]])
    assert not misses, misses[:20]


def test_discord_style_token_with_a_long_third_segment_is_masked_whole():
    token = "MTA0NjE4NzQ5MjM0NTY3ODkw.GaBcDe.marmalade114abcdefghijklmnopqrstuvwxyz0123"  # third segment 42 chars
    assert quality.mask_credentials(f"bot uses {token} here") == "bot uses [REDACTED] here"
    assert token in quality.withheld_values(f"bot uses {token} here")
