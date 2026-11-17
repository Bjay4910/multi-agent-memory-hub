import pytest

from room.memory.redact import redact_secrets

SECRETS = [
    "AI" "zaSyA1234567890abcdefghijklmnopqrstu",
    "s" "k-ant-api03-abcdefghijklmnop1234",
    "s" "k-proj-ABCDEFGHIJKLMNOP12345678",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "gh" "o_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "github_pat_11ABCDEFG0123456789_abcdefghijk",
    "LL" "M|1234567890|AbCdEfGhIjKlMnOp",
    "xoxb-1234567890-abcdefghij",
    "AKIAABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpM",
    "Zx9QpL2mN8vR4tY7wK1sD6fG3hJ5aB0cE2uI",  # long random token
]


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_patterns_are_removed(secret):
    out = redact_secrets(f"my value is {secret} ok")
    assert secret not in out and "[REDACTED]" in out
    assert out.startswith("my value is ") and out.endswith(" ok")


@pytest.mark.parametrize("text", [
    "Authorization: Bea" "rer abc.def-ghi123456",
    "api_" "key=hunter2hunter2", "password: 'correct-horse'", "TOKEN = abcdef123456",
])
def test_labelled_values_are_removed(text):
    out = redact_secrets(text)
    assert "[REDACTED]" in out
    assert not any(v in out for v in ("abc.def-ghi123456", "hunter2hunter2", "correct-horse", "abcdef123456"))


def test_private_key_block():
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----\nafter"
    assert redact_secrets(text) == "before\n[REDACTED]\nafter"


def test_configured_env_key_values_are_removed(monkeypatch):
    monkeypatch.setenv("MUSE_API_KEY", "plain-looking-value")
    assert "plain-looking-value" not in redact_secrets("key plain-looking-value here")


@pytest.mark.parametrize("text", [
    "/Users/x/.agent-room/sessions/20261117T164741Z-b89c21.jsonl",
    "https://github.com/Bjay4910/multi-agent-memory-hub",
    "I am applying to TU Munich and ETH Zurich for a CS masters in 2027.",
    "multi_agent_memory_hub_power_manager_project",  # long but not random
    "line one\n\n  indented line two",
])
def test_normal_text_is_untouched(text):
    assert redact_secrets(text) == text
