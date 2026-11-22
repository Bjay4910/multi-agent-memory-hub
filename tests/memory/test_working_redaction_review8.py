"""Gap 3, G3i: multi-word labels written with _ or -, more strong suffixes, flag clauses with a secret-shaped word.

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
W1, W3, N6 = "zanewetiri", "zanewetiri fenupebaw gifogixa", "663562"

# -- A. multi-word labels with _ or - -----------------------------------------------------------------------------
A = [
    (f"license_key = {W1}", W1), (f"recovery_code = {W1}", W1), (f"secret_code = {W1}", W1),
    (f"seed_phrase = {W3}", W3), (f"2fa_code = {N6}", N6), (f"auth_header = {W1}", W1),
    (f"mot_de_passe = {W1}", W1), (f"license-key = {W1}", W1),
    # 15 more
    (f"license_key: {W1}", W1), (f"LICENSE-KEY = {W1}", W1), (f"recovery-code = {N6}", N6),
    (f"secret-code: {W1}", W1), (f"seed-phrase: {W3}", W3), (f"2fa-code = {N6}", N6), (f"auth-header: {W1}", W1),
    (f"mot-de-passe = {W1}", W1), (f"my_license_key = {W1}", W1), (f"the recovery_code is {W1}", W1),
    (f"Recovery_Code = {W1}", W1), (f"the seed_phrase for prod is {W3}", W3), (f"backup_recovery_code: {N6}", N6),
    (f"Secret_Code = {W1}", W1), (f"session-cookie = {W1}", W1),
]

# -- B. strong suffixes --------------------------------------------------------------------------------------------
B = [
    ("user_passphrase = zanewetiri fenupebaw", "zanewetiri fenupebaw"), (f"vault_passcode = {N6}", N6),
    (f"staging_pwd = {W1}", W1), (f"admin_pw = {W1}", W1), (f"login_otp = {N6}", N6), (f"service_totp = {N6}", N6),
    (f"github_pat = {W1}", W1), (f"wifi_psk = {W1}", W1), (f"webhook_hmac = {W1}", W1),
    (f"api_credentials = {W1}", W1),
    # 15 more
    (f"app_passphrase: {W3}", W3), ("door_passcode = 4821", "4821"), (f"root_pwd = {W1}", W1), (f"db_pw: {W1}", W1),
    (f"sms_otp = {N6}", N6), (f"mfa_totp = {N6}", N6), (f"gitlab_pat = {W1}", W1), (f"router_psk = {W1}", W1),
    (f"signing_hmac: {W1}", W1), (f"auth_jwt = {W1}", W1), (f"aws_credentials = {W1}", W1),
    (f"the backup_passphrase is {W3}", W3), (f"Staging_Pwd = {W1}", W1), (f"my_wifi_psk: {W1}", W1),
    (f"deploy_credentials: {W1}", W1),
]

# -- C. a flag word, then a secret-shaped word of letters only ----------------------------------------------------
C = [
    ("the password is on dlvpCtzswt", "dlvpCtzswt"), ("my passphrase is no gfnfWqjlj", "gfnfWqjlj"),
    ("the password is off DbwvD7R", "DbwvD7R"),
    # 15 more
    ("the password is on qvwrAmzkp", "qvwrAmzkp"), ("password: off KMaZQVWRT", "KMaZQVWRT"),
    ("passphrase: yes dlvpCtzswtQ", "dlvpCtzswtQ"), ("the password is true gfnfWqjljX", "gfnfWqjljX"),
    ("api key: null dlvpCtzswt", "dlvpCtzswt"), ("the secret code is enabled qvwrAmzkp", "qvwrAmzkp"),
    ("password: disabled bxTrqvmLpz", "bxTrqvmLpz"), ("the password is nil KMaZQVWRT", "KMaZQVWRT"),
    ("password = no qvwrAmzkp", "qvwrAmzkp"), ("my passphrase is false dlvpCtzswt", "dlvpCtzswt"),
    ("token: yes qvwrAmzkp", "qvwrAmzkp"), ("the secret is on gfnfWqjlj", "gfnfWqjlj"),
    ("auth: off dlvpCtzswt", "dlvpCtzswt"), ("password: ON qvwrAmzkp", "qvwrAmzkp"),
    ("the pin is none bxTrqvmLpz", "bxTrqvmLpz"),
]


def _ids(cases):
    return [f"{i:02d}-{t.split()[0] if '_' in t.split()[0] or '-' in t.split()[0] else t[:18]}" for i, (t, _) in
            enumerate(cases, 1)]


def _check(text, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses = run5(env, path, text, "Harmless follow-up message.")
    assert statuses[-1] in ("created", "updated"), statuses
    first = next(t for t in stored_texts() if t.endswith(PAD_USER))
    assert "[REDACTED]" in first and value not in first, first
    assert leaks(env, value) == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", A, ids=_ids(A))
def test_a_multi_word_label_with_underscore_or_hyphen_is_masked(text, value, path, tmp_path, monkeypatch):
    _check(text, value, path, tmp_path, monkeypatch)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", B, ids=_ids(B))
def test_b_strong_suffix_is_masked(text, value, path, tmp_path, monkeypatch):
    _check(text, value, path, tmp_path, monkeypatch)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text, value", C, ids=_ids(C))
def test_c_flag_word_then_a_secret_shaped_word_is_masked(text, value, path, tmp_path, monkeypatch):
    _check(text, value, path, tmp_path, monkeypatch)


@pytest.mark.xfail(strict=True, reason="cannot be done in working.py: quality.SECRET_LABELS has 'credentials' but no "
                   "singular 'credential', so the extraction filter never matches and there is nothing to keep")
def test_b_singular_credential_suffix(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, "direct", f"db_credential = {W1}", "Harmless follow-up message.")
    assert leaks(env, W1) == []


# -- guards: unchanged before and after -----------------------------------------------------------------------------

GUARDS = THIRTEEN + [
    "The foreign_key is checked on every insert.", "The primary_key is an auto-incrementing integer.",
    "The sort_key is the second element of the tuple.", "The cache_key is built from the request path.",
    "The source_key is stored only as a hash.", "The partition_key is the tenant id.",
    "PRAGMA foreign_keys = ON during bulk import.",
    "We redesigned the password_reset_flow last sprint.", "The cookie_banner covers the footer on mobile.",
    "Move pin_code_layout into the shared components folder.", "The passwd_file_path setting points to /etc/passwd.",
    "The pw_policy_doc lives in the wiki.", "The otp_screen_layout ships next week.",
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("sentence", GUARDS, ids=[f"g{i:02d}" for i in range(1, len(GUARDS) + 1)])
def test_guard_sentence_is_unchanged(sentence, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, path, sentence, "Harmless follow-up message.")
    assert f"{sentence}\n{PAD_USER}" in stored_texts()


@pytest.mark.xfail(strict=True, reason="documented gap: a camelCase name (userPassword, dbPassword) is not a label in "
                   "quality's pattern (the label must not follow a letter), so there is no match to keep")
@pytest.mark.parametrize("text", [f"userPassword = {W1}", f"dbPassword: {W1}"])
def test_gap_camel_case_label(text, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    run5(env, "direct", text, "Harmless follow-up message.")
    assert leaks(env, W1) == []
