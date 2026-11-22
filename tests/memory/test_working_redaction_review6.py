"""Gap 3, review 6 of da01302: the label that opened the match, flag clauses with a secret, snake_case names.

Paths: "claude" (one capture), "claude-resumed" and "agy-stop-hooks" (a capture, more turns, a second capture),
"direct" (working.upsert). Fake values only. Temp homes only.
"""

import pytest

from memory.test_working_redaction import PAD_USER, Env
from memory.test_working_redaction_review import leaks
from memory.test_working_redaction_review4 import run, stored_texts
from memory.test_working_redaction_review5 import THIRTEEN, table_corpus
from room.memory import working

PATHS = ["claude", "claude-resumed", "agy-stop-hooks", "direct"]


def first_turn():
    return next(t for t in stored_texts() if t.endswith(PAD_USER))


# -- 1. a strong label with a weak word in its filler ---------------------------------------------------------------

STRONG_FILLER = [
    ("The password for auth module is lamutonaxat", "lamutonaxat"),
    ("The password for auth is lamutonaxat vilivibuz difahiso", "lamutonaxat vilivibuz difahiso"),
    ("The passphrase for my token is lamutonaxat vilivibuz difahiso", "lamutonaxat vilivibuz difahiso"),
    ("The password for the secret vault is lamutonaxat", "lamutonaxat"),
    ("The api key for my key server is lamutonaxat", "lamutonaxat"),
    ("The api key for auth service is lamutonaxat", "lamutonaxat"),
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", STRONG_FILLER, ids=[f"f{i}" for i in range(1, 7)])
def test_1_strong_label_with_a_weak_word_in_its_filler_is_masked(text, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run(env, path, text, "Harmless follow-up message.")
    assert statuses[-1] in ("created", "updated"), statuses
    assert "[REDACTED]" in first_turn()
    assert leaks(env, value) == []


# -- 2. a flag word, then a short alphanumeric secret ---------------------------------------------------------------

@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text", ["the password is on SzFDVb2", "my passphrase is no SzFDVb2"])
def test_2_flag_word_then_a_secret_is_masked(text, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, path, text, "Harmless follow-up message.")
    assert "[REDACTED]" in first_turn()
    assert leaks(env, "SzFDVb2") == []


@pytest.mark.parametrize("text", ["foreign_keys = ON at connection setup.", "auth = False"])
def test_2_flag_clause_of_plain_words_is_still_kept(text):
    assert working.redact_for_note(text) == text


@pytest.mark.xfail(strict=True, reason="documented gap: a flag word followed by plain words is a setting, not masked")
@pytest.mark.parametrize("text, value", [("password: false alarm", "alarm"), ("password: true story", "story")])
def test_2_gap_flag_word_then_plain_words(text, value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, "direct", text, "Harmless follow-up message.")
    assert leaks(env, value) == []


# -- 3. snake_case names: only credential names are labels ------------------------------------------------------------

SNAKE_ORDINARY = ["The foreign_key is checked on every insert.", "The primary_key is an auto-incrementing integer.",
                  "The sort_key is the second element of the tuple.", "The cache_key is built from the request path.",
                  "The source_key is stored only as a hash."]
SNAKE_CREDENTIAL = [("The api_key is lamutonaxat", "lamutonaxat"), ("GITHUB_TOKEN = X9kQ2mLp7Zz9", "X9kQ2mLp7Zz9"),
                    ("the client_secret is lamutonaxat", "lamutonaxat"), ("the access_token is X9kQ2mLp7Zz9", "X9kQ2mLp7Zz9")]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("sentence", SNAKE_ORDINARY, ids=[s.split()[1] for s in SNAKE_ORDINARY])
def test_3_non_credential_snake_case_name_is_not_masked(sentence, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, path, sentence, "Harmless follow-up message.")
    assert f"{sentence}\n{PAD_USER}" in stored_texts()


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", SNAKE_CREDENTIAL, ids=[t.split()[1] if t[0] in "Tt" else t.split()[0]
                                                              for t, _ in SNAKE_CREDENTIAL])
def test_3_credential_snake_case_name_is_masked(text, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, path, text, "Harmless follow-up message.")
    assert "[REDACTED]" in first_turn()
    assert leaks(env, value) == []


# -- guards for the finish ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("sentence", THIRTEEN, ids=[f"s{i:02d}" for i in range(1, 14)])
def test_guard_the_13_sentences_are_still_unchanged(sentence, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run(env, path, sentence, "Harmless follow-up message.")
    assert f"{sentence}\n{PAD_USER}" in stored_texts()


@pytest.mark.parametrize("mode", ["three-ways", "claude-resumed", "agy-stop-hooks"])
def test_guard_table_7719_never_leaks(mode, tmp_path, monkeypatch):
    """claude, agy and direct (one capture each), then the two multi-capture paths, with CLI and MCP read-back;
    16-hex hashes blanked (see test_working_redaction_review4.test_guard_table_3307_never_leaks)."""
    from memory.test_working_redaction import words_in
    from memory.test_working_redaction_review4 import _HASH_FIELD
    corpus = table_corpus(7719)
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
    print(f"seed 7719 ({mode}): 525 inputs, {len(found_leaks)} leaks")
    assert not found_leaks, f"{len(found_leaks)} leaks, first: {found_leaks[:10]}"


@pytest.mark.parametrize("text, value", [("db_password = hunter2", "hunter2"), ("session_cookie = abc123x", "abc123x"),
                                         ("user_password: tangerine", "tangerine")])
def test_snake_case_name_with_a_strong_suffix_is_masked(text, value, tmp_path, monkeypatch):
    """Was a strict xfail (review 6 gap); closed in G3h by the strong suffixes _password, _passwd, _cookie, _pin."""
    env = Env(tmp_path, monkeypatch)
    run(env, "direct", text, "Harmless follow-up message.")
    assert leaks(env, value) == []
