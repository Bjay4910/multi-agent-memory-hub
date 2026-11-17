"""Antigravity (agy) adapter tests using fake executables: no network, no real model."""

import asyncio
import json
import os
import time

import pytest

from room.agents import build_agent
from room.agents.base import AgentError
from room.agents.gemini_agy import AntigravityGeminiAgent, build_command
from room.agents.subprocess_cli import clean_output, strip_ansi


def fake_agy(tmp_path, body, monkeypatch):
    script = tmp_path / "fake-agy"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("AGY_BIN", str(script))
    return script


def reply(**kw):
    return asyncio.run(AntigravityGeminiAgent(**kw).reply("What is X?", []))


def test_gemini_name_maps_to_agy_adapter():
    assert isinstance(build_agent("gemini"), AntigravityGeminiAgent)
    assert build_agent("gemini-api").name == "gemini-api"


def test_command_is_text_only_and_prompt_last():
    cmd = build_command("agy", "You are gemini.\n\nTask: hi")
    assert cmd[0] == "agy"
    assert "--sandbox" in cmd and "--disable-slash-commands" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert settings == {"permissions": {"deny": ["*"]}}
    assert cmd[-2:] == ["-p", "You are gemini.\n\nTask: hi"]
    assert build_command("agy", "p", model="gemini-4-hydrogen-high")[-4:-2] == ["--model", "gemini-4-hydrogen-high"]


def test_reply_returns_clean_stdout(tmp_path, monkeypatch):
    fake_agy(tmp_path, r"""printf '\033[1;32mA graph database\033[0m stores nodes.\n'
printf 'warning: --mode plan has no effect\n'
""", monkeypatch)
    assert reply() == "A graph database stores nodes."


def test_prompt_contains_room_instructions_and_task(tmp_path, monkeypatch):
    fake_agy(tmp_path, 'for a; do last="$a"; done; printf "%s" "$last"\n', monkeypatch)
    out = reply()
    assert out.startswith("You are gemini") and "What is X?" in out


def test_runs_in_empty_temp_dir_without_stdin_or_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-value")
    monkeypatch.setenv("MUSE_API_KEY", "muse-test-value")
    fake_agy(tmp_path, r"""echo "cwd=$(basename "$PWD") files=$(ls -A | wc -l | tr -d ' ')"
echo "gemini=${GEMINI_API_KEY:-unset} muse=${MUSE_API_KEY:-unset}"
if read -r line; then echo "stdin=data"; else echo "stdin=empty"; fi
""", monkeypatch)
    out = reply()
    assert "cwd=agent-room-cli-" in out and "files=0" in out
    assert "gemini=unset muse=unset" in out
    assert "stdin=empty" in out


def test_empty_answer_surfaces_stderr_reason(tmp_path, monkeypatch):
    fake_agy(tmp_path, 'echo "jetski: no output produced - write_file permission auto-denied" >&2\n', monkeypatch)
    with pytest.raises(AgentError, match="no answer: jetski: no output produced"):
        reply()


def test_nonzero_exit_is_agent_error(tmp_path, monkeypatch):
    fake_agy(tmp_path, 'echo "Error: not signed in" >&2\nexit 3\n', monkeypatch)
    with pytest.raises(AgentError, match="exited with code 3: Error: not signed in"):
        reply()


def test_timeout_kills_whole_process_group(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    fake_agy(tmp_path, f"sleep 30 &\necho $! > {pidfile}\nwait\n", monkeypatch)
    start = time.monotonic()
    with pytest.raises(AgentError, match="timed out after"):
        reply(timeout=2.0)  # long enough for bash to write the pid file even on a busy machine
    assert time.monotonic() - start < 8
    child = int(pidfile.read_text())
    for _ in range(50):  # the grandchild must be gone too
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        pytest.fail("background child of agy survived the timeout")


def test_timeout_from_env(monkeypatch):
    monkeypatch.setenv("AGY_TIMEOUT", "7")
    assert AntigravityGeminiAgent().timeout == 7
    monkeypatch.delenv("AGY_TIMEOUT")
    assert AntigravityGeminiAgent().timeout == 120


def test_healthcheck_uses_version(tmp_path, monkeypatch):
    fake_agy(tmp_path, '[ "$1" = "--version" ] && echo "1.5.10" && exit 0\nexit 1\n', monkeypatch)
    ok, detail = asyncio.run(AntigravityGeminiAgent().healthcheck())
    assert ok and "Antigravity CLI 1.5.10" in detail


def test_missing_binary(monkeypatch):
    monkeypatch.setenv("AGY_BIN", "definitely-not-a-real-agy")
    ok, detail = asyncio.run(AntigravityGeminiAgent().healthcheck())
    assert not ok and "not found" in detail
    with pytest.raises(AgentError, match="not found"):
        reply()


def test_clean_output_handles_ansi_spinners_and_redraws():
    raw = "\x1b]0;agy\x07⠋\r⠙\r\x1b[2K\nThinking...\rAnswer line 1\n\x1b[33m⠸\x1b[0m\n---\n| a | b |\n"
    assert clean_output(raw) == "Answer line 1\n---\n| a | b |"
    assert strip_ansi("\x1b[1mbold\x1b[0m") == "bold"
    assert clean_output("jetski: noise\nreal answer", ("jetski:",)) == "real answer"
