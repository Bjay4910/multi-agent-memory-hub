"""Gap 3, review 5 of cffce8e: narrow the own-turn masks (weak labels), hyphenated and CamelCase values, zero-width.

Paths: "claude" and "agy" (one capture of the whole transcript), "claude-resumed" and "agy-stop-hooks" (a capture,
more turns, a second capture), "direct" (working.upsert). Fake values only. Temp homes only.
"""

import pytest

from memory.test_working_redaction import PAD_USER, Env
from memory.test_working_redaction_review import AgySession, first_turns, leaks
from memory.test_working_redaction_review4 import run, stored_texts
from room.memory import working

PATHS = ["claude", "claude-resumed", "agy", "agy-stop-hooks", "direct"]
PATHS4 = ["claude", "claude-resumed", "agy-stop-hooks", "direct"]


def run5(env, path, first, later):
    if path == "agy":
        s = AgySession(env)
        first_turns(s, first)
        s.add("user", later)
        s.add("assistant", "Okay.")
        return [s.capture()]
    return run(env, path, first, later)


# -- 1. ordinary sentences under a weak label are stored as main stores them ---------------------------------------

THIRTEEN = [
    "The key point is that we ship on Friday.", "The auth module is fine, no changes needed.",
    "Token expiry is set to one hour by default.", "Key bindings are listed in the help screen.",
    "The key is to keep it simple.", "Our secret weapon is speed.", "Auth is handled by the gateway.",
    "API key header is named X-Api-Key.", "The secret to good tests is small fixtures.",
    "The secret santa list is in the shared drive.", "Auth errors are logged.", "key takeaway: ship it",
    "Token = one word piece",
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("sentence", THIRTEEN, ids=[f"s{i:02d}" for i in range(1, 14)])
def test_1_ordinary_sentence_in_its_own_turn_is_unchanged(sentence, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run5(env, path, sentence, "Harmless follow-up message.")
    assert statuses[-1] in ("created", "updated"), statuses
    assert f"{sentence}\n{PAD_USER}" in stored_texts()


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("sentence", THIRTEEN, ids=[f"s{i:02d}" for i in range(1, 14)])
def test_1_ordinary_sentence_after_a_real_secret_is_unchanged(sentence, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, path, "the password is X9kQ2mLp7Zz9", sentence)
    assert sentence in stored_texts()
    assert leaks(env, "X9kQ2mLp7Zz9") == []


# -- 2. guards: must stay masked ------------------------------------------------------------------------------------

STRONG_PLAIN = [("the password is swordfish", "swordfish"),
                ("my passphrase is correct horse battery staple", "correct horse battery staple"),
                ("api key: velourquartz", "velourquartz"), ("pin: 4821", "4821")]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", STRONG_PLAIN, ids=[v.split()[0] for _, v in STRONG_PLAIN])
def test_2_strong_label_plain_value_stays_masked(text, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run5(env, path, text, "Harmless follow-up message.")
    assert statuses[-1] in ("created", "updated"), statuses
    assert leaks(env, value) == []


SHAPED = ["onXk92Lm4q", "Yes7Hq2mLp", "none_sk-live-abc123", "enabled9Qx2Lm7Zp", "false9Zk2Q", "TRUE4821337799",
          "qvwrАmzkp", "kАmеzоvqr", "KMаZQVWRT", "Read-Only-9Zk2", "sk-live-4fGh7JkL9mNb2VcX",
          "X9kQ2mLp7Zz9", "48213377", "p@ss-W0rd!x", "ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5",
          "AKIAIOSFODNN7EXAMPLE", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA=="]
LATIN = {"qvwrАmzkp": "qvwrAmzkp", "kАmеzоvqr": "kAmezovqr", "KMаZQVWRT": "KMaZQVWRT"}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("label", ["the password is", "token:", "key =", "auth =", "the secret is"])
@pytest.mark.parametrize("value", SHAPED)
def test_2_secret_shaped_value_stays_masked_under_any_label_and_in_a_later_turn(value, label, path, tmp_path,
                                                                                monkeypatch):
    env = Env(tmp_path, monkeypatch)
    plain = LATIN.get(value, value)
    run5(env, path, f"{label} {value}", f"Repeat {plain} here.")
    assert "Repeat [REDACTED] here." in stored_texts()
    assert leaks(env, plain) == []


@pytest.mark.parametrize("path", PATHS)
def test_2_flag_word_then_a_secret_in_the_same_value_stays_masked(path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, path, "token: on Xk92Lm4qZ", "Repeat Xk92Lm4qZ here.")
    assert leaks(env, "Xk92Lm4qZ") == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text", ["the token for CI is ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5 today",
                                  "the key is X9kQ2mLp7Zz9 for prod", "access token: swordfishes",
                                  "refresh token = velourquartz", "GITHUB_TOKEN=velourquartz",
                                  "the API key for prod is velourquartz"])
def test_2_weak_label_with_a_shaped_word_or_a_strong_compound_stays_masked(text, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, path, text, "Harmless follow-up message.")
    first = next(t for t in stored_texts() if t.endswith(PAD_USER))
    assert "[REDACTED]" in first, first


# -- A. hyphenated words and CamelCase identifiers --------------------------------------------------------------------

HYPHEN_CAMEL = [("auth = Read-Only", "Open the file in Read-Only mode."),
                ("auth = Not-Found", "HTTP 404 means Not-Found on the server."),
                ("key = Content-Type", "Set the Content-Type header to JSON."),
                ("key = X-Request-Id", "Send the X-Request-Id header on every call."),
                ("key = maxStrengths", "Tune maxStrengths in the config.")]


@pytest.mark.parametrize("path", PATHS4)
@pytest.mark.parametrize("first, later", HYPHEN_CAMEL, ids=[f.split()[-1] for f, _ in HYPHEN_CAMEL])
def test_a_hyphenated_and_camel_later_turn_unchanged(first, later, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, path, first, later)
    assert later in stored_texts(), f"{later!r} was changed"


@pytest.mark.parametrize("value, shaped", [("Read-Only", False), ("Not-Found", False), ("Content-Type", False),
                                           ("X-Request-Id", False), ("maxStrengths", False), ("Read-Only-9Zk2", True),
                                           ("Xk92-Lm4q-Zz", True), ("Xkqzvwrt", False), ("qvwrAmzkp", True),
                                           ("kAmezovqr", True)])
def test_a_secret_shaped_unit(value, shaped):
    assert working._secret_shaped(value) is shaped


# -- C. zero-width inside a secret, on every path ----------------------------------------------------------------------

@pytest.mark.parametrize("path", PATHS4)
@pytest.mark.parametrize("raw, plain", [("X9kQ​2mLp7Zz9", "X9kQ2mLp7Zz9"), ("qv​wrAmzkp", "qvwrAmzkp")])
def test_c_zero_width_secret_repeated_plain_is_masked(raw, plain, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, path, f"the password is {raw}", f"Repeat {plain} here.")
    assert "Repeat [REDACTED] here." in stored_texts()
    assert leaks(env, plain) == []
    assert raw not in env.all_bytes()


# -- by design and documented gaps -------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["direct", "agy"])
def test_strong_label_sentence_is_masked_by_design(path, tmp_path, monkeypatch):
    """Documented recall cost, worse than main on purpose: a strong label takes the rest of the clause."""
    env = Env(tmp_path, monkeypatch)
    run5(env, path, "The password is stored in the vault.", "Harmless follow-up message.")
    assert f"The password is [REDACTED].\n{PAD_USER}" in stored_texts()


@pytest.mark.xfail(strict=True, reason="documented gap (same as main): a secret of several plain words behind a "
                   "weak label (key, token, auth, secret) is not masked")
@pytest.mark.parametrize("text, value", [("the token is purple monkey dishwasher", "purple monkey dishwasher"),
                                         ("the secret is blue kiwi fish", "blue kiwi fish"),
                                         ("auth: tangerine basalt", "tangerine basalt"),
                                         ("the key is copper leaf river", "copper leaf river")])
def test_gap_weak_label_plain_word_secret(text, value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, "direct", text, "Harmless follow-up message.")
    assert leaks(env, value) == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", [("token = swordfish", "swordfish"), ("token: swordfish", "swordfish"),
                                         ("the token is swordfish", "swordfish"), ("the key is velourquartz", "velourquartz"),
                                         ("the secret for Harbor Log equals copperleaf", "copperleaf"),
                                         ("the secret-code is b757", "b757"), ("Log in using token h8u7 today.", "h8u7"),
                                         ("secret: velourquartz", "velourquartz"), ("secret = velourquartz", "velourquartz"),
                                         ("the auth token is swordfish", "swordfish")])
def test_2_weak_label_forms_that_redact_py_masks_stay_masked(text, value, path, tmp_path, monkeypatch):
    """A weak label on its own with a one-word value, or a form redact.py masks (as on main); "auth token" and
    "secret code" are strong."""
    env = Env(tmp_path, monkeypatch)
    run5(env, path, text, "Harmless follow-up message.")
    assert leaks(env, value) == []


@pytest.mark.xfail(strict=True, reason="documented gap (pre-existing, same on main and a1ea489): a flag word, then "
                   "punctuation, then a secret; the core filter's value ends at the punctuation (fix needs redact.py)")
@pytest.mark.parametrize("text, value", [("password: on, X9kQ2mLp7Zz9", "X9kQ2mLp7Zz9"),
                                         ("password: off; X9kQ2mLp7Zz9", "X9kQ2mLp7Zz9"), ("pin: on, 48213377", "48213377")])
def test_gap_flag_word_punctuation_then_secret(text, value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, "direct", text, "Harmless follow-up message.")
    assert leaks(env, value) == []


# -- Guard: the Handled forms table (175 examples) with seed 4417, every path, CLI and MCP read-back -----------------

def table_corpus(seed):
    import random

    from memory import test_working_redaction_fuzz as fz
    pairs, manual = fz._pairs()
    rng = random.Random(seed)
    corpus = []
    for row, (form, examples, _minimum) in enumerate(fz._rows(), 1):
        for example in examples:
            text, secret = manual.get(example, (None, None))
            text, secret = text or example, secret or pairs.get(example)
            for value in fz._variants(form, text, secret, rng):
                corpus.append((f"row{row}", text.replace(secret, value), value))
    return corpus


@pytest.mark.parametrize("mode", ["three-ways", "claude-resumed", "agy-stop-hooks"])
def test_guard_table_4417_never_leaks(mode, tmp_path, monkeypatch):
    """claude, agy and direct (one capture each), then the two multi-capture paths; 16-hex hashes blanked (see
    test_working_redaction_review4.test_guard_table_3307_never_leaks)."""
    from memory.test_working_redaction import words_in
    from memory.test_working_redaction_review4 import _HASH_FIELD
    corpus = table_corpus(4417)
    assert len(corpus) == 525
    found_leaks = []
    for i, (row, text, value) in enumerate(corpus):
        env = Env(tmp_path / f"c{i}", monkeypatch)
        if mode == "three-ways":
            statuses = (env.claude(text), env.agy(text), env.direct(text))
        else:
            statuses = run(env, mode, text, "Harmless follow-up message.")
        assert "refused-secret" not in statuses, (row, text)
        readable = env.outputs(["Okapi-Lantern", value, *value.split()[:2]])
        stored = _HASH_FIELD.sub('""', env.all_bytes())
        found = sorted({f"output:{w}" for w in words_in(value, readable)} | {f"file:{w}" for w in words_in(value, stored)})
        if found:
            found_leaks.append((row, text[:80], value, found))
    print(f"seed 4417 ({mode}): 525 inputs, {len(found_leaks)} leaks")
    assert not found_leaks, f"{len(found_leaks)} leaks, first: {found_leaks[:10]}"
