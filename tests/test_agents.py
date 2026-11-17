"""Adapter tests that never touch the network or spawn a real model."""

import asyncio

import pytest

from room.agents import build_agent
from room.agents.base import AgentError, build_prompt, redact
from room.agents.claude_cli import ClaudeCLIAgent, build_command
from room.transcript import Message


def test_build_prompt_first_and_later_turns():
    _, first = build_prompt("hermes", "Q?", [Message("s", "user", 0, "user", "Q?")])
    assert "first to answer" in first and "Q?" in first
    t = [Message("s", "user", 0, "user", "Q?"), Message("s", "gemini", 1, "agent", "A1")]
    system, later = build_prompt("muse", "Q?", t)
    assert "You are muse" in system
    assert "[round 1] gemini:\nA1" in later


def test_redact_removes_key_values(monkeypatch):
    monkeypatch.setenv("MUSE_API_KEY", "super-secret-value-123")
    assert "super-secret" not in redact("bad header: Bearer super-secret-value-123")


def test_unknown_agent():
    with pytest.raises(ValueError):
        build_agent("gpt")


def test_missing_keys_fail_gracefully(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    for name in ("gemini", "muse"):
        agent = build_agent(name)
        ok, detail = asyncio.run(agent.healthcheck())
        assert not ok and "not configured" in detail
        with pytest.raises(AgentError):
            asyncio.run(agent.reply("q", []))


def test_hermes_unreachable_is_graceful(monkeypatch):
    monkeypatch.setenv("HERMES_BASE_URL", "http://127.0.0.1:9/v1")  # discard port: nothing listens
    agent = build_agent("hermes")
    agent.timeout = 2
    ok, detail = asyncio.run(agent.healthcheck())
    assert not ok and "hermes" in detail


def test_claude_command_has_no_tools():
    cmd = build_command("claude", "sys", "Task: hi")
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in cmd
    assert cmd[-1] == "Task: hi"


def test_claude_missing_binary(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "definitely-not-a-real-claude-binary")
    agent = ClaudeCLIAgent()
    ok, detail = asyncio.run(agent.healthcheck())
    assert not ok and "not found" in detail
    with pytest.raises(AgentError, match="not found"):
        asyncio.run(agent.reply("q", []))


def _fake_cli(tmp_path, body):
    script = tmp_path / "fake-claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script)


def test_claude_reply_uses_stdout_and_strips_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-leak")
    monkeypatch.setenv("CLAUDE_BIN", _fake_cli(tmp_path, 'echo "answer, key=${GEMINI_API_KEY:-unset}"\n'))
    assert asyncio.run(ClaudeCLIAgent().reply("q", [])) == "answer, key=unset"


def test_claude_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", _fake_cli(tmp_path, 'echo "not logged in" >&2\nexit 1\n'))
    with pytest.raises(AgentError, match="exited with code 1: not logged in"):
        asyncio.run(ClaudeCLIAgent().reply("q", []))


def test_claude_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", _fake_cli(tmp_path, "sleep 5\n"))
    with pytest.raises(AgentError, match="timed out"):
        asyncio.run(ClaudeCLIAgent(timeout=0.3).reply("q", []))
