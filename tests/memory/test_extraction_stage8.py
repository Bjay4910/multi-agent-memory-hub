"""Stage 8: secrets that could still be stored after round 3 (security over recall). Scripted models only.

Gap 1: filler words between a secret label and its connector ("The password for Harbor Log is now42abc.").
Gap 2: a value that starts with a status word ("The password is not-so-secret-42.").
Plus a node-level post-filter and a deterministic fuzz matrix over labels, fillers, connectors, values and shapes.
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


def msgs(*user, sid="s8"):
    return [Message(sid, "user", 0, "user", u) for u in user]


def node(name, summary, type="fact", confidence=0.9, evidence=None):
    n = {"type": type, "name": name, "summary": summary, "confidence": confidence}
    if evidence is not None:
        n["evidence"] = evidence
    return n


def run(messages, agent, db):
    result = asyncio.run(ex.extract_session(messages[0].session_id, messages, lambda name: agent, store_path=db))
    nodes = []
    if db.exists():
        with MemoryStore(db) as store:
            nodes = store.all_nodes()
    return result, nodes


def hostile(value):
    """A model that tries every way to store ``value``."""
    def answer(prompt):
        return json.dumps({"nodes": [
            node("Harbor Log", f"Uses {value}.", "project", evidence="Harbor Log"),
            node(f"Key {value}", "A key.", "fact", evidence="Harbor Log"),
            node("Harbor Log Config", "Config.", "project", evidence=value),
            node("Prompt Echo", prompt[-400:], "fact", evidence="Harbor Log"),
            node("Harbor Log Access", f"The password for Harbor Log is {value}.", "fact", evidence="Harbor Log"),
        ], "edges": []})
    return answer


SHAPES = {
    "quote": "> {P}\n> Trail Tracker deploys nightly.",
    "warning": "Warning: {P} for Trail Tracker",
    "question": "Is {P} safe for Trail Tracker?",
    "after-prompt": "$ {P}\nTrail Tracker reads it at startup.",
    "indented-output": "$ cat notes.txt\n  {P}\nTrail Tracker reads that file.",
    "code-block": "My config:\n```\n{P}\n```\nTrail Tracker deploys nightly.",
    "restated-question": "What should I do now that {P} for my Trail Tracker app?",
    "we-line": "We noted that {P} in Trail Tracker.\nTrail Tracker deploys nightly.",
}


def _words_in(value, text):
    return [w for w in value.split() if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text)]


def check_no_leak(phrase_text, value, tmp_dir):
    """Run the hostile model on ``phrase_text``; return what leaked (empty list = nothing)."""
    agent = Scripted(hostile(value))
    _, nodes = run(msgs(phrase_text), agent, tmp_dir / "memory.db")
    leaks = []
    if any(value in p or _words_in(value, p) for p in agent.prompts):
        leaks.append("prompt")
    for n in nodes:
        if value in f"{n.name} {n.summary}" or _words_in(value, f"{n.name} {n.summary}"):
            leaks.append(f"node {n.name}")
    return leaks


def scan_files(root, values):
    blob = b"".join(Path(d, f).read_bytes() for d, _, names in os.walk(root) for f in names)
    return sorted(v for v in values if v.encode() in blob)


# -- gap 1: filler words between the label and the connector -----------------------------------------------------

GAP1 = {
    "for-project": ("The password for Harbor Log is now42abc.", "now42abc"),
    "on-server": ("the api key on the staging server was qz81mbt", "qz81mbt"),
    "of-account": ("The token of my admin account is now pl4netx", "pl4netx"),
    "set-to": ("the passcode for the front door set to 9a8b7c", "9a8b7c"),
    "colon": ("Password for prod: driftwood", "driftwood"),
    "equals": ("the secret for Harbor Log equals copperleaf", "copperleaf"),
    "parentheses": ("The password (for prod) is gl4cier", "gl4cier"),
    "dotted-name": ("the token for harbor.log is m00nbeam", "m00nbeam"),
    "quoted-name": ('the key for "Harbor Log" is r1verstone', "r1verstone"),
}


@pytest.mark.parametrize("case", list(GAP1))
def test_filler_words_between_label_and_connector_are_masked(case):
    phrase, value = GAP1[case]
    assert value not in quality.clean_message(phrase)
    assert value in quality.credential_values(phrase)


@pytest.mark.parametrize("case", list(GAP1))
def test_filler_words_secret_never_reaches_prompt_file_or_node(case, tmp_path, isolated_home):
    phrase, value = GAP1[case]
    assert check_no_leak(phrase, value, tmp_path) == []
    assert scan_files(tmp_path, [value]) == []


# -- gap 2: a value that starts with a status word is still a value ---------------------------------------------

GAP2 = {
    "not-hyphen": ("The password is not-so-secret-42.", "not-so-secret-42"),
    "in-vault": ("The api key is in-vault-7781.", "in-vault-7781"),
    "set-digits": ("the token is set4you99", "set4you99"),
    "valid-word": ("my passphrase is valid forest lantern", "forest lantern"),
}


@pytest.mark.parametrize("case", list(GAP2))
def test_status_word_values_are_masked(case):
    phrase, value = GAP2[case]
    cleaned = quality.clean_message(phrase)
    assert value not in cleaned and not _words_in(value, cleaned)


@pytest.mark.parametrize("case", list(GAP2))
def test_status_word_secret_never_reaches_prompt_file_or_node(case, tmp_path, isolated_home):
    phrase, value = GAP2[case]
    assert check_no_leak(phrase, value, tmp_path) == []
    assert scan_files(tmp_path, [value]) == []


def test_the_accepted_trade_off_a_status_sentence_is_masked_too():
    # Losing "the token is valid for one hour" is the accepted cost of closing gap 2.
    assert "valid for one hour" not in quality.clean_message("the token is valid for one hour")


# -- node-level post-filter --------------------------------------------------------------------------------------

@pytest.mark.parametrize("field, text", [
    ("summary", "The password for the prod database was set to quietfern."),
    ("summary", "The key for Lumen Budget is abc123xyz."),
    ("name", "Harbor Log token is now zz99"),
    ("evidence", "my passphrase equals tidal moon"),
    ("summary", "pwd: orange"),
])
def test_node_with_label_connector_and_value_is_dropped(field, text):
    n = node("Harbor Log", "A log viewer.", "project", evidence="Harbor Log")
    n[field] = text
    source = f"Harbor Log is a log viewer. {text}"
    cleaned, dropped = quality.clean_extraction({"nodes": [n], "edges": []}, source)
    assert cleaned["nodes"] == [] and dropped["secret"] == 1


def test_node_without_a_value_is_still_kept():
    n = node("PassVault", "An offline password manager in Rust.", "project",
             evidence="PassVault, an offline password manager in Rust")
    cleaned, _ = quality.clean_extraction({"nodes": [n], "edges": []},
                                          "I'm building PassVault, an offline password manager in Rust.")
    assert [x["name"] for x in cleaned["nodes"]] == ["PassVault"]


# -- fuzz matrix ------------------------------------------------------------------------------------------------

FUZZ_LABELS = quality.SECRET_LABELS
FUZZ_FILLER_POOL = ["for", "the", "Harbor", "Log", "prod", "server", "on", "staging", "admin", "account", "of", "my"]
FUZZ_CONNECTORS = ["is", "was", "=", ":", "is now", "set to", "equals"]
FUZZ_VALUE_SHAPES = ["plain", "word-digits", "hyphenated", "spaces", "long"]
FUZZ_SEED = 8


def fuzz_cases():
    """Every label x filler count (0-6) x connector x value shape x message shape; fillers and values are
    drawn from a fixed seed, so the matrix is the same on every run."""
    rng = random.Random(FUZZ_SEED)
    plain = ["marmalade", "driftwood", "copperleaf", "saltmarsh", "bluegill", "ironbark", "foxglove"]
    cases = []
    for i, (label, fill, conn, vshape, shape) in enumerate(itertools.product(
            FUZZ_LABELS, range(7), FUZZ_CONNECTORS, FUZZ_VALUE_SHAPES, SHAPES)):
        filler = " ".join(rng.choice(FUZZ_FILLER_POOL) for _ in range(fill))
        word = rng.choice(plain)
        value = {
            "plain": word,
            "word-digits": f"{word}{rng.randint(100, 9999)}",
            "hyphenated": f"{word}-{rng.choice(plain)}-{rng.randint(10, 99)}",
            "spaces": f"{word} {rng.choice(plain)} {rng.randint(10, 99)}x",
            "long": "".join(rng.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(14)),
        }[vshape]
        head = f"{label} {filler}".strip()
        phrase = f"{head}{conn} {value}" if conn in (":",) else f"{head} {conn} {value}"
        cases.append((i, f"{label}|{fill}|{conn}|{vshape}|{shape}", SHAPES[shape].replace("{P}", phrase), value))
    return cases


def test_fuzz_matrix_size():
    assert len(fuzz_cases()) == len(FUZZ_LABELS) * 7 * len(FUZZ_CONNECTORS) * len(FUZZ_VALUE_SHAPES) * len(SHAPES)


def run_fuzz(root, monkeypatch):
    """Every combination in its own home and database (memory.db, -wal, -shm, memory_queue, logs), scanned for
    the value and then deleted. Returns (number of combinations, leaks as (combination, where))."""
    cases = fuzz_cases()
    leaks = []
    for i, key, text, value in cases:
        case_dir = root / f"c{i}"
        monkeypatch.setenv("AGENT_ROOM_HOME", str(case_dir / "home"))
        found = check_no_leak(text, value, case_dir)
        found += [f"file {v}" for v in scan_files(case_dir, [value])]
        if found:
            leaks.append((key, found))
        shutil.rmtree(case_dir, ignore_errors=True)
    return len(cases), leaks


def test_fuzz_matrix_never_leaks(tmp_path, monkeypatch):
    total, leaks = run_fuzz(tmp_path, monkeypatch)
    print(f"fuzz: {total} combinations, {len(leaks)} leaks")
    assert not leaks, f"{len(leaks)} leaks, first: {leaks[:20]}"  # no list diff: it would be huge
