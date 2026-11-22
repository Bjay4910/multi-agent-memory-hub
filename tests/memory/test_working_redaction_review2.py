"""Gap 3, second review of 24cf486: the reviewer's inputs verbatim, plus 3 fresh values per case (seed 4413).

Paths: Claude Code capture, a resumed Claude session, Antigravity Stop hooks, direct working.upsert; read back
through `room working list/show/search`, MCP working_search and session_latest, and every byte under the temp
AGENT_ROOM_HOME (notes, temp files, .lock, capture/capture.log). Fake values only.
"""

import json
import random
import re
import string

import pytest

from memory.test_working_redaction import PAD_USER, PAD_USER_2, Env
from memory.test_working_redaction_review import AgySession, ClaudeSession, first_turns, leaks, prefixes_in, words_strict
from room import capture, mcp_server
from room.memory import working

RNG = random.Random(4413)


def letters(n):
    return "".join(RNG.choice("bcdfghjklmnpqrstvwxz") if i % 2 else RNG.choice("aeiou") for i in range(n))


def alnum(n, chars=string.ascii_lowercase + string.digits):
    while True:
        v = "".join(RNG.choice(chars) for _ in range(n))
        if re.search(r"\d", v) and re.search(r"[a-z]", v):
            return v


def digits(n):
    return "".join(RNG.choice("123456789") for _ in range(n))


def run_paths(env, first, later, path):
    """Store ``first`` then ``later`` through one path; returns the statuses."""
    if path == "direct":
        items = [{"key": "u1", "role": "user", "ts": "t", "text": f"{first}\n{PAD_USER}"},
                 {"key": "u2", "role": "user", "ts": "t", "text": PAD_USER_2},
                 {"key": "u3", "role": "user", "ts": "t", "text": later}]
        return (working.upsert("claude-code", "k", "okapi-lantern", items)[0],)
    session = (ClaudeSession if path.startswith("claude") else AgySession)(env)
    first_turns(session, first)
    if path == "claude":  # one capture with both turns
        session.add("user", later)
        session.add("assistant", "Okay.")
        return (session.capture(),)
    s1 = session.capture()
    session.add("user", later)
    session.add("assistant", "Okay.")
    return s1, session.capture()


PATHS = ["claude", "claude-resumed", "agy-stop-hooks", "direct"]


# -- a1, a3: bare repeats of letters-only, digits-only, multi-word and "redacted"-looking values ----------------

def a1_cases():
    cases = [("velourquartz", "the password is velourquartz for staging", "Wait, did I type velourquartz earlier?", ["velourquartz"]),
             ("pin", "the PIN is 774192", "Remember 774192", ["774192"]),
             ("seed", "seed phrase tangerine basalt pelican spruce", "First word was tangerine",
              ["tangerine basalt pelican spruce"]),
             ("same-turn", "password: zirconium (yes, zirconium is active)", "Fine.", ["zirconium"]),
             ("unredacted", "the password is unredacted7741", "I typed unredacted7741 into the shell.", ["unredacted7741"])]
    for i in range(3):
        w, d, ws = letters(12), digits(6), [letters(n) for n in (9, 6, 7, 6)]
        z, u = letters(9), f"unredacted{digits(4)}"
        cases += [(f"velourquartz-fresh{i}", f"the password is {w} for staging", f"Wait, did I type {w} earlier?", [w]),
                  (f"pin-fresh{i}", f"the PIN is {d}", f"Remember {d}", [d]),
                  (f"seed-fresh{i}", f"seed phrase {' '.join(ws)}", f"First word was {ws[0]}", [" ".join(ws)]),
                  (f"same-turn-fresh{i}", f"password: {z} (yes, {z} is active)", "Fine.", [z]),
                  (f"unredacted-fresh{i}", f"the password is {u}", f"I typed {u} into the shell.", [u])]
    return cases


# Known gap (strict xfail): "seed phrase tangerine basalt ..." has no connector after the label, so the
# extraction filter masks nothing and nothing can be withheld; the verified form is "seed phrase: ...".
SEED_GAP = pytest.mark.xfail(strict=True, reason="label without a connector is not masked (seed phrase), documented gap")
A1 = [pytest.param(*c, id=c[0], marks=[SEED_GAP] if c[0].startswith("seed") else []) for c in a1_cases()]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("name, first, later, values", A1)
def test_a1_a3_bare_repeats_are_scrubbed(name, first, later, values, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert run_paths(env, first, later, path)[-1] in ("created", "updated")
    for v in values:
        assert leaks(env, v) == [], v


# -- a2: a JSON-escaped value and its unescaped repeat ------------------------------------------------------------

A2 = [("vqx", 'vqx\\"zmk')] + [(f"fresh{i}", f'{letters(3)}\\"{letters(3)}') for i in range(3)]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("name, escaped", A2, ids=[c[0] for c in A2])
def test_a2_json_escaped_value_and_its_unescaped_repeat(name, escaped, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    plain = escaped.replace('\\"', '"')
    run_paths(env, '{"password": "' + escaped + '", "env": "prod"}', plain, path)
    stored, readable = env.all_bytes(), env.outputs(["Okapi-Lantern"])
    for blob in (stored, readable):
        assert plain not in blob and escaped not in blob and plain.replace('"', '\\"') not in blob


# -- a4: a repeat that straddles the 2,000-character turn cut ----------------------------------------------------

A4 = ["vqx7741plover"] + [alnum(13) for _ in range(3)]


@pytest.mark.parametrize("path", ["claude-resumed", "agy-stop-hooks", "claude"])
@pytest.mark.parametrize("value", A4)
def test_a4_repeat_across_the_turn_cut(value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    later = ("plan the release " * 200)[:capture.MAX_TURN_CHARS - 6] + value + " and more words after it."
    assert later.index(value) < capture.MAX_TURN_CHARS < later.index(value) + len(value)
    run_paths(env, f"the password is {value}", later, path)
    assert leaks(env, value, fragments=True) == []


# -- a5, a7: the project folder name ------------------------------------------------------------------------------

def _a5_name(value):
    """A folder name where ``value`` (after "pwd=") straddles the 40-character project cut."""
    head = ("release-notes-project-" * 3)[:40 - 4 - 5]
    return f"{head}pwd={value}"


A5 = ["vqx7741plover"] + [alnum(13) for _ in range(3)]


@pytest.mark.parametrize("kind", ["claude", "agy"])
@pytest.mark.parametrize("value", A5)
def test_a5_secret_across_the_project_cut(value, kind, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    name = _a5_name(value)
    assert name.index(value) < 40 < name.index(value) + len(value)
    session = (ClaudeSession if kind == "claude" else AgySession)(env, cwd=f"/Users/someone/Developer/{name}")
    first_turns(session, "A plain planning message.")
    assert session.worker() == 0
    [note] = working.list_notes()
    assert not prefixes_in(value, note["project"]) and not prefixes_in(value, (env.home / "capture" / "capture.log").read_text())
    assert leaks(env, value, fragments=True) == []


A7 = ["vqx7741plover"] + [alnum(13) for _ in range(3)]


@pytest.mark.parametrize("kind", ["claude", "agy"])
@pytest.mark.parametrize("value", A7)
def test_a7_secret_in_the_folder_name_repeated_bare_in_a_turn(value, kind, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    session = (ClaudeSession if kind == "claude" else AgySession)(env, cwd=f"/Users/someone/Developer/password is {value}")
    first_turns(session, f"I keep typing {value} by mistake.")
    assert session.capture() == "created"
    assert leaks(env, value) == []


# -- a6: redact_secrets-only secrets inside code fences, repeated bare (also with lookalikes and zero-width) -------

def a6_secrets():
    b64 = string.ascii_letters + string.digits
    fixed = [("sk", "sk-proj1Aa2Bb3Cc4Dd5Ee6Ff7Gg8Hh9Ii0"), ("ghp", "gh" "p_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6"),
             ("akia", "AKIA" + "IOSFODNN7EXAMPLE"),
             ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0aSJ9.c2lnbmF0dXJlYWJjMTIzNDU2"),
             ("pem", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7aBcDeFgHiJkLmNoPqRsTuVwXyZ")]
    fresh = []
    for i in range(3):
        fresh += [(f"sk-fresh{i}", "sk-proj" + "a" + alnum(31, b64)), (f"ghp-fresh{i}", "gh" "p_" + "a" + alnum(35, b64)),
                  (f"akia-fresh{i}", "AKIA" + "A" + "".join(RNG.choice(string.ascii_uppercase + "234567") for _ in range(15))),
                  (f"jwt-fresh{i}", "eyJ" + "a" + alnum(20, b64) + ".eyJ" + alnum(16, b64) + "." + alnum(30, b64)),
                  (f"pem-fresh{i}", "MIIE" + "a" + "".join(RNG.choice(b64) for _ in range(59)))]
    return fixed + fresh


def _fenced(kind, value, fence):
    if kind.startswith("pem"):
        body = f"-----BEGIN PRIVATE KEY-----\n{value}\n{value[::-1]}\n-----END PRIVATE KEY-----"
    else:
        body = f"export SERVICE_CRED={value}" if not kind.startswith("jwt") else f"curl -H 'X: {value}' https://api.example.com"
    return f"My config:\n{fence}\n{body}\n{fence}"


def _variants(value):
    """The bare repeat as typed, with a Cyrillic а for the first Latin a, and with a zero-width space inside."""
    i = value.index("a") if "a" in value else value.lower().index("a")
    cyr = value[:i] + "а" + value[i + 1:] if value[i] == "a" else value[:i] + "А" + value[i + 1:]
    zw = value[:6] + "​" + value[6:]
    return [("plain", value), ("cyrillic", cyr), ("zero-width", zw)]


A6 = [(k, v, fence, vname, rep) for k, v in a6_secrets() for fence in ("```", "~~~") for vname, rep in _variants(v)]


@pytest.mark.parametrize("kind, value, fence, variant, repeat", A6,
                         ids=[f"{k}-{'tick' if f == '```' else 'tilde'}-{n}" for k, _, f, n, _ in A6])
def test_a6_fenced_redact_only_secret_and_its_bare_repeat(kind, value, fence, variant, repeat, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert run_paths(env, _fenced(kind, value, fence), f"Earlier I pasted {repeat} there.", "agy-stop-hooks")[-1] == "updated"
    stored, readable = env.all_bytes(), env.outputs(["Okapi-Lantern"])
    for blob in (stored, readable):
        assert repeat not in blob
        assert words_strict(value, blob) == []


# -- c1: a value after a code block, then a bare repeat -----------------------------------------------------------

C1 = ["vqx7741starling"] + [alnum(15) for _ in range(3)]


@pytest.mark.xfail(strict=True, reason="a value after a code block below its label is not masked, documented gap")
@pytest.mark.parametrize("value", C1)
def test_c1_value_after_a_code_block_then_bare(value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run_paths(env, f"password:\n```\nhello\n```\n  {value}", f"So {value} it is.", "agy-stop-hooks")
    assert leaks(env, value) == []


# -- c3: an earlier reply that a later reply in the same turn replaces --------------------------------------------

C3 = ["vqx7741heron"] + [alnum(12) for _ in range(3)]


@pytest.mark.parametrize("value", C3)
def test_c3_overwritten_reply_secret_then_bare_repeat(value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    s = AgySession(env)
    s.add("user", f"Help me plan the Okapi-Lantern release.\n{PAD_USER}")
    s.add("assistant", f"Draft: the password is {value}")  # replaced by the next reply in the same turn
    s.add("assistant", "Final: here is the plan.")
    s.add("user", PAD_USER_2)
    s.add("assistant", "Plan noted.")
    assert s.capture() == "created"
    s.add("user", f"Did you say {value} before?")
    s.add("assistant", "Okay.")
    assert s.capture() == "updated"
    assert leaks(env, value) == []


# -- c5: things the scrub and the MCP output must leave alone -----------------------------------------------------

def test_c5_mcp_output_keeps_parameters_after_a_masked_value(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert env.direct("Search uses https://api.example.com/items?api_key=lilac7x9&page=2&sort=asc for lists.") == "created"
    latest = mcp_server.call_tool("session_latest", {"agent": "claude-code"})[0]
    assert "api_key=[REDACTED]&page=2&sort=asc" in latest and "lilac7x9" not in latest
    search = mcp_server.call_tool("working_search", {"query": "items page sort"})[0]  # excerpts around the match
    assert "[REDACTED]&page=2" in search and "lilac7x9" not in search


def test_c5_scrub_keeps_words_that_only_contain_the_value(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert working._scrub("the airplane left early", {"plan"}) == "the airplane left early"
    run_paths(env, "password: plan", "The airplane left early.", "direct")
    assert "airplane" in env.all_bytes()


def test_c5_scrub_never_corrupts_an_existing_marker(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert working._scrub("foo[REDACTED] is odd", {"foo["}) == "foo[REDACTED] is odd"
    run_paths(env, "password: foo[", "foo[REDACTED] is odd", "direct")
    stored = env.all_bytes()
    assert "[[REDACTED]" not in stored and "REDACTED]REDACTED" not in stored


def test_c5_a_turn_ending_in_a_label_is_not_refused(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    items = [{"key": "u1", "role": "user", "ts": "t", "text": f"{PAD_USER}\nMy staging password:"},
             {"key": "u2", "role": "user", "ts": "t", "text": PAD_USER_2}]
    assert working.upsert("claude-code", "k", "okapi-lantern", items)[0] == "created"
    for s in (ClaudeSession(env), AgySession(env)):
        s.add("user", f"{PAD_USER}\nMy staging password:")
        s.add("assistant", "Noted.")
        s.add("user", PAD_USER_2)
        assert s.capture() == "created"


# -- c4: a note written before the change, holding a short secret ------------------------------------------------

def _legacy_note(text):
    """A note as the old code stored it (redact_secrets only), for session "legacy"."""
    now = working.utcnow()
    note = {"version": working.FORMAT_VERSION, "id": "claude-code-20261101T000000Z-abc123", "source": "claude-code",
            "project": "okapi-lantern", "source_key": working._hash("claude-code:legacy"),
            "captured_at": working.iso(now), "updated_at": working.iso(now),
            "expires_at": working.iso(now + working.timedelta(days=10)), "promoted_at": None, "dropped_turns": 0,
            "turn_keys": sorted(working._hash(k) for k in ("u1", "u2")),
            "turns": [{"role": "user", "ts": "t", "text": f"{text}\n{PAD_USER}"},
                      {"role": "user", "ts": "t", "text": PAD_USER_2}]}
    directory = working.ensure_dir()
    (directory / f"{note['id']}.json").write_text(json.dumps(note))
    return note


C4 = ["q7"] + ["".join(RNG.choice("bcdfghjkmnpqrstvwxz") + RNG.choice("23456789")) for _ in range(3)]


@pytest.mark.parametrize("value", C4)
def test_c4_a_legacy_note_with_a_short_secret_is_masked_when_its_session_goes_on(value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    _legacy_note(f"the PIN is {value}")
    items = [{"key": "u3", "role": "user", "ts": "t", "text": "One more planning message for the release."}]
    status, _ = working.upsert("claude-code", "legacy", "okapi-lantern", items)
    stored = env.all_bytes()
    assert not re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", stored), (status, value)
