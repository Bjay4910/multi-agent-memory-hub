"""Gap 3, G3h: snake_case names ending in _password, _passwd, _cookie or _pin are credential labels for any prefix.

Paths: "claude" and "agy" (one capture), "direct" (working.upsert), "claude-resumed" (a capture, more turns, a second
capture). Every check reads back through `room working list/show/search` and the MCP tools (via ``leaks``).
Fake values only. Temp homes only.
"""

import pytest

from memory.test_working_redaction import PAD_USER, Env
from memory.test_working_redaction_review import leaks
from memory.test_working_redaction_review4 import stored_texts
from memory.test_working_redaction_review5 import THIRTEEN, run5

PATHS = ["claude", "agy", "direct", "claude-resumed"]

SUFFIXED = [
    ("db_password = hunter2", "hunter2"), ("session_cookie = abc123x", "abc123x"),
    ("user_password: tangerine", "tangerine"), ("admin_passwd = hunter2", "hunter2"), ("card_pin = 4821", "4821"),
    ("mysql_password = velour7", "velour7"), ("redis_passwd: quartzine", "quartzine"),
    ("login_cookie = sess91ab", "sess91ab"), ("bank_pin = 7302", "7302"), ("root_password is marlin62", "marlin62"),
    ("ftp_passwd = heron58", "heron58"), ("auth_cookie: tidepool", "tidepool"), ("phone_pin: 5519", "5519"),
    ("smtp_password = kestrel", "kestrel"), ("vpn_passwd: bluebell", "bluebell"),
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", SUFFIXED, ids=[t.split()[0] for t, _ in SUFFIXED])
def test_suffixed_snake_case_name_is_masked(text, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run5(env, path, text, "Harmless follow-up message.")
    assert statuses[-1] in ("created", "updated"), statuses
    first = next(t for t in stored_texts() if t.endswith(PAD_USER))
    assert first.startswith(text.split(value)[0] + "[REDACTED]"), first
    assert leaks(env, value) == []


# -- guards: unchanged before and after ---------------------------------------------------------------------------

GUARDS = THIRTEEN + [
    "The foreign_key is checked on every insert.", "The primary_key is an auto-incrementing integer.",
    "The sort_key is the second element of the tuple.", "The cache_key is built from the request path.",
    "The source_key is stored only as a hash.",
    "We redesigned the password_reset_flow last sprint.", "The cookie_banner covers the footer on mobile.",
    "Move pin_code_layout into the shared components folder.", "The passwd_file_path setting points to /etc/passwd.",
    "Rename password_reset_flow to reset_flow in the router.", "The cookie_banner_text is translated per locale.",
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("sentence", GUARDS, ids=[f"g{i:02d}" for i in range(1, len(GUARDS) + 1)])
def test_guard_sentence_is_unchanged(sentence, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, path, sentence, "Harmless follow-up message.")
    assert f"{sentence}\n{PAD_USER}" in stored_texts()


@pytest.mark.xfail(strict=True, reason="documented gap: a lower-case snake_case name ending in _key, _token or _secret "
                   "with a prefix outside the credential whitelist keeps only a secret-shaped value masked")
@pytest.mark.parametrize("text, value", [("db_secret = tangerine", "tangerine"), ("deploy_token = hunter2", "hunter2")])
def test_gap_key_token_secret_suffix_outside_the_whitelist(text, value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, "direct", text, "Harmless follow-up message.")
    assert leaks(env, value) == []
