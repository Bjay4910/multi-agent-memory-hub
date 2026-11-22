"""Gap 3, fourth review of a1ea489: Cyrillic lookalikes, Antigravity replaced replies, the cut through a marker,
over-masking of ordinary later text, and a literal [REDACTED] inside a secret (a documented gap).

Each case runs on the paths it names: "claude" (one capture of the whole transcript), "claude-resumed" (a capture,
then more turns, then a second capture of the same session), "agy-stop-hooks" (a Stop hook, more turns, a second
Stop hook of the same conversation) and "direct" (working.upsert). Fake values only. Temp homes only.
"""

import re

import pytest

from memory.test_working_redaction import PAD_USER, PAD_USER_2, Env
from memory.test_working_redaction_review import AgySession, ClaudeSession, first_turns, leaks
from room import capture
from room.memory import quality, working

PATHS = ["claude", "claude-resumed", "agy-stop-hooks", "direct"]


def run(env, path, first, later):
    """``first`` in the first user turn (with padding so the note is kept), a second user turn, then ``later`` in
    the last user turn. Returns the capture statuses."""
    if path == "direct":
        items = [{"key": "u1", "role": "user", "ts": "t", "text": f"{first}\n{PAD_USER}"},
                 {"key": "a1", "role": "assistant", "ts": "t", "text": "Noted, thanks."},
                 {"key": "u2", "role": "user", "ts": "t", "text": PAD_USER_2},
                 {"key": "u3", "role": "user", "ts": "t", "text": later}]
        return [working.upsert("claude-code", "k", "okapi-lantern", items)[0]]
    s = (ClaudeSession if path.startswith("claude") else AgySession)(env)
    first_turns(s, first)
    statuses = []
    if path != "claude":
        statuses.append(s.capture())
    s.add("user", later)
    s.add("assistant", "Okay.")
    statuses.append(s.capture())
    return statuses


def stored_texts():
    return [t["text"] for n in working.list_notes() for t in n["turns"]]


# -- 1. Cyrillic lookalikes: _secret_shaped judges the normalized value --------------------------------------------

CYRILLIC = [("qvwrАmzkp", "qvwrAmzkp"), ("kАmеzоvqr", "kAmezovqr"), ("KMаZQVWRT", "KMaZQVWRT")]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("raw, latin", CYRILLIC, ids=[c[1] for c in CYRILLIC])
def test_1_cyrillic_lookalike_secret_is_scrubbed_from_a_later_turn(raw, latin, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run(env, path, f"the password is {raw}", f"Repeat {latin} here.")
    assert statuses[-1] in ("created", "updated"), statuses
    assert "Repeat [REDACTED] here." in stored_texts()
    assert leaks(env, latin) == []
    assert raw not in env.all_bytes()


# -- 2. Antigravity: a stored reply replaced later in the same turn is rebuilt from its raw text ---------------------

def test_2_agy_replaced_reply_is_rebuilt_before_the_cut(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    secret = "LMBXcegfpBL8"
    filler = ("the release summary covers each step and owner " * 60)[:1995]
    long_reply = filler + f" {secret} tail words."
    assert long_reply.index(secret) < capture.MAX_TURN_CHARS < long_reply.index(secret) + len(secret)
    s = AgySession(env)
    first_turns(s, "Summarise the release plan for the team.")
    s.add("user", "Write the long summary now.")
    s.add("assistant", long_reply)
    assert s.capture() == "created"
    s.add("assistant", f"Sorry, the password is {secret}.")  # same turn: replaces the long reply
    assert s.capture() == "updated"
    texts = stored_texts()
    assert any(t.startswith(filler[:100]) for t in texts), "the replaced reply stays stored"
    assert "Sorry, the password is [REDACTED]." in texts
    assert leaks(env, secret, fragments=True) == []


# -- 3. The 2,000-character cut never slices a [REDACTED] marker ----------------------------------------------------

CUT_MARKER = ["a" * 1984 + " token: default done", "a" * 1981 + " password: false done",
              "a" * 1981 + " password: X9kQ2mLp7Zz9 done"]
HALF_MARKER = re.compile(r"\[REDACTE(?!D\])")


@pytest.mark.parametrize("path", ["claude-resumed", "agy-stop-hooks"])
@pytest.mark.parametrize("first", CUT_MARKER, ids=["token-default", "password-false", "password-secret"])
def test_3_cut_never_slices_a_marker_and_a_later_capture_succeeds(first, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run(env, path, first, "Harmless follow-up message.")
    assert statuses == ["created", "updated"], statuses
    assert "Harmless follow-up message." in stored_texts()
    assert not HALF_MARKER.search(env.all_bytes())
    assert not HALF_MARKER.search(env.outputs(["Okapi-Lantern", "Harmless"]))
    assert leaks(env, "X9kQ2mLp7Zz9", fragments=True) == []


# -- 4. Over-masking: ordinary later text is stored unchanged --------------------------------------------------------

PROBES = [
    ("the secret is don't push to main", "Remember: don't push to main today."),
    ("the password is can't be empty", "Username can't be empty in the form."),
    ("the key is doesn't work", "That flag doesn't work on macOS."),
    ("auth = not-found", "HTTP 404 means not-found on the server."),
    ("token = non-empty", "Check that the list is non-empty first."),
    ("auth = not-enabled", "Feature is not-enabled in staging."),
    ("auth = True/False", "The toggle returns True/False."),
    ("auth = isEnabled", "Check isEnabled before rendering."),
    ("key = hasAccess", "The flag hasAccess controls editing."),
    ("the key is port 8080", "The server listens on port 8080."),
    ("key = localhost:8080", "Open http://localhost:8080 in Chrome."),
    ("key = 127.0.0.1", "Bind Postgres strictly to 127.0.0.1."),
    ("token = 20261122", "Build tag 20261122 shipped today."),
    ("key = 2026-11-22", "Release date is 2026-11-22."),
    ("key = 2.4.1-beta", "Upgraded to version 2.4.1-beta today."),
    ("key = KeyError", "Catch KeyError and fall back to default."),
    ("key = ValueError", "Raise ValueError when input is empty."),
    ("key = defaultValue", "Pass defaultValue into the reducer."),
    ("Use sorted(xs, key=str.lower) here.", "Call str.lower on each header."),
    ("Use sorted(xs, key=lambda x: x[1]) here.", "Write lambda x: x[1] for tuples."),
    ("auth = read-only", "Open memory.db in read-only mode."),
    ("Check https://example.com?key=sort-order&page=2", "The sort-order column is ascending."),
]
SQL = ("We also renamed PRIMARY_KEY to USER_ID in the legacy import table and kept foreign key constraints enabled "
       "via PRAGMA foreign_keys = ON at connection setup.")
ALL_PROBES_FIRST = "\n".join(first for first, _ in PROBES)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("first, later", PROBES, ids=[f"p{i:02d}" for i in range(1, len(PROBES) + 1)])
def test_4_probe_later_turn_is_stored_unchanged(first, later, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run(env, path, first, later)
    assert statuses[-1] in ("created", "updated"), statuses
    assert later in stored_texts(), f"{later!r} was changed"


@pytest.mark.parametrize("later", [later for _, later in PROBES] + [SQL], ids=[f"p{i:02d}" for i in range(1, 24)])
def test_4_all_probes_in_one_turn_then_each_ordinary_sentence(later, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, "direct", ALL_PROBES_FIRST, later)
    assert later in stored_texts(), f"{later!r} was changed"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("where", ["own-turn", "after-secret"])
def test_4_sql_sentence_is_stored_unchanged(where, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    if where == "own-turn":
        statuses = run(env, path, "Notes on the schema migration.", SQL)
    else:
        statuses = run(env, path, "the password is X9kQ2mLp7Zz9", SQL)
    assert statuses[-1] in ("created", "updated"), statuses
    assert SQL in stored_texts()


# The guard: real secret-shaped values are still scrubbed from later turns.
STILL_SHAPED = ["X9kQ2mLp7Zz9", "bluebell42x", "ochre-falcon-58", "p@ss-W0rd!x", "48213377",
                "sk-proj-4fGh7JkL9mNb2VcX", "ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5",
                "AKIAIOSFODNN7EXAMPLE", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==", "KMaZQVWRT", "qvwrAmzkp"]


@pytest.mark.parametrize("path", ["direct", "agy-stop-hooks"])
@pytest.mark.parametrize("secret", STILL_SHAPED)
def test_4_guard_real_secret_shapes_are_still_scrubbed_across_turns(secret, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, path, f"the password is {secret}", f"Repeat {secret} here.")
    assert "Repeat [REDACTED] here." in stored_texts()
    assert leaks(env, secret) == []


@pytest.mark.parametrize("value, shaped", [
    ("qvwrАmzkp", True), ("kАmеzоvqr", True), ("KMаZQVWRT", True),
    ("qv​wrAmzkp", True), ("X9kQ2mLp7Zz9", True), ("sk-proj-4fGh7JkL9mNb2VcX", True), ("p@ss-W0rd!x", True),
    ("48213377", True),
    ("don't push to main", False), ("shouldn't", False), ("not-found", False), ("non-empty", False),
    ("read-only", False), ("sort-order", False), ("True/False", False), ("isEnabled", False), ("hasAccess", False),
    ("KeyError", False), ("ValueError", False), ("defaultValue", False), ("localhost:8080", False),
    ("127.0.0.1", False), ("127.0.0.1:5432", False), ("http://localhost:8080", False), ("20261122", False),
    ("2026-11-22", False), ("2.4.1-beta", False), ("str.lower", False), ("lambda x: x[1]", False),
    ("os.path.join", False),
])
def test_4_secret_shaped_unit(value, shaped):
    assert working._secret_shaped(value) is shaped


# -- 5. Documented gap: a literal [REDACTED] inside a secret ---------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="documented gap: a literal [REDACTED] typed inside a secret splits it; the "
                   "piece after the marker is not labelled, so it is stored (review 4, item 5)")
def test_5_literal_marker_inside_a_secret(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, "direct", "password: x9d8Jt12[REDACTED]rPuFQf34", "Harmless follow-up message.")
    assert leaks(env, "x9d8Jt12") == [] and leaks(env, "rPuFQf34") == []


def test_quality_normalized_is_what_the_scrub_uses():
    """Sanity: the fold this file relies on (quality's lookalike table) reads the reviewer's values as Latin."""
    for raw, latin in CYRILLIC:
        assert quality._normalized(raw)[0] == latin


def test_refusal_rule_still_uses_the_stronger_check(tmp_path, monkeypatch):
    """Replaces the patch point of test_working_redaction.py::test_the_refusal_rule_uses_the_stronger_check: if the
    working layer's mask ever misses a credential the extraction filter still sees, nothing is written."""
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(working, "_mask_credentials", lambda text: text)  # simulate the masking missing it
    assert env.direct("the password is swordfish") == "refused-secret"
    assert working.list_notes() == []
    assert "swordfish" not in env.all_bytes()


# -- Guard: the docs' Handled forms table (50 forms, 175 examples) with seed 3307, every path -------------------------

_HASH_FIELD = re.compile(r'"[0-9a-f]{16}"')


def table_corpus_3307():
    import random

    from memory import test_working_redaction_fuzz as fz
    pairs, manual = fz._pairs()
    rng = random.Random(3307)
    corpus = []
    for row, (form, examples, _minimum) in enumerate(fz._rows(), 1):
        for example in examples:
            text, secret = manual.get(example, (None, None))
            text, secret = text or example, secret or pairs.get(example)
            for value in fz._variants(form, text, secret, rng):
                corpus.append((f"row{row}", text.replace(secret, value), value))
    return corpus


def test_guard_table_3307_size():
    from memory import test_working_redaction_fuzz as fz
    rows = fz._rows()
    assert (len(rows), sum(len(e) for _, e, _ in rows), len(table_corpus_3307())) == (50, 175, 525)


@pytest.mark.parametrize("mode", ["three-ways", "claude-resumed", "agy-stop-hooks"])
def test_guard_table_3307_never_leaks(mode, tmp_path, monkeypatch):
    """claude, agy and direct (one capture each, via leaks_for), then the two multi-capture paths."""
    from memory.test_working_redaction import words_in
    found_leaks = []
    for i, (row, text, value) in enumerate(table_corpus_3307()):
        env = Env(tmp_path / f"c{i}", monkeypatch)
        if mode == "three-ways":
            statuses = (env.claude(text), env.agy(text), env.direct(text))
        else:
            statuses = run(env, mode, text, "Harmless follow-up message.")
        assert "refused-secret" not in statuses, (row, text)
        readable = env.outputs(["Okapi-Lantern", value, *value.split()[:2]])
        # turn keys and session keys are stored as 16-hex hashes of the transcript's own ids, never of the text: a
        # short hex value ("b833") can sit inside one by chance (sha256("step:2") starts "bc45a83b833..."), so they
        # are blanked before the check
        stored = _HASH_FIELD.sub('""', env.all_bytes())
        found = sorted({f"output:{w}" for w in words_in(value, readable)} | {f"file:{w}" for w in words_in(value, stored)})
        if found:
            found_leaks.append((row, text[:80], value, found))
    print(f"seed 3307 ({mode}): 525 inputs, {len(found_leaks)} leaks")
    assert not found_leaks, f"{len(found_leaks)} leaks, first: {found_leaks[:10]}"
