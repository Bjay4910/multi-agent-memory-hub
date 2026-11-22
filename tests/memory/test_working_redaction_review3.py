"""Gap 3, third review of 2918756: secret-shaped cross-turn scrub only, underscore boundaries, retroactive cut.

A. leaks that must be fixed; B. ordinary text must never be damaged; C. documented gaps (strict xfails).
The reviewer's inputs verbatim, plus fresh values from seed 6021. Fake values only.
"""

import json
import random
import re
import string

import pytest

from memory.test_working_redaction import PAD_USER, PAD_USER_2, Env
from memory.test_working_redaction_review import AgySession, ClaudeSession, first_turns, leaks, prefixes_in
from room import capture
from room.memory import working

RNG = random.Random(6021)


def shaped(n=11):
    """A secret-shaped value: letters and digits, mixed case, n characters."""
    while True:
        v = "".join(RNG.choice(string.ascii_letters + string.digits) for _ in range(n))
        if re.search(r"\d", v) and re.search(r"[a-z]", v) and re.search(r"[A-Z]", v):
            return v


def b64(n=24):
    return shaped(n) + "="


SECRETS = ["X9kQ2mLp7Zz"] + [shaped() for _ in range(3)]


def direct(env, first, later):
    items = [{"key": "u1", "role": "user", "ts": "t", "text": f"{first}\n{PAD_USER}"},
             {"key": "u2", "role": "user", "ts": "t", "text": PAD_USER_2},
             {"key": "u3", "role": "user", "ts": "t", "text": later}]
    return working.upsert("claude-code", "k", "okapi-lantern", items)[0]


def two_captures(env, cls, first, later, **kw):
    s = cls(env, **kw)
    first_turns(s, first)
    a = s.capture()
    s.add("user", later)
    s.add("assistant", "Okay.")
    return a, s.capture()


# -- A. leaks that must be fixed ---------------------------------------------------------------------------------

UNDERSCORE = [("tok", "tok_{s}_end"), ("prod", "deploy prod_{s}_v2 tonight"), ("env-b64", "set env_{b}= in CI")]


@pytest.mark.parametrize("path", ["direct", "claude", "agy"])
@pytest.mark.parametrize("form", UNDERSCORE, ids=[f[0] for f in UNDERSCORE])
@pytest.mark.parametrize("secret", SECRETS)
def test_a_underscore_glued_repeats_are_scrubbed(secret, form, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    value = secret + "=" if form[0] == "env-b64" else secret
    later = form[1].replace("{s}", secret).replace("{b}", secret)
    first = f"the password is {value}"
    if path == "direct":
        direct(env, first, later)
    else:
        two_captures(env, ClaudeSession if path == "claude" else AgySession, first, later)
    assert leaks(env, secret) == []


@pytest.mark.parametrize("kind", ["claude", "agy"])
@pytest.mark.parametrize("secret", SECRETS)
def test_a_underscore_folder_name_in_the_note_and_capture_log(secret, kind, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    s = (ClaudeSession if kind == "claude" else AgySession)(env, cwd=f"/Users/someone/Developer/svc_{secret}")
    first_turns(s, f"the password is {secret}")
    assert s.worker() == 0
    [note] = working.list_notes()
    assert secret not in note["project"]
    assert secret not in (env.home / "capture" / "capture.log").read_text()
    assert leaks(env, secret) == []


@pytest.mark.parametrize("kind", ["claude-resumed", "agy-stop-hooks"])
@pytest.mark.parametrize("secret", SECRETS)
def test_a_retroactive_cut_a_bare_secret_across_the_cut_then_labelled_later(secret, kind, tmp_path, monkeypatch):
    """Capture 1 stores a bare secret that straddles the 2,000-character cut; capture 2 labels it. The earlier turn is
    rebuilt from its raw text with the new values, so not even the part before the cut stays."""
    env = Env(tmp_path, monkeypatch)
    long_turn = ("plan the release " * 200)[:capture.MAX_TURN_CHARS - 5] + secret + " and more words."
    assert long_turn.index(secret) < capture.MAX_TURN_CHARS < long_turn.index(secret) + len(secret)
    two_captures(env, ClaudeSession if kind.startswith("claude") else AgySession, long_turn,
                 f"By the way, the password is {secret}")
    assert leaks(env, secret, fragments=True) == []


@pytest.mark.parametrize("pattern", ["xx@{s}yy", "xx{s}@yy"])
@pytest.mark.parametrize("secret", SECRETS)
def test_a_repeat_with_one_non_alphanumeric_end(secret, pattern, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, f"the password is {secret}", "logged as " + pattern.replace("{s}", secret))
    assert leaks(env, secret) == []


# -- B. ordinary text must never be damaged ----------------------------------------------------------------------

REVIEWER_OVERMASK = [
    ("auth = False", "retry is False"),
    ("the secret is never run schema drops", "We never run schema drops on a Friday."),
    ("pin: 500", "Postgres cannot accept 500 concurrent writes."),
    ("foreign key constraints = on.", "keep alerts on"),
    ("Use sorted(xs, key=len) here.", "check len(rows) > 0 first"),
    ("key = None", "Return None if the list is empty."),
    ("the password is 8080", "port 8080 vs 9090"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7aBcDeFgHiJkLmNoPqRsTuVwXyZ\n-----END PRIVATE KEY-----",
     "keep the repository private"),
    ("password: !=", "if a!=b then stop"),
]

ORDINARY_LATER = [
    "retry is False", "debug = False in production", "set verbose to True", "return None when empty",
    "the server listens on port 8080", "use port 9090 for metrics", "postgres runs on 5432", "redis on 6379",
    "if a != b: raise ValueError", "assert len(rows) > 0", "for i in range(10): print(i)", "top = sorted(xs)[:3]",
    "keep alerts on", "never run schema drops", "do not push to main", "we cannot accept 500 writes",
    "the repository stays private", "we rotate private keys quarterly", "the login module works fine",
    "the token count is 4096", "max_tokens = 1024", "timeout = 30", "x = y + 1", "user_id is an integer",
    "foreign key constraints keep rows consistent", "SELECT id FROM users WHERE active", "git rebase -i HEAD~3",
    "npm install --save-dev vitest", "export PATH=$HOME/bin:$PATH", "chmod 600 notes.json",
    "the build failed on Tuesday", "rename PRIMARY_KEY to USER_ID", "the cache hit rate is 98%",
    "error code 500 means a server error", "it returned 404 not found", "version 2.4.1 ships tomorrow",
    "the meeting is at 10:30", "split the file into 3 parts", "None of the tests failed", "True or False?",
]

WEAK_FIRST = ("auth = False; pin: 500; pw = 8080; key = None; password: !=; the secret is never run schema drops; "
              "foreign key constraints = on; sorted(xs, key=len)")


def _stored_later(env, later):
    texts = [t["text"] for n in working.list_notes() for t in n["turns"]]
    return [t for t in texts if later in t]


@pytest.mark.parametrize("first, later", REVIEWER_OVERMASK, ids=[f"r{i}" for i in range(len(REVIEWER_OVERMASK))])
@pytest.mark.parametrize("path", ["direct", "claude", "agy"])
def test_b_reviewer_cases_leave_the_later_turn_unchanged(first, later, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    if path == "direct":
        direct(env, first, later)
    else:
        two_captures(env, ClaudeSession if path == "claude" else AgySession, first, later)
    assert _stored_later(env, later), f"{later!r} was changed"


@pytest.mark.parametrize("later", ORDINARY_LATER)
@pytest.mark.parametrize("path", ["direct", "agy"])
def test_b_ordinary_sentences_after_weak_secrets_are_unchanged(later, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    if path == "direct":
        direct(env, WEAK_FIRST, later)
    else:
        two_captures(env, AgySession, WEAK_FIRST, later)
    assert _stored_later(env, later), f"{later!r} was changed"


# -- C. documented gaps (strict xfail) ---------------------------------------------------------------------------

def gap(reason):
    return pytest.mark.xfail(strict=True, reason=f"documented gap: {reason}")


@gap("a plain-word or short secret repeated bare in a later turn is not scrubbed (not secret-shaped)")
@pytest.mark.parametrize("value", ["swordfish", "774192", "zirconium"])
def test_c_plain_word_or_short_secret_repeated_bare(value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, f"the password is {value}", f"I typed {value} again.")
    assert leaks(env, value) == []


@gap("4-letter words inside a free-text clause are not scrubbed from other turns")
def test_c_inner_four_letter_pieces(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, "the password is blue kiwi fish", "the kiwi was fresh")
    assert "kiwi was fresh" not in env.all_bytes()


@gap("a 1-character masked value is not withheld")
def test_c_one_character_region(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, "pw = Q", "it was Q again")
    assert "it was Q again" not in env.all_bytes()


@gap("a secret glued inside a longer letters-and-digits token is not scrubbed")
@pytest.mark.parametrize("secret", SECRETS[:2])
def test_c_secret_inside_a_longer_alphanumeric_token(secret, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, f"the password is {secret}", f"build abc{secret}def done")
    assert leaks(env, secret) == []


@gap("URL-decoded repeats are not scrubbed")
def test_c_url_decoded_repeat(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, "https://x.example.com/?api_key=lil%40c8314Q", "typed lil@c8314Q")
    assert "lil@c8314Q" not in env.all_bytes()


@gap("JSON \\u escapes are not decoded")
def test_c_json_u_escape_repeat(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, '{"password": "\\u006cilac8314Q"}', "it was lilac8314Q")
    assert "lilac8314Q" not in env.all_bytes()


@gap("a value split across two turns is not masked")
def test_c_split_across_turns(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    direct(env, "the password is crimX9", "kQ2mLp7 was the rest")
    assert "kQ2mLp7" not in env.all_bytes()


@gap("a legacy note captured again with no new turn is not rewritten")
def test_c_legacy_note_recaptured_without_a_new_turn(tmp_path, monkeypatch):
    from memory.test_working_redaction_review2 import _legacy_note

    env = Env(tmp_path, monkeypatch)
    _legacy_note("the password is swordfish42")
    working.upsert("claude-code", "legacy", "okapi-lantern",
                   [{"key": "u1", "role": "user", "ts": "t", "text": "x"}, {"key": "u2", "role": "user", "ts": "t", "text": "y"}])
    assert "swordfish42" not in env.all_bytes()
