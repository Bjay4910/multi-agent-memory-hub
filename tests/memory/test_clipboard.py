import json
import subprocess

import pytest

from room import cli, clipboard
from room.clipboard import MAX_CLIPBOARD_CHARS

from .test_cli_memory import fakes, room  # noqa: F401 - fixture reuse

# Fake key, assembled at runtime so the repo never holds a key-shaped literal.
FAKE_KEY = "sk" "-" + "proj" + "A1b2C3d4E5f6G7h8J9k0"


@pytest.fixture
def clip(monkeypatch):
    """Mock pbpaste: set clip.text to what the clipboard holds."""
    state = {"text": "", "calls": 0}

    def fake_run(cmd, **kw):
        assert cmd == ["pbpaste"]
        state["calls"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout=state["text"].encode(), stderr=b"")

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: "/usr/bin/pbpaste")
    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    return state


def _answer(monkeypatch, reply):
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if reply is EOFError:
            raise EOFError
        return reply

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def _session(isolated_home):
    files = list((isolated_home / "sessions").glob("clipboard-*.jsonl"))
    return json.loads(files[0].read_text()) if files else None


def test_clipboard_saved_after_yes(fakes, clip, monkeypatch, capsys, isolated_home):  # noqa: F811
    clip["text"] = "Chat summary: I moved Agent Room memory to a vault. " + "x" * 400
    prompts = _answer(monkeypatch, "y")
    assert room("remember", "--clipboard", "--tag", "chat") == 0
    out = capsys.readouterr().out
    assert prompts == ["Save this to memory? [y/N] "]
    assert f"Clipboard: {len(clip['text']):,} characters" in out
    preview = out.split("---\n")[1]
    assert preview.startswith("Chat summary") and len(preview) < 330  # ~300 chars + "..."
    assert "Saved note clipboard-" in out and "(memory) stored by hermes" in out
    line = _session(isolated_home)
    assert line["agent"] == "clipboard" and line["content"].startswith("Tags: chat")
    room("memory", "show", "Agent Room")
    assert "source: clipboard clipboard-" in capsys.readouterr().out


@pytest.mark.parametrize("reply", ["", "n", "no", "maybe", EOFError])
def test_clipboard_default_is_no(fakes, clip, monkeypatch, capsys, isolated_home, reply):  # noqa: F811
    clip["text"] = "something"
    _answer(monkeypatch, reply)
    assert room("remember", "--clipboard") == 1
    assert "Not saved." in capsys.readouterr().out
    assert _session(isolated_home) is None
    assert "extractor" not in fakes.built


def test_clipboard_yes_flag_skips_prompt(fakes, clip, monkeypatch, isolated_home):  # noqa: F811
    clip["text"] = "note"
    prompts = _answer(monkeypatch, "n")
    assert room("remember", "--clipboard", "--yes") == 0
    assert prompts == [] and _session(isolated_home)["content"] == "note"


def test_clipboard_redacted_before_preview_and_storage(fakes, clip, monkeypatch, capsys, isolated_home):  # noqa: F811
    clip["text"] = f"my key is {FAKE_KEY} and api_" "key=supersecretvalue123"
    assert room("remember", "--clipboard", "--yes") == 0
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err and "supersecretvalue123" not in captured.out
    raw = next((isolated_home / "sessions").glob("clipboard-*.jsonl")).read_text()
    assert FAKE_KEY not in raw and "[REDACTED]" in raw
    assert FAKE_KEY not in fakes.built["extractor"].prompts[0][1]


def test_clipboard_size_cap(fakes, clip, monkeypatch, capsys, isolated_home):  # noqa: F811
    clip["text"] = "a" * (MAX_CLIPBOARD_CHARS + 1)
    prompts = _answer(monkeypatch, "y")
    assert room("remember", "--clipboard") == 1
    assert "the limit is 50,000" in capsys.readouterr().err
    assert prompts == [] and _session(isolated_home) is None


def test_clipboard_empty_or_unavailable(clip, monkeypatch, capsys):
    clip["text"] = "   \n"
    assert room("remember", "--clipboard", "--yes") == 1
    assert "clipboard is empty" in capsys.readouterr().err
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert room("remember", "--clipboard", "--yes") == 1
    assert "pbpaste not found" in capsys.readouterr().err


def test_clipboard_with_text_is_rejected(clip, capsys):
    assert room("remember", "hello", "--clipboard") == 2
    assert clip["calls"] == 0


def test_clipboard_queued_when_hermes_is_down(clip, monkeypatch, capsys, isolated_home):
    from room.agents.base import AgentError

    class Down:
        name = "hermes"

        async def complete(self, *a, **k):
            raise AgentError("hermes: Ollama is not running")

    monkeypatch.setattr(cli, "agent_factory", lambda name: Down())
    clip["text"] = "remember this"
    assert room("remember", "--clipboard", "--yes") == 0
    assert "queued for `room memory process`" in capsys.readouterr().out
    queued = list((isolated_home / "memory_queue").glob("clipboard-*.json"))
    assert len(queued) == 1 and json.loads(queued[0].read_text())["source"] == "clipboard"
