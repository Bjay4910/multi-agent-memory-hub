"""Extraction round 3: findings from the independent review of round 2. Scripted models only.

Each block is named after the review finding (A-G) it covers.
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
from room.memory.store import MemoryStore
from room.transcript import Message

DOCS = Path(__file__).resolve().parents[2] / "docs" / "MEMORY.md"

# Fake secrets, split so no scanner sees a whole token in the source.
FAKE_GHP = "gh" "p_" + "T8r4L2k9W1q7Z3x6C5v0B8n2M4a6S1d3F9g7"            # 40 characters
FAKE_SKA = "sk-" "ant-" "api03-" + "Hq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4Ai6P"  # 53 characters
assert len(FAKE_GHP) == 40 and len(FAKE_SKA) == 53


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


def msgs(*user, sid="r3"):
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


# -- A. ordinary technical words about tokens, passwords and keys are facts, not secrets -----------------------

@pytest.mark.parametrize("text, kept", [
    ("Trail Tracker uses JWT tokens for mobile auth.",
     node("Trail Tracker", "Trail Tracker uses JWT tokens for mobile auth.", "project",
          evidence="Trail Tracker uses JWT tokens for mobile auth")),
    ("I'm building PassVault, an offline password manager in Rust.",
     node("PassVault", "An offline password manager in Rust.", "project",
          evidence="PassVault, an offline password manager in Rust")),
    ("Muse has a daily limit of 200,000 tokens.",
     node("Muse", "Muse has a daily limit of 200,000 tokens.", "project",
          evidence="Muse has a daily limit of 200,000 tokens")),
    ("We renamed PRIMARY_KEY to USER_ID in Harbor Log.",
     node("Harbor Log", "We renamed PRIMARY_KEY to USER_ID in Harbor Log.", "project",
          evidence="We renamed PRIMARY_KEY to USER_ID in Harbor Log")),
])
def test_technical_words_about_tokens_passwords_and_keys_are_kept(text, kept):
    cleaned, dropped = quality.clean_extraction({"nodes": [kept], "edges": []}, text)
    assert [n["name"] for n in cleaned["nodes"]] == [kept["name"]] and dropped["secret"] == 0


# -- B. real secrets in every message shape never reach the model, a file, or a stored node ---------------------

SHAPES = {
    "quote": "> My token is {S} for Trail Tracker",
    "warning": "Warning: I set the key {S} for Trail Tracker",
    "question": "Is {S} the right key for Trail Tracker?",
    "after-prompt": "$ export ANTHROPIC_API_KEY={S}\nTrail Tracker reads it at startup.",
    "indented-output": "$ cat .env\n  TOKEN={S}\nTrail Tracker reads that file.",
    "code-block": "My config:\n```\nKEY={S}\n```\nTrail Tracker deploys nightly.",
    "restated-question": "What should I call the key {S} for my Trail Tracker app?",
    "we-line": "We rotated {S} for Trail Tracker last week.",
}


def hostile(secret):
    """A model that tries every way to store the secret or talk about it."""
    def answer(prompt):
        return json.dumps({"nodes": [
            node("Trail Tracker", f"Uses {secret}.", "project", evidence="Trail Tracker"),
            node(f"Key {secret}", "A key.", "fact", evidence="Trail Tracker"),
            node("Trail Tracker Config", "Config.", "project", evidence=secret),
            node("Prompt Echo", prompt[-400:], "fact", evidence="Trail Tracker"),
            node("User's secret token", "The user has a secret token.", "fact", evidence="Trail Tracker"),
            node("User set ANTHROPIC_API_KEY", "The user set their API key.", "fact", evidence="Trail Tracker"),
        ], "edges": []})
    return answer


@pytest.mark.parametrize("secret", [FAKE_GHP, FAKE_SKA], ids=["ghp", "sk-ant"])
@pytest.mark.parametrize("shape", list(SHAPES))
def test_fake_secret_in_every_shape_never_leaks(tmp_path, isolated_home, shape, secret):
    agent = Scripted(hostile(secret))
    _, nodes = run(msgs(SHAPES[shape].replace("{S}", secret)), agent, tmp_path)
    assert all(secret not in p for p in agent.prompts)
    for root in (tmp_path, isolated_home):
        for dirpath, _, names in os.walk(root):
            for name in names:
                assert secret.encode() not in Path(dirpath, name).read_bytes(), name
    talk = re.compile(r"secret|token|api key|api_key", re.I)
    assert not [n.name for n in nodes if talk.search(f"{n.name} {n.summary}")]


# -- C. a statement before the question is a fact; the question itself is not -----------------------------------

@pytest.mark.parametrize("source, keep, drop", [
    ("Since I am joining Corvid Analytics as a data engineer next month, what should I read?",
     node("Corvid Analytics", "The user's employer from next month.", "organization",
          evidence="joining Corvid Analytics as a data engineer"),
     node("Reading List", "What the user should read.", "task", evidence="what should I read")),
    ("Since I prefer Rust over Go for CLI tools, which argument parser should I pick?",
     node("Rust Over Go", "Prefers Rust over Go for CLI tools.", "preference",
          evidence="I prefer Rust over Go for CLI tools"),
     node("Pick Argument Parser", "Choose an argument parser.", "decision",
          evidence="which argument parser should I pick")),
])
def test_premise_before_a_question_is_kept_but_the_question_is_not(source, keep, drop):
    cleaned, _ = quality.clean_extraction({"nodes": [keep, drop], "edges": []}, source)
    assert [n["name"] for n in cleaned["nodes"]] == [keep["name"]]


# -- D. tool output and logs that still leak through the pre-filter ----------------------------------------------

@pytest.mark.parametrize("noise", [
    "$ git log -n 2\n  a1b2c3 Fix typo\n\n  Signed-off-by: Release Bot",
    "$ git status\nOn branch main\nChanges not staged for commit\nmodified: src/app.py",
    "Warning: region us failed health check\nError: variable i is not defined in loop",
    "$ apt-get install pkg\n  I: Installing package dependencies\n  I [main] worker started",
    "panic: runtime error: index out of range [3] with length 3\n\ngoroutine 1 [running]:\nmain.main()\n"
    "\t/home/dev/app/main.go:12 +0x1d\nexit status 2",
    "INFO: Started server process [4821]\nDEBUG: loading config from /etc/app.yaml",
], ids=["git-log-blank-line", "git-status", "us-and-i", "apt-log-prefixes", "go-panic", "info-debug"])
def test_tool_output_and_logs_are_dropped(noise):
    assert quality.clean_message(noise) == ""


# -- E. a new shell prompt ends a stack trace ----------------------------------------------------------------------

def test_new_prompt_after_a_traceback_resets_the_trace_state():
    text = "Traceback (most recent call last):\n  File \"a.py\", line 1\n$ npm test\n  I switched Trail Tracker to Vitest"
    assert "I switched Trail Tracker to Vitest" in quality.clean_message(text)


# -- F. facts the pre-filter drops today -----------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="Known gap (decision: no graph-aware fix). Without knowing the user's "
                   "projects, 'Ledgerly' looks like 'Python' in 'Is Python good for beginners?', so a single-word "
                   "name cannot safely let an impersonal question through.")
def test_question_about_a_single_word_project_reaches_the_model():
    assert quality.clean_message("Is Postgres right for Ledgerly?") == "Is Postgres right for Ledgerly?"


def test_we_line_under_tool_output_that_states_a_fact_is_kept():
    assert "We migrated Harbor Log to pytest last week." in quality.clean_message(
        "$ pytest\n  We migrated Harbor Log to pytest last week.")


# -- G. docs/MEMORY.md lists definition-question openers; every one listed must really be dropped -----------------

def _documented_openers():
    text = re.sub(r"\s+", " ", DOCS.read_text(encoding="utf-8"))
    m = re.search(r'impersonal definition question \("([^"]+)"\)', text)
    assert m, "definition-question list not found in docs/MEMORY.md"
    return [o.strip() for o in m.group(1).split("/")]


def test_docs_definition_question_openers_match_behaviour():
    openers = _documented_openers()
    assert openers
    for opener in openers:
        q = "How does Apache Kafka work?" if opener.startswith("how does") else f"{opener.capitalize()} Apache Kafka?"
        assert quality.clean_message(q) == "", f"docs say {opener!r} questions are dropped, but {q!r} is kept"


# -- F follow-up and extra cases asked for after stage 1 ------------------------------------------------------------

def test_we_line_without_a_title_case_name_under_tool_output_is_still_dropped():
    assert "we expected 3 but got 4" not in quality.clean_message("$ npm test\n  we expected 3 but got 4\n  Tests: 1 failed")


@pytest.mark.xfail(strict=True, reason="Known gap: 'What is <Name>'s ...?' has the same shape as the general-knowledge "
                   "question 'What is Apache Kafka's license?', so no safe rule tells them apart without the graph.")
def test_definition_shaped_question_about_a_project_reaches_the_model():
    assert quality.clean_message("What is Trail Tracker's launch date?") == "What is Trail Tracker's launch date?"


def test_definition_shaped_general_question_with_possessive_is_dropped():
    assert quality.clean_message("What is Apache Kafka's license?") == ""


def test_windows_path_ending_in_backslash_does_not_drop_the_next_line():
    out = quality.clean_message("My repo lives at C:\\Users\\alex\\\nI am migrating Trail Tracker to Rust.")
    assert "I am migrating Trail Tracker to Rust." in out


# -- stage 3: continuation and speaker rules --------------------------------------------------------------------

@pytest.mark.parametrize("text", ["docker run \\\n    --rm -it web:latest", "$ docker run \\\n  -e MODE=prod web:latest"])
def test_backslash_after_a_space_still_continues_into_an_indented_line(text):
    assert quality.clean_message(text) == ""


@pytest.mark.parametrize("text, kept", [
    ("Warning: im moving Trail Tracker to Rust", "im moving Trail Tracker to Rust"),
    ("$ ls\n  im moving Trail Tracker to Go", "im moving Trail Tracker to Go"),
])
def test_informal_im_counts_as_the_user_speaking_in_both_rules(text, kept):
    assert kept in quality.clean_message(text)


# -- stage 4: secrets with no recognizable prefix ---------------------------------------------------------------

# (phrase, value that must never be stored or reach the model)
PREFIXLESS = {
    "my-password-is": ("my password is hunter2", "hunter2"),
    "password-colon": ("password: Tr0ub4dor&3", "Tr0ub4dor&3"),
    "the-token-is": ("the token is abc123def456", "abc123def456"),
    "export-api-key": ("export API_KEY=abcd1234", "abcd1234"),
    "bearer": ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIzNDU2",
               "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIzNDU2"),
    "my-api-key-is": ("my api key is 12345", "12345"),
    "client-secret": ("client_secret = xyz", "xyz"),
    "passwd-var": ("DB_PASSWD=pw4Harbor", "pw4Harbor"),
    "pat-var": ("GH_PAT=patValue77", "patValue77"),
    "private-key": ("private_key = k3yMaterial", "k3yMaterial"),
    "access-key": ("access_key: AK77zz", "AK77zz"),
}

PHRASE_SHAPES = {
    "quote": "> {P}\n> Trail Tracker deploys nightly.",
    "warning": "Warning: {P} for Trail Tracker",
    "question": "Is {P} safe for Trail Tracker?",
    "after-prompt": "$ {P}\nTrail Tracker reads it at startup.",
    "indented-output": "$ cat notes.txt\n  {P}\nTrail Tracker reads that file.",
    "code-block": "My config:\n```\n{P}\n```\nTrail Tracker deploys nightly.",
    "restated-question": "What should I do now that {P} for my Trail Tracker app?",
    "we-line": "We noted that {P} in Trail Tracker.\nTrail Tracker deploys nightly.",
}

SECRET_TALK = re.compile(r"password|passwd|secret|token|api.?key|access.?key|private.?key|bearer|credential", re.I)


def hostile_value(value):
    def answer(prompt):
        return json.dumps({"nodes": [
            node("Trail Tracker", f"Uses {value}.", "project", evidence="Trail Tracker"),
            node(f"Key {value}", "A key.", "fact", evidence="Trail Tracker"),
            node("Trail Tracker Config", "Config.", "project", evidence=value),
            node("Prompt Echo", prompt[-400:], "fact", evidence="Trail Tracker"),
            node("User's password", "The user's password is set.", "fact", evidence="Trail Tracker"),
            node("Stored Credentials", "The user stores an API key and a password.", "fact", evidence="Trail Tracker"),
        ], "edges": []})
    return answer


@pytest.mark.parametrize("secret", list(PREFIXLESS))
@pytest.mark.parametrize("shape", list(PHRASE_SHAPES))
def test_prefixless_secret_in_every_shape_never_leaks(tmp_path, isolated_home, shape, secret):
    phrase, value = PREFIXLESS[secret]
    agent = Scripted(hostile_value(value))
    _, nodes = run(msgs(PHRASE_SHAPES[shape].replace("{P}", phrase)), agent, tmp_path)
    assert all(value not in p for p in agent.prompts), "value reached the model"
    for root in (tmp_path, isolated_home):
        for dirpath, _, names in os.walk(root):
            for name in names:
                assert value.encode() not in Path(dirpath, name).read_bytes(), name
    assert not [n.name for n in nodes if SECRET_TALK.search(f"{n.name} {n.summary}")]
    assert not [n.name for n in nodes if value in f"{n.name} {n.summary}"]


@pytest.mark.parametrize("secret", list(PREFIXLESS))
def test_credential_values_are_found(secret):
    phrase, value = PREFIXLESS[secret]
    assert value in quality.credential_values(f"Trail Tracker notes. {phrase}. More text.")


@pytest.mark.parametrize("label", ["api key", "api_key", "api-key", "access key", "access_key", "access-key",
                                   "private key", "private_key", "private-key", "secret key", "secret_key",
                                   "secret-key", "client secret", "client_secret", "client-secret"])
def test_separators_are_consistent_across_key_labels(label):
    assert "v4lue99" in quality.credential_values(f"my {label} is v4lue99")
    assert "v4lue99" in quality.credential_values(f"{label} = v4lue99")
    assert "v4lue99" in quality.credential_values(f"{label}: v4lue99")


def test_withheld_values_drop_nodes_even_without_a_secret_pattern():
    data = {"nodes": [node("Trail Tracker", "Uses hunter2.", "project", evidence="Trail Tracker"),
                      node("Harbor Log", "A log viewer.", "project", evidence="Harbor Log")], "edges": []}
    cleaned, dropped = quality.clean_extraction(data, "Trail Tracker and Harbor Log", withheld={"hunter2"})
    assert [n["name"] for n in cleaned["nodes"]] == ["Harbor Log"] and dropped["secret"] == 1


@pytest.mark.parametrize("plain", [
    "the token is valid for one hour", "Trail Tracker's token limit is 4096", "We renamed PRIMARY_KEY to USER_ID",
    "PassVault is a password manager", "CREATE TABLE users (id INT PRIMARY KEY, api_key_hash TEXT)",
])
def test_plain_technical_text_has_no_credential_value(plain):
    assert quality.credential_values(plain) == set()


def test_docs_list_every_secret_variable_suffix_and_label():
    text = DOCS.read_text(encoding="utf-8")
    for suffix in quality.SECRET_VAR_SUFFIXES:
        assert f"`_{suffix}`" in text, suffix
    for label in quality.SECRET_LABELS:
        assert label in text, label


# -- stage 4b: plain-word passwords after a label ---------------------------------------------------------------

PLAIN_WORD = {
    "the-password-is": ("the password is swordfish", "swordfish"),
    "password-is": ("password is swordfish", "swordfish"),
    "passcode": ("The passcode is correcthorse", "correcthorse"),
    "token-is": ("the token is abcdefgh", "abcdefgh"),
    "secret-is": ("the secret is banana", "banana"),
    "pwd-colon": ("pwd: orange", "orange"),
    "passphrase-words": ("my passphrase is blue sky mining", "blue sky mining"),
}


@pytest.mark.parametrize("secret", list(PLAIN_WORD))
@pytest.mark.parametrize("shape", list(PHRASE_SHAPES))
def test_plain_word_password_in_every_shape_never_leaks(tmp_path, isolated_home, shape, secret):
    phrase, value = PLAIN_WORD[secret]
    agent = Scripted(hostile_value(value))
    _, nodes = run(msgs(PHRASE_SHAPES[shape].replace("{P}", phrase)), agent, tmp_path)
    assert all(value not in p for p in agent.prompts), "value reached the model"
    for word in value.split():
        assert all(f" {word}" not in p for p in agent.prompts), f"part of the value reached the model: {word}"
    # memory.db, -wal, -shm (tmp_path) and memory_queue / logs (isolated_home)
    for root in (tmp_path, isolated_home):
        for dirpath, _, names in os.walk(root):
            for name in names:
                assert value.encode() not in Path(dirpath, name).read_bytes(), name
    assert not [n.name for n in nodes if SECRET_TALK.search(f"{n.name} {n.summary}")]
    assert not [n.name for n in nodes if value in f"{n.name} {n.summary}"]


def test_a_label_with_no_value_keeps_the_fact():
    for text in ["PassVault is an offline password manager", "Trail Tracker uses JWT tokens",
                 "Muse has a token limit of 200,000"]:
        assert quality.clean_message(text) == text


def test_over_masked_policy_line_is_still_usable():
    out = quality.clean_message("the password policy is strict for Trail Tracker, which ships in March")
    assert "Trail Tracker" in out and "March" in out


# -- the stale-duplicate guard (stage 3 bug: a second _SPEAKER_START overrode the first) -------------------------

def module_level_duplicates(path):
    import ast
    seen, dupes = set(), []
    for stmt in ast.parse(Path(path).read_text(encoding="utf-8")).body:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name):
                if t.id in seen:
                    dupes.append(t.id)
                seen.add(t.id)
    return dupes


def test_duplicate_guard_catches_a_redefined_name(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('import re\n_SPEAKER_START = re.compile("a")\nX = 1\n_SPEAKER_START = re.compile("b")\n')
    assert module_level_duplicates(f) == ["_SPEAKER_START"]


def test_quality_module_defines_each_module_level_name_once():
    assert module_level_duplicates(quality.__file__) == []


def test_withheld_values_include_words_of_an_over_masked_clause_the_model_never_saw():
    text = "We noted that my password is hunter2 in Trail Tracker.\nTrail Tracker deploys nightly."
    withheld = quality.withheld_values(text)
    assert "hunter2" in withheld and "Trail" not in withheld and "Tracker" not in withheld


# -- stage 5: the clause before a question is kept; it must not open a leak path ----------------------------------

PREMISE = {
    "premise-password": ("Since my password is hunter2, which tool should I pick?", "hunter2"),
    "premise-token": ("Since the token is abcdefgh1234, how do I rotate it?", "abcdefgh1234"),
    "premise-ghp": (f"Since I use {FAKE_GHP}, what should I do?", FAKE_GHP),
}


@pytest.mark.parametrize("secret", list(PREMISE))
@pytest.mark.parametrize("shape", list(PHRASE_SHAPES))
def test_secret_in_a_kept_premise_never_leaks(tmp_path, isolated_home, shape, secret):
    phrase, value = PREMISE[secret]
    agent = Scripted(hostile_value(value))
    _, nodes = run(msgs(PHRASE_SHAPES[shape].replace("{P}", phrase)), agent, tmp_path)
    assert all(value not in p for p in agent.prompts), "value reached the model"
    for root in (tmp_path, isolated_home):
        for dirpath, _, names in os.walk(root):
            for name in names:
                assert value.encode() not in Path(dirpath, name).read_bytes(), name
    assert not [n.name for n in nodes if SECRET_TALK.search(f"{n.name} {n.summary}")]
    assert not [n.name for n in nodes if value in f"{n.name} {n.summary}"]


def test_premise_is_kept_end_to_end_and_the_question_is_not(tmp_path):
    out = {"nodes": [node("Corvid Analytics", "The user's employer from next month.", "organization",
                          evidence="joining Corvid Analytics as a data engineer"),
                     node("Reading List", "What the user should read.", "task", evidence="what should I read")],
           "edges": []}
    _, nodes = run(msgs("Since I am joining Corvid Analytics as a data engineer next month, what should I read?"),
                   Scripted(out), tmp_path)
    assert [n.name for n in nodes] == ["Corvid Analytics"]


# -- stage 5: what over-masking costs (measured, not fixed by loosening the leak protection) -----------------------

def _honest_trail_tracker():
    return {"nodes": [node("Trail Tracker", "The user's project.", "project", evidence="Trail Tracker")], "edges": []}


def test_project_named_outside_the_masked_clause_is_kept(tmp_path):
    result, nodes = run(msgs("We stored my password is hunter2 in Trail Tracker.", "Trail Tracker ships in March."),
                        Scripted(_honest_trail_tracker()), tmp_path)
    assert [n.name for n in nodes] == ["Trail Tracker"]


def test_over_masking_loss_is_counted_not_silent(tmp_path):
    # Trail Tracker appears only inside the masked clause: the model never sees it, and a node naming it
    # is dropped. The loss is real (see the xfail below); this checks it is at least counted.
    result, nodes = run(msgs("We stored my password is hunter2 in Trail Tracker."),
                        Scripted(_honest_trail_tracker()), tmp_path)
    assert nodes == [] and result.counts.get("dropped", 0) >= 1


@pytest.mark.xfail(strict=True, reason="Known gap (measured): a name that appears only inside an over-masked "
                   "credential clause is lost; keeping it would mean storing words the leak filter withheld.")
def test_project_named_only_inside_the_masked_clause_is_kept(tmp_path):
    _, nodes = run(msgs("We stored my password is hunter2 in Trail Tracker."), Scripted(_honest_trail_tracker()), tmp_path)
    assert [n.name for n in nodes] == ["Trail Tracker"]


# -- stage 5b: a hypothetical premise is not a fact ------------------------------------------------------------

HYPOTHETICAL = [
    ("If I joined Google next year, what should I learn?",
     node("Google", "The user's employer from next year.", "organization", evidence="I joined Google next year")),
    ("If I prefer Rust, which parser should I pick?",
     node("Prefers Rust", "The user prefers Rust.", "preference", evidence="I prefer Rust")),
    ("Suppose I am accepted at UCSB, what should I do first?",
     node("UCSB", "The user was accepted at UCSB.", "organization", evidence="I am accepted at UCSB")),
    ("Assuming I get the offer from Corvid Analytics, how do I negotiate?",
     node("Corvid Analytics", "The user has an offer from Corvid Analytics.", "organization",
          evidence="I get the offer from Corvid Analytics")),
    ("What if I switch to Go, would that help?",
     node("Switch To Go", "The user switched to Go.", "decision", evidence="I switch to Go")),
    ("Say I were a data engineer at Corvid, what would I study?",
     node("Data Engineer At Corvid", "The user is a data engineer at Corvid.", "fact",
          evidence="I were a data engineer at Corvid")),
    ("Imagine I prefer Vue, which library should I pick?",
     node("Prefers Vue", "The user prefers Vue.", "preference", evidence="I prefer Vue")),
    ("Hypothetically, I joined Stripe, which team fits?",
     node("Stripe", "The user joined Stripe.", "organization", evidence="I joined Stripe")),
    ("Would it help if I learned Rust, which course?",
     node("Rust", "The user learned Rust.", "skill", evidence="I learned Rust")),
]

FACTUAL = [
    ("Since I am joining Corvid Analytics as a data engineer next month, what should I read?",
     node("Corvid Analytics", "The user's employer from next month.", "organization",
          evidence="joining Corvid Analytics as a data engineer")),
    ("Because I prefer Rust over Go for CLI tools, which argument parser should I pick?",
     node("Rust Over Go", "Prefers Rust over Go for CLI tools.", "preference", evidence="I prefer Rust over Go for CLI tools")),
    ("As a data engineer at Corvid Analytics, which book should I read?",
     node("Data Engineer At Corvid Analytics", "The user is a data engineer at Corvid Analytics.", "fact",
          evidence="a data engineer at Corvid Analytics")),
    ("Now that I have joined Corvid Analytics, what should I read?",
     node("Corvid Analytics", "The user joined Corvid Analytics.", "organization", evidence="I have joined Corvid Analytics")),
]


@pytest.mark.parametrize("source, premise", HYPOTHETICAL, ids=[s[:30] for s, _ in HYPOTHETICAL])
def test_hypothetical_premise_is_not_stored(tmp_path, source, premise):
    cleaned, _ = quality.clean_extraction({"nodes": [premise], "edges": []}, source)
    assert cleaned["nodes"] == []
    _, nodes = run(msgs(source), Scripted({"nodes": [premise], "edges": []}), tmp_path)
    assert nodes == []


@pytest.mark.parametrize("source, premise", FACTUAL, ids=[s[:30] for s, _ in FACTUAL])
def test_factual_premise_is_still_stored(tmp_path, source, premise):
    cleaned, _ = quality.clean_extraction({"nodes": [premise], "edges": []}, source)
    assert [n["name"] for n in cleaned["nodes"]] == [premise["name"]]
    _, nodes = run(msgs(source), Scripted({"nodes": [premise], "edges": []}), tmp_path)
    assert [n.name for n in nodes] == [premise["name"]]


# -- stage 5c: a hypothetical question does not create a project or application -----------------------------------

HYPOTHETICAL_PROJECT = [
    ("If I build Trail Tracker, which stack should I use?", "Trail Tracker", "project"),
    ("Suppose I start Ledgerly Pro, which database should I pick?", "Ledgerly Pro", "project"),
    ("What if I rebuild Harbor Log in Rust, would that help?", "Harbor Log", "project"),
    ("Would it help if I built Trail Tracker with Rust?", "Trail Tracker", "project"),
    ("Imagine I launched Corvid Notes, which host should I use?", "Corvid Notes", "application"),
]


@pytest.mark.parametrize("source, name, type_", HYPOTHETICAL_PROJECT, ids=[s[:30] for s, _, _ in HYPOTHETICAL_PROJECT])
def test_hypothetical_question_creates_no_project_or_application(tmp_path, source, name, type_):
    n = node(name, "The user's project.", type_, evidence=name)
    cleaned, _ = quality.clean_extraction({"nodes": [n], "edges": []}, source)
    assert cleaned["nodes"] == []
    _, nodes = run(msgs(source), Scripted({"nodes": [n], "edges": []}), tmp_path)
    assert nodes == []


@pytest.mark.parametrize("source", ["Which database should Harbor Log use?", "Is Harbor Log ready for Postgres?",
                                    "I built Harbor Log in Python."])
def test_project_from_a_plain_question_or_statement_is_still_kept(tmp_path, source):
    n = node("Harbor Log", "The user's project.", "project", evidence="Harbor Log")
    cleaned, _ = quality.clean_extraction({"nodes": [n], "edges": []}, source)
    assert [x["name"] for x in cleaned["nodes"]] == ["Harbor Log"]
    _, nodes = run(msgs(source), Scripted({"nodes": [n], "edges": []}), tmp_path)
    assert [x.name for x in nodes] == ["Harbor Log"]
