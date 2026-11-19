"""Claude Code capture into the working layer, and the hook installer.

Made-up transcripts under a temp CLAUDE_CONFIG_DIR, temp AGENT_ROOM_HOME,
subprocess.Popen mocked. Never touches ~/.claude or ~/.agent-room.
"""

import io
import json
import os
import stat
import sys
import threading
import time

import pytest

from room import capture, cli
from room.agents.subprocess_cli import child_env
from room.memory import working

FAKE_USER = "gh" "p_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6"
FAKE_ASSIST = "s" "k-" + "proj9Xy8Wv7Ut6Sr5Qp4On3Ml2Kj1"
SID = "0b9f3c4e-1111-4a2b-9c8d-123456789abc"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch, isolated_home):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.delenv("ROOM_SPAWNED", raising=False)
    return tmp_path / "claude-config"


@pytest.fixture
def enabled(isolated_home):
    capture.set_enabled(True)


class Transcript:
    def __init__(self, root, session_id=SID, cwd="/Users/someone/Developer/demo-proj", entrypoint="cli"):
        self.path = root / "projects" / "-Users-someone-Developer-demo-proj" / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id, self.cwd, self.entrypoint, self.n = session_id, cwd, entrypoint, 0

    def _write(self, entry):
        self.n += 1
        entry.setdefault("uuid", f"00000000-0000-4000-8000-{self.n:012d}")
        entry.setdefault("timestamp", f"2026-11-18T10:{self.n % 60:02d}:00.000Z")
        entry.update({"sessionId": self.session_id, "cwd": self.cwd, "entrypoint": self.entrypoint})
        entry.setdefault("isSidechain", False)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def user(self, text, **extra):
        self._write({"type": "user", "message": {"role": "user", "content": text}, **extra})

    def assistant(self, *blocks, **extra):
        self._write({"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}, **extra})

    def raw(self, entry):
        self._write(entry)

    def hook(self, **extra):
        return {"session_id": self.session_id, "transcript_path": str(self.path), "cwd": self.cwd,
                "hook_event_name": "SessionEnd", "reason": "prompt_input_exit", **extra}


def text(t):
    return {"type": "text", "text": t}


def rich_session(root):
    t = Transcript(root)
    t.raw({"type": "permission-mode", "permissionMode": "default"})
    t.user("Please help me plan the Okapi-Lantern release; keep the README short and plain.")
    t.user("<system-reminder>META-REMINDER-TEXT</system-reminder>", isMeta=True)
    t.assistant({"type": "thinking", "thinking": "PRIVATE-THINKING-TEXT"}, text("Sure. Plan: one README, one changelog."),
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/TOOL-INPUT-PATH"}})
    t.user([{"type": "tool_result", "tool_use_id": "t1", "content": "FILE-CONTENTS-BLOB " * 200}])
    t.raw({"type": "attachment", "attachment": {"content": "ATTACHMENT-TEXT"}})
    t.raw({"type": "api-request-blob", "content": [text("API-BLOB-TEXT")]})
    t.assistant(text("SIDECHAIN-TEXT"), isSidechain=True)
    t.user([text("<system-reminder>INLINE-REMINDER</system-reminder>Also: I prefer pytest."),
            {"type": "image", "source": {"type": "base64", "data": "IMAGEDATA"}}])
    t.user("<command-name>/clear</command-name><command-args></command-args>")
    t.user("<local-command-stdout>LOCAL-COMMAND-OUTPUT</local-command-stdout>")
    t.user("HUGE-PASTE " + "lorem ipsum " * 2000)
    t.assistant(text("Noted: pytest, short README for Okapi-Lantern."))
    return t


def stored(home):
    d = home / "working"
    notes = [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
    return notes


def all_bytes(home):
    return b"".join(p.read_bytes() for p in home.rglob("*") if p.is_file())


# -- 5. filtering --------------------------------------------------------------------------------------

def test_keeps_only_user_and_assistant_text(env, enabled, isolated_home):
    t = rich_session(env)
    assert capture.capture_claude(t.hook()) == "created"
    [note] = stored(isolated_home)
    body = "\n".join(f"{x['role']}: {x['text']}" for x in note["turns"])
    assert "Okapi-Lantern release" in body and "Plan: one README" in body and "Also: I prefer pytest." in body
    for banned in ("META-REMINDER-TEXT", "PRIVATE-THINKING-TEXT", "TOOL-INPUT-PATH", "FILE-CONTENTS-BLOB",
                   "ATTACHMENT-TEXT", "API-BLOB-TEXT", "SIDECHAIN-TEXT", "INLINE-REMINDER", "IMAGEDATA",
                   "/clear", "LOCAL-COMMAND-OUTPUT"):
        assert banned not in body, banned
    [paste] = [x for x in note["turns"] if x["text"].startswith("HUGE-PASTE")]
    assert len(paste["text"]) <= capture.MAX_TURN_CHARS + 80 and "characters cut]" in paste["text"]
    assert note["project"] == "demo-proj" and note["source"] == "claude-code"
    assert SID not in json.dumps(note)  # the Claude session uuid is stored only as a hash


# -- 6/16. secrets never reach disk ------------------------------------------------------------------------

def test_real_secret_in_transcript_never_reaches_disk(env, enabled, isolated_home):
    t = Transcript(env)
    t.user(f"My GitHub token is {FAKE_USER}, I rotated it. Planning the Okapi-Lantern release notes today, "
           "with a short changelog, a migration note for existing users and a list of known issues.")
    t.assistant(text(f"Do not paste keys like {FAKE_ASSIST}. For Okapi-Lantern, keep the changelog short."))
    t.user("Second question: should the changelog list every fix or only user-facing ones? "
           "I want it readable by people who never open the code.")
    t.assistant(text("Only user-facing ones."))
    raw = t.path.read_bytes()
    assert FAKE_USER.encode() in raw and FAKE_ASSIST.encode() in raw  # the secrets are really in the input
    assert capture.capture_claude(t.hook()) == "created"
    data = all_bytes(isolated_home)
    assert FAKE_USER.encode() not in data and FAKE_ASSIST.encode() not in data
    assert b"[REDACTED]" in data


def test_secret_surviving_redaction_is_refused(env, enabled, isolated_home, monkeypatch):
    monkeypatch.setattr(capture, "redact_secrets", lambda s: s)
    monkeypatch.setattr(working, "redact_secrets", lambda s: s)
    t = Transcript(env)
    t.user(f"token {FAKE_USER} and a long enough message about the Okapi-Lantern release plan " * 3)
    t.user("and a second user message so it is not too short, about the release order and the owners")
    assert FAKE_USER.encode() in t.path.read_bytes()  # the secret is really in the input
    assert capture.capture_claude(t.hook()) == "refused-secret"
    assert stored(isolated_home) == []
    assert FAKE_USER.encode() not in all_bytes(isolated_home)


# -- 7. never captures the room itself -------------------------------------------------------------------

def test_room_spawned_children_carry_the_guard():
    assert child_env()["ROOM_SPAWNED"] == "1"


def test_room_spawned_is_never_captured(env, enabled, isolated_home, monkeypatch):
    t = rich_session(env)
    monkeypatch.setenv("ROOM_SPAWNED", "1")
    assert capture.capture_claude(t.hook()) == "room-spawned"
    calls = []
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert capture.run_hook(json.dumps(t.hook())) == 0
    assert calls == [] and stored(isolated_home) == []


def test_non_interactive_and_missing_transcripts_are_skipped(env, enabled, isolated_home, tmp_path):
    t = Transcript(env, entrypoint="sdk-cli")  # what `claude -p` records
    t.user("long enough text about a plan " * 20)
    t.user("second message")
    assert capture.capture_claude(t.hook()) == "non-interactive"
    assert capture.capture_claude({**t.hook(), "transcript_path": str(env / "projects" / "nope.jsonl")}) == "missing-transcript"
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(t.path.read_text().replace("sdk-cli", "cli"))
    assert capture.capture_claude({**t.hook(), "transcript_path": str(outside)}) == "outside-projects"
    assert capture.capture_claude({**t.hook(), "transcript_path": str(env / "projects" / ".." / ".." / "elsewhere.jsonl")}) == "outside-projects"
    assert stored(isolated_home) == []


# -- 8. opt-in ------------------------------------------------------------------------------------------------

def test_disabled_by_default(env, isolated_home, monkeypatch):
    t = rich_session(env)
    assert not capture.is_enabled()
    assert capture.capture_claude(t.hook()) == "disabled"
    calls = []
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert capture.run_hook(json.dumps(t.hook())) == 0
    assert calls == [] and stored(isolated_home) == []


def test_enable_disable_status_commands(isolated_home, capsys):
    assert cli.main(["capture", "status"]) == 0 and "off" in capsys.readouterr().out
    assert cli.main(["capture", "enable"]) == 0 and capture.is_enabled()
    assert stat.S_IMODE((isolated_home / "capture").stat().st_mode) == 0o700
    assert cli.main(["capture", "status"]) == 0 and "on" in capsys.readouterr().out
    assert cli.main(["capture", "disable"]) == 0 and not capture.is_enabled()


# -- 9/20. short, dedupe, resume, two at once -------------------------------------------------------------------

def test_short_session_skipped(env, enabled, isolated_home):
    t = Transcript(env)
    t.user("hi")
    t.assistant(text("hello"))
    assert capture.capture_claude(t.hook()) == "too-short"
    assert stored(isolated_home) == []


def test_dedupe_and_resume_store_only_new_turns(env, enabled, isolated_home):
    t = rich_session(env)
    assert capture.capture_claude(t.hook()) == "created"
    assert capture.capture_claude(t.hook()) == "unchanged"
    [first] = stored(isolated_home)
    t.user("Resumed later: the Okapi-Lantern date moved to Friday.")
    t.assistant(text("Updated the plan for Friday."))
    assert capture.capture_claude(t.hook(reason="other")) == "updated"
    [second] = stored(isolated_home)
    assert second["id"] == first["id"]
    assert second["turns"][:len(first["turns"])] == first["turns"]
    assert [x["text"] for x in second["turns"][len(first["turns"]):]] == [
        "Resumed later: the Okapi-Lantern date moved to Friday.", "Updated the plan for Friday."]


def test_two_captures_at_once(env, enabled, isolated_home):
    a = rich_session(env)
    b = Transcript(env, session_id="0b9f3c4e-2222-4a2b-9c8d-123456789abc")
    for i in range(3):
        b.user(f"Session B message {i} about the Kiwi-Harbor migration, which needs a careful plan "
               "covering the data copy, the cut-over window and the rollback steps.")
        b.assistant(text(f"Session B reply {i}."))
    results, barrier = [], threading.Barrier(6)

    def run(tr):
        barrier.wait()
        results.append(capture.capture_claude(tr.hook()))

    threads = [threading.Thread(target=run, args=(tr,)) for tr in (a, a, a, b, b, b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["created", "created", "unchanged", "unchanged", "unchanged", "unchanged"]
    notes = stored(isolated_home)
    assert len(notes) == 2
    for n in notes:
        assert len({x["text"] for x in n["turns"]}) == len(n["turns"])


# -- 10. size cap -------------------------------------------------------------------------------------------------

def test_size_cap(env, enabled, isolated_home):
    t = Transcript(env)
    for i in range(30):
        t.user(f"question {i:02d} " + "words " * 300)
        t.assistant(text(f"answer {i:02d} " + "reply " * 300))
    assert capture.capture_claude(t.hook()) == "created"
    [note] = stored(isolated_home)
    assert sum(len(x["text"]) for x in note["turns"]) <= working.MAX_NOTE_CHARS
    assert note["turns"][-1]["text"].startswith("answer 29")


# -- 11. the hook never fails or blocks Claude Code -------------------------------------------------------------------

class FakePopen:
    calls = []

    def __init__(self, argv, **kwargs):
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        FakePopen.calls.append((argv, kwargs, self))

    def wait(self, *a, **k):
        raise AssertionError("the hook waited for the worker")


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"session_id": 5}', '{"transcript_path": "/x"}',
                                 json.dumps({"session_id": "s", "transcript_path": "x" * 10}) + " " * (70 * 1024)])
def test_hook_bad_input_exits_zero_fast(enabled, monkeypatch, raw):
    FakePopen.calls = []
    monkeypatch.setattr(capture.subprocess, "Popen", FakePopen)
    started = time.monotonic()
    assert capture.run_hook(raw) == 0
    assert time.monotonic() - started < 1.0
    assert FakePopen.calls == []


def test_hook_starts_detached_worker_with_fixed_argv(env, enabled, monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(capture.subprocess, "Popen", FakePopen)
    t = rich_session(env)
    started = time.monotonic()
    assert capture.run_hook(json.dumps(t.hook())) == 0
    assert time.monotonic() - started < 1.0
    [(argv, kwargs, proc)] = FakePopen.calls
    assert argv == [sys.executable, "-m", "room.cli", "capture", "claude", "--worker"]
    assert not any(SID in a or "demo-proj" in a for a in argv)
    assert kwargs.get("shell", False) is False and kwargs["start_new_session"] is True
    assert kwargs["stdin"] == capture.subprocess.PIPE
    assert kwargs["stdout"] == kwargs["stderr"] == capture.subprocess.DEVNULL
    assert json.loads(proc.stdin.getvalue())["session_id"] == SID  # input goes over stdin, not argv


def test_hook_survives_popen_failure(env, enabled, monkeypatch):
    def boom(*a, **k):
        raise OSError("no fork")
    monkeypatch.setattr(capture.subprocess, "Popen", boom)
    assert capture.run_hook(json.dumps(rich_session(env).hook())) == 0


def test_worker_survives_read_only_home(env, tmp_path, monkeypatch):
    home = tmp_path / "ro-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_ROOM_HOME", str(home))
    (home / "capture").mkdir()
    (home / "capture" / "enabled").write_text("")
    home.chmod(0o500)
    try:
        assert capture.run_worker(json.dumps(rich_session(env).hook())) == 0
    finally:
        home.chmod(0o700)


def test_cli_hook_entry_always_exits_zero(enabled, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("garbage"))
    assert cli.main(["capture", "claude"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("garbage"))
    assert cli.main(["capture", "claude", "--worker"]) == 0


# -- 12. hook installer ----------------------------------------------------------------------------------------------

POWER_MANAGER = {"type": "command", "command": "/Users/someone/pm/bin/agent-power hook session-end", "timeout": 3}


def existing_settings():
    return {
        "enabledPlugins": {"some-plugin@market": True},
        "theme": "dark",
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "/Users/someone/pm/bin/agent-power hook start"}]}],
            "SessionEnd": [{"matcher": "", "hooks": [POWER_MANAGER]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/Users/someone/pm/bin/agent-power hook stop", "async": True}]}],
        },
    }


def test_print_shows_valid_json(capsys):
    assert cli.main(["capture", "install-hook", "--print"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    [group] = cfg["hooks"]["SessionEnd"]
    [hook] = group["hooks"]
    assert hook["type"] == "command" and hook["command"].endswith(" capture claude")
    assert hook["timeout"] == capture.HOOK_TIMEOUT_S


def test_apply_merges_and_uninstall_removes_only_ours(tmp_path, capsys):
    path = tmp_path / "settings.json"
    original = json.dumps(existing_settings(), indent=2) + "\n"
    path.write_text(original)
    path.chmod(0o644)

    assert cli.main(["capture", "install-hook", "--apply", "--settings", str(path)]) == 0
    after = json.loads(path.read_text())
    backups = sorted(tmp_path.glob("settings.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    expected = existing_settings()
    ours = capture.hook_config()["hooks"]["SessionEnd"][0]
    expected["hooks"]["SessionEnd"].append(ours)
    assert after == expected  # everything kept, ours appended once

    assert cli.main(["capture", "install-hook", "--apply", "--settings", str(path)]) == 0
    assert json.loads(path.read_text()) == expected and len(list(tmp_path.glob("settings.json.bak-*"))) == 1
    assert "already installed" in capsys.readouterr().out

    assert cli.main(["capture", "install-hook", "--uninstall", "--settings", str(path)]) == 0
    assert json.loads(path.read_text()) == existing_settings()
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 2
    assert cli.main(["capture", "install-hook", "--uninstall", "--settings", str(path)]) == 0
    assert "not installed" in capsys.readouterr().out


def test_apply_creates_missing_file(tmp_path):
    path = tmp_path / "new" / "settings.json"
    assert cli.main(["capture", "install-hook", "--apply", "--settings", str(path)]) == 0
    assert json.loads(path.read_text()) == capture.hook_config()


@pytest.mark.parametrize("content", ["{not json", "[]", '{"hooks": []}', '{"hooks": {"SessionEnd": {}}}'])
def test_apply_refuses_unexpected_files_untouched(tmp_path, content, capsys):
    path = tmp_path / "settings.json"
    path.write_text(content)
    assert cli.main(["capture", "install-hook", "--apply", "--settings", str(path)]) == 2
    assert path.read_text() == content and list(tmp_path.glob("*.bak-*")) == []
    assert "not changed" in capsys.readouterr().err


def test_install_hook_needs_exactly_one_action(capsys):
    with pytest.raises(SystemExit):
        cli.main(["capture", "install-hook"])
    with pytest.raises(SystemExit):
        cli.main(["capture", "install-hook", "--print", "--apply"])


def test_default_settings_path_is_claude_home(env):
    assert capture.default_settings_path() == env / "settings.json"


# -- review findings: compaction summaries, interrupt markers, cut-then-redact ------------------------

def test_compaction_summary_and_interrupt_markers_are_not_user_text(env, enabled, isolated_home):
    t = rich_session(env)
    t.user("COMPACT-SUMMARY-TEXT: the model's summary of earlier work, tool output and files.",
           isCompactSummary=True, isVisibleInTranscriptOnly=True)
    t.user("[Request interrupted by user]")
    t.user([text("[Request interrupted by user for tool use]")])
    assert capture.capture_claude(t.hook()) == "created"
    [note] = stored(isolated_home)
    body = "\n".join(x["text"] for x in note["turns"])
    assert "COMPACT-SUMMARY-TEXT" not in body and "Request interrupted" not in body


def test_secret_split_by_the_turn_cut_is_still_redacted(env, enabled, isolated_home):
    """Redaction must run on the whole message before it is cut to MAX_TURN_CHARS."""
    t = Transcript(env)
    lead = "Planning notes for the Okapi-Lantern launch. " * 60
    lead = lead[: capture.MAX_TURN_CHARS - 12]  # the key starts 12 characters before the cut
    t.user(lead + FAKE_ASSIST + " and more planning text after the key " * 5)
    t.user("Second message: list the owners, dates and risks for the launch, in plain words please.")
    assert FAKE_ASSIST.encode() in t.path.read_bytes()
    assert capture.capture_claude(t.hook()) == "created"
    data = all_bytes(isolated_home)
    assert FAKE_ASSIST[:12].encode() not in data  # not even the part before the cut
