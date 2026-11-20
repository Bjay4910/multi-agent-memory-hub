"""Antigravity CLI (agy) capture into the working layer, and its hooks.json installer.

Made-up transcripts in the real agy format under a temp ANTIGRAVITY_CLI_HOME, temp
AGENT_ROOM_HOME and temp HOME. Never touches ~/.gemini, ~/.claude or ~/.agent-room.
"""

import copy
import hashlib
import io
import json
import os
import sys
import threading
import time

import pytest

from room import capture, capture_agy, cli, mcp_server
from room.agents.base import SYSTEM_PROMPT
from room.memory import working

FAKE_USER = "gh" "p_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4"
FAKE_REPLY = "s" "k-" + "proj1Aa2Bb3Cc4Dd5Ee6Ff7Gg8Hh9Ii0"
CID = "6879392a-cea0-4fc6-9c37-4b81cc475186"
CID2 = "0d1c6d64-9c59-482d-8db8-0f93425f4de2"
CWD = "/Users/someone/Developer/agy-demo"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch, isolated_home):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("ANTIGRAVITY_CLI_HOME", str(tmp_path / "agy-cli"))
    monkeypatch.delenv("ROOM_SPAWNED", raising=False)
    # Default: the hook's ancestors look interactive. Tests that need -p override this.
    monkeypatch.setattr(capture_agy, "_ancestor_args", lambda: ["agy", "/bin/zsh"])
    return tmp_path


@pytest.fixture
def enabled(isolated_home):
    capture.set_enabled(True)


class AgyTranscript:
    """Writes lines in the format of ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl."""

    def __init__(self, root, cid=CID, name="transcript_full.jsonl"):
        self.cid = cid
        self.path = root / "agy-cli" / "brain" / cid / ".system_generated" / "logs" / name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.step = 0

    def _write(self, entry):
        entry.setdefault("step_index", self.step)
        entry.setdefault("created_at", f"2026-11-20T06:{self.step % 60:02d}:00Z")
        entry.setdefault("status", "DONE")
        self.step += 1
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def user(self, text, settings=False):
        content = f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nMETADATA-LOCAL-TIME\n</ADDITIONAL_METADATA>"
        if settings:
            content += "\n<USER_SETTINGS_CHANGE>\nSETTINGS-CHANGE-TEXT\n</USER_SETTINGS_CHANGE>"
        self._write({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content})

    def tool_step(self):
        self._write({"type": "PLANNER_RESPONSE", "source": "MODEL", "model": "m", "thinking": "PLANNER-THINKING-TEXT",
                     "tool_calls": [{"name": "run_command", "args": {"CommandLine": "TOOL-CALL-ARGS"}}]})
        self._write({"type": "GENERIC", "source": "MODEL", "content": "TOOL-OUTPUT-BLOB " * 50})

    def reply(self, text, thinking="PLANNER-THINKING-TEXT"):
        self._write({"type": "PLANNER_RESPONSE", "source": "MODEL", "model": "m", "content": text, "thinking": thinking})

    def system(self):
        self._write({"type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": "<SYSTEM_MESSAGE>SYSTEM-NOTICE-TEXT</SYSTEM_MESSAGE>"})

    def hook(self, **extra):
        payload = {"conversationId": self.cid, "transcriptPath": str(self.path), "cwd": CWD, "workspacePaths": [CWD],
                   "hookEventName": "Stop", "fullyIdle": True, "terminationReason": "model_stop",
                   "lastUserInput": "x", "modelName": "auto", "artifactDirectoryPath": "/x"}
        payload.update(extra)
        return payload


def two_turns(root, **kw):
    t = AgyTranscript(root, **kw)
    t.user("Please help me plan the Okapi-Lantern release; keep the README short and plain.", settings=True)
    t.tool_step()
    t.reply("Running the tests now; waiting for them to finish.")  # progress line, not the final reply
    t.tool_step()
    t.system()
    t.reply("Plan: one README, one changelog, release on Friday. The README covers install, usage and limits; "
            "the changelog lists every user-visible change since the last tag.")
    t.user("Also remember I prefer pytest over unittest for Okapi-Lantern, always please.")
    t.reply("Noted: pytest for Okapi-Lantern. Anything else for the release plan? I can draft the release notes "
            "and the checklist next if that helps.")
    return t


def notes(home):
    d = home / "working"
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def all_bytes(root):
    for dirpath, _, names in os.walk(root):
        for n in names:
            yield os.path.join(dirpath, n), open(os.path.join(dirpath, n), "rb").read()


# -- what is kept --------------------------------------------------------------------------------

def test_keeps_only_typed_user_text_and_final_replies(env, enabled, isolated_home):
    t = two_turns(env)
    assert capture_agy.capture_antigravity(t.hook()) == "created"
    [note] = notes(isolated_home)
    assert note["source"] == "antigravity" and note["project"] == "agy-demo"
    assert [x["role"] for x in note["turns"]] == ["user", "assistant", "user", "assistant"]
    body = json.dumps(note)
    assert "Okapi-Lantern release" in body and "one README, one changelog" in body
    for banned in ("PLANNER-THINKING-TEXT", "TOOL-CALL-ARGS", "TOOL-OUTPUT-BLOB", "SYSTEM-NOTICE-TEXT",
                   "METADATA-LOCAL-TIME", "SETTINGS-CHANGE-TEXT", "USER_REQUEST", "Running the tests now"):
        assert banned not in body, banned
    assert oct(os.stat(isolated_home / "working").st_mode & 0o777) == "0o700"
    assert all(oct(os.stat(p).st_mode & 0o777) == "0o600" for p in (isolated_home / "working").glob("*.json"))
    assert note["expires_at"] > note["captured_at"]


def test_plain_transcript_jsonl_is_accepted_too(env, enabled, isolated_home):
    t = two_turns(env, name="transcript.jsonl")
    assert capture_agy.capture_antigravity(t.hook()) == "created"


def test_big_paste_is_capped_and_marked(env, enabled, isolated_home):
    t = two_turns(env)
    t.user("PASTE " * 2000)
    t.reply("Thanks, read it.")
    capture_agy.capture_antigravity(t.hook())
    [note] = notes(isolated_home)
    paste = next(x["text"] for x in note["turns"] if x["text"].startswith("PASTE"))
    assert len(paste) < 2200 and "characters cut]" in paste


def test_secrets_never_reach_disk(env, enabled, isolated_home):
    t = AgyTranscript(env)
    t.user(f"My token is {FAKE_USER} please keep the Okapi-Lantern deploy notes short and plain for me.")
    t.reply(f"Do not paste keys like {FAKE_REPLY}; here is the plan for the Okapi-Lantern deploy.")
    t.user("Thanks, and also write the checklist for Okapi-Lantern with plain words please, all of it.")
    t.reply("Checklist: build, test, tag, release. Nothing else is needed for Okapi-Lantern. Each step has one "
            "owner, and the release waits until the tests pass on a clean checkout.")
    assert capture_agy.capture_antigravity(t.hook()) == "created"
    for path, data in all_bytes(isolated_home):
        assert FAKE_USER.encode() not in data and FAKE_REPLY.encode() not in data, path


# -- dedupe, resume, short, concurrency ------------------------------------------------------------

def test_per_turn_hook_never_duplicates_and_adds_only_new_turns(env, enabled, isolated_home):
    t = two_turns(env)
    assert capture_agy.capture_antigravity(t.hook()) == "created"
    assert capture_agy.capture_antigravity(t.hook()) == "unchanged"
    t.user("One more question about the Okapi-Lantern changelog format, please.")
    t.tool_step()
    t.reply("Use Keep a Changelog headings.")
    assert capture_agy.capture_antigravity(t.hook()) == "updated"
    [note] = notes(isolated_home)
    texts = [x["text"] for x in note["turns"]]
    assert len(texts) == 6 and len(set(texts)) == 6
    assert texts[-1] == "Use Keep a Changelog headings."


def test_single_turn_conversation_is_too_short(env, enabled, isolated_home):
    t = AgyTranscript(env)
    t.user("Reply with just the word hello.")
    t.reply("hello")
    assert capture_agy.capture_antigravity(t.hook()) == "too-short"
    assert notes(isolated_home) == []


def test_two_conversations_at_once(env, enabled, isolated_home):
    a, b = two_turns(env, cid=CID), two_turns(env, cid=CID2)
    results = []
    threads = [threading.Thread(target=lambda h=h: results.append(capture_agy.capture_antigravity(h)))
               for h in (a.hook(), b.hook(), a.hook(), b.hook())]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["created", "created", "unchanged", "unchanged"]
    assert len(notes(isolated_home)) == 2


# -- guards -----------------------------------------------------------------------------------------

def test_disabled_by_default(env, isolated_home):
    assert capture_agy.capture_antigravity(two_turns(env).hook()) == "disabled"
    assert notes(isolated_home) == []


def test_room_spawned_env_is_never_captured(env, enabled, isolated_home, monkeypatch):
    monkeypatch.setenv("ROOM_SPAWNED", "1")
    t = two_turns(env)
    assert capture_agy.capture_antigravity(t.hook()) == "room-spawned"
    started = []
    monkeypatch.setattr(capture_agy.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert capture_agy.run_hook(json.dumps(t.hook())) == 0
    assert started == [] and notes(isolated_home) == []


def test_room_prompt_transcript_is_skipped(env, enabled, isolated_home):
    t = AgyTranscript(env)
    t.user(SYSTEM_PROMPT.format(name="gemini") + "\n\nTask: what is a graph database? " * 5)
    t.reply("A graph database stores nodes and edges. " * 10)
    t.user("Round 2: refine your answer considering the other replies, in detail please.")
    t.reply("Refined: it is good for connected data. " * 10)
    assert capture_agy.capture_antigravity(t.hook()) == "room-spawned"
    assert notes(isolated_home) == []


def test_print_mode_parent_is_skipped_by_the_hook(env, enabled, monkeypatch):
    monkeypatch.setattr(capture_agy, "_ancestor_args", lambda: ["agy -p Reply with hello", "/bin/zsh"])
    started = []
    monkeypatch.setattr(capture_agy.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert capture_agy.run_hook(json.dumps(two_turns(env).hook())) == 0
    assert started == []


@pytest.mark.parametrize("args, interactive", [
    (["agy"], True), (["/Users/x/.local/bin/agy --model auto"], True),
    (["agy -p hi"], False), (["agy --print hi"], False), (["/usr/local/bin/agy --print=hi"], False),
    (["sh -c x", "agy -p hi"], False), (["python3 something -p"], True), ([], True),
])
def test_print_mode_detection(args, interactive):
    assert capture_agy.is_print_mode(args) is (not interactive)


def test_conversation_without_user_input_is_skipped(env, enabled, isolated_home):
    t = AgyTranscript(env)
    t.system()
    t.reply("Background task finished. " * 30)
    assert capture_agy.capture_antigravity(t.hook()) == "no-user-input"


# -- hostile payloads ----------------------------------------------------------------------------------

@pytest.mark.parametrize("cid", [
    "../../etc", "..", "a/b", "", "x" * 300, "6879392a-cea0-4fc6-9c37-4b81cc47518\x00", "6879392A-CEA0-4FC6-9C37-4B81CC475186",
    "6879392a-cea0-4fc6-9c37-4b81cc475186/../" + CID2, "ü879392a-cea0-4fc6-9c37-4b81cc475186", 12345, None,
])
def test_hostile_conversation_ids_are_refused(env, enabled, isolated_home, cid):
    t = two_turns(env)
    assert capture_agy.capture_antigravity(t.hook(conversationId=cid)) == "bad-input"
    assert notes(isolated_home) == []


def test_hostile_transcript_paths_are_refused(env, enabled, isolated_home):
    t = two_turns(env)
    other = two_turns(env, cid=CID2)
    outside = env / "outside.jsonl"
    outside.write_bytes(t.path.read_bytes())
    link_dir = env / "agy-cli" / "brain" / "11111111-2222-4333-8444-555555555555" / ".system_generated" / "logs"
    link_dir.mkdir(parents=True)
    (link_dir / "transcript_full.jsonl").symlink_to(outside)
    bad = [
        str(outside),
        str(other.path),  # a real transcript, but of another conversation
        str(t.path.parent / ".." / ".." / ".." / CID2 / ".system_generated" / "logs" / "transcript_full.jsonl"),
        str(t.path.with_name("other.jsonl")),
        str(t.path) + "\x00",
        "relative/transcript_full.jsonl",
        "x" * 5000,
    ]
    for p in bad:
        assert capture_agy.capture_antigravity(t.hook(transcriptPath=p)) in ("bad-input", "outside-brain"), p
    linked = {"conversationId": "11111111-2222-4333-8444-555555555555",
              "transcriptPath": str(link_dir / "transcript_full.jsonl"), "cwd": CWD}
    assert capture_agy.capture_antigravity(linked) == "outside-brain"
    assert notes(isolated_home) == []


def test_hostile_cwd_becomes_a_safe_tag(env, enabled, isolated_home):
    t = two_turns(env)
    assert capture_agy.capture_antigravity(t.hook(cwd="/x/../../$(rm -rf ~)/`id`;evil\n", workspacePaths=None)) == "created"
    [note] = notes(isolated_home)
    assert all(c.isalnum() or c in " _.-" for c in note["project"]) and len(note["project"]) <= 40
    assert sorted(p.name for p in (isolated_home / "working").iterdir()) == [".lock", f"{note['id']}.json"]


# -- large transcripts -------------------------------------------------------------------------------

def test_huge_file_is_refused_without_reading(env, enabled, isolated_home):
    t = two_turns(env)
    with t.path.open("r+b") as fh:
        fh.truncate(capture_agy.MAX_TRANSCRIPT_BYTES + 1)  # sparse: instant, no real disk use
    start = time.monotonic()
    assert capture_agy.capture_antigravity(t.hook()) == "too-large"
    assert time.monotonic() - start < 1


def test_many_lines_stop_at_the_line_cap_fast(env, enabled, isolated_home):
    t = two_turns(env)
    line = json.dumps({"type": "GENERIC", "source": "MODEL", "step_index": 9, "content": "x" * 40}) + "\n"
    with t.path.open("a") as fh:
        fh.write(line * (capture_agy.MAX_LINES + 10))
    start = time.monotonic()
    assert capture_agy.capture_antigravity(t.hook()) == "too-large"
    assert time.monotonic() - start < 5
    assert notes(isolated_home) == []


def test_one_giant_line_is_skipped_with_bounded_reads(env, enabled, isolated_home, monkeypatch):
    t = AgyTranscript(env)
    t.user("Please help me plan the Okapi-Lantern release; keep the README short and plain.")
    with t.path.open("a") as fh:
        fh.write(json.dumps({"type": "GENERIC", "content": "G" * (capture_agy.MAX_LINE_BYTES * 2)}) + "\n")
    t.reply("Plan: one README, one changelog, release on Friday, nothing more needed. The README covers "
            "install, usage and limits in plain words.")
    t.user("Also remember I prefer pytest over unittest for Okapi-Lantern, always please.")
    t.reply("Noted: pytest for Okapi-Lantern. Anything else for the release plan? I can draft the release notes "
            "and the checklist next if that helps, and keep both short and plain.")
    biggest = []
    real_open = open

    class Spy(io.BufferedReader):
        def readline(self, size=-1):
            assert 0 < size <= capture_agy.MAX_LINE_BYTES + 1
            out = super().readline(size)
            biggest.append(len(out))
            return out

    monkeypatch.setattr(capture_agy, "_open_binary", lambda p: Spy(real_open(p, "rb", buffering=0)))
    assert capture_agy.capture_antigravity(t.hook()) == "created"
    assert max(biggest) <= capture_agy.MAX_LINE_BYTES + 1
    assert len(notes(isolated_home)[0]["turns"]) == 4


# -- the hook process --------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"conversationId": 5}', "x" * (capture.MAX_HOOK_INPUT + 10)])
def test_hook_bad_input_exits_zero_fast(enabled, monkeypatch, raw):
    started = []
    monkeypatch.setattr(capture_agy.subprocess, "Popen", lambda *a, **k: started.append(a))
    t0 = time.monotonic()
    assert capture_agy.run_hook(raw) == 0
    assert time.monotonic() - t0 < 0.5 and started == []


def test_hook_starts_detached_worker_with_fixed_argv(env, enabled, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            self.stdin = io.BytesIO()
            self.stdin.close = lambda: seen.setdefault("payload", self.stdin.getvalue())

    monkeypatch.setattr(capture_agy.subprocess, "Popen", FakePopen)
    t = two_turns(env)
    assert capture_agy.run_hook(json.dumps(t.hook())) == 0
    assert seen["argv"] == [sys.executable, "-m", "room.cli", "capture", "antigravity", "--worker"]
    assert seen["kw"]["start_new_session"] is True and seen["kw"].get("shell") is not True
    payload = json.loads(seen["payload"])
    assert set(payload) <= {"conversationId", "transcriptPath", "cwd", "workspacePaths"}


def test_hook_survives_popen_failure(env, enabled, monkeypatch):
    def boom(*a, **k):
        raise OSError("no fork")
    monkeypatch.setattr(capture_agy.subprocess, "Popen", boom)
    assert capture_agy.run_hook(json.dumps(two_turns(env).hook())) == 0


def test_cli_hook_entry_prints_empty_object_and_exits_zero(enabled, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("garbage"))
    assert cli.main(["capture", "antigravity"]) == 0
    assert capsys.readouterr().out.strip() == "{}"


def test_cli_worker_captures(env, enabled, isolated_home, monkeypatch, capsys):
    t = two_turns(env)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(t.hook())))
    assert cli.main(["capture", "antigravity", "--worker"]) == 0
    assert len(notes(isolated_home)) == 1
    assert "created" in (isolated_home / "capture" / "capture.log").read_text()


# -- installer -----------------------------------------------------------------------------------------

REAL_SHAPE = """{
  "agent-power": {
    "enabled": true,
    "PreInvocation": [
      {
        "type": "command",
        "command": "/Users/someone/Developer/agent-aware-power-manager/.venv/bin/agent-power hook-event",
        "timeout": 5
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/someone/Developer/agent-aware-power-manager/.venv/bin/agent-power hook-event",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "/Users/someone/Developer/agent-aware-power-manager/.venv/bin/agent-power hook-event",
        "timeout": 5
      }
    ]
  }
}
"""


def power_group_text(text):
    start = text.index('  "agent-power": {')
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]


def test_print_shows_separate_agent_room_group(capsys):
    assert cli.main(["capture", "install-hook", "--agent", "antigravity", "--print"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert list(data) == ["agent-room"]
    [handler] = data["agent-room"]["Stop"]
    assert handler["type"] == "command" and handler["command"].endswith(" capture antigravity")
    assert handler["timeout"] <= 10


def test_apply_reapply_uninstall_never_alter_agent_power(tmp_path, capsys):
    f = tmp_path / "hooks.json"
    f.write_text(REAL_SHAPE)
    original_group = power_group_text(REAL_SHAPE)
    args = ["capture", "install-hook", "--agent", "antigravity", "--hooks-file", str(f)]

    assert cli.main(args + ["--apply"]) == 0
    after1 = f.read_text()
    assert power_group_text(after1) == original_group  # byte-for-byte
    data = json.loads(after1)
    assert list(data) == ["agent-power", "agent-room"]
    assert len(list(tmp_path.glob("hooks.json.bak-*"))) == 1
    assert next(tmp_path.glob("hooks.json.bak-*")).read_text() == REAL_SHAPE

    assert cli.main(args + ["--apply"]) == 0
    assert "already installed" in capsys.readouterr().out
    assert f.read_text() == after1 and power_group_text(f.read_text()) == original_group
    assert len(list(tmp_path.glob("hooks.json.bak-*"))) == 1

    assert cli.main(args + ["--uninstall"]) == 0
    assert f.read_text() == REAL_SHAPE
    assert power_group_text(f.read_text()) == original_group
    assert cli.main(args + ["--uninstall"]) == 0
    assert f.read_text() == REAL_SHAPE


@pytest.mark.parametrize("original", [
    '{"agent-power":{"enabled":true,"Stop":[{"command":"p hook-event","timeout":5}]}}',
    '{\n\t"agent-power" : {"Stop": [ ]} ,\n\t"zeta": {"enabled": false}\n}',
    '{}',
    '  {  }  \n',
])
def test_any_formatting_of_other_groups_is_kept_byte_for_byte(tmp_path, original):
    f = tmp_path / "hooks.json"
    f.write_text(original)
    args = ["capture", "install-hook", "--agent", "antigravity", "--hooks-file", str(f)]
    assert cli.main(args + ["--apply"]) == 0
    after = f.read_text()
    for key, value in json.loads(original).items():
        assert json.loads(after)[key] == value
    if '"agent-power"' in original:
        span = original[original.index('"agent-power"'):original.index("}}") + 1] if original.startswith('{"agent-power":{') else '"agent-power" : {"Stop": [ ]}'
        assert span in after
    assert cli.main(args + ["--apply"]) == 0 and f.read_text() == after
    assert cli.main(args + ["--uninstall"]) == 0
    assert f.read_text() == original


@pytest.mark.parametrize("content", ['{"agent-room": {"Stop": []}, "agent-room": {"Stop": []}}', "{not json", "[]", '"x"', '{"agent-power": {"Stop": []}, "agent-room": 7}',
                                     '{"agent-room": {"Stop": [{"type": "command", "command": "someone-else"}]}}'])
def test_apply_refuses_unexpected_files_untouched(tmp_path, content, capsys):
    f = tmp_path / "hooks.json"
    f.write_text(content)
    assert cli.main(["capture", "install-hook", "--agent", "antigravity", "--hooks-file", str(f), "--apply"]) == 2
    assert f.read_text() == content
    assert list(tmp_path.glob("hooks.json.bak-*")) == []
    assert "not changed" in capsys.readouterr().err


def test_apply_creates_missing_file(tmp_path):
    f = tmp_path / "config" / "hooks.json"
    assert cli.main(["capture", "install-hook", "--agent", "antigravity", "--hooks-file", str(f), "--apply"]) == 0
    assert list(json.loads(f.read_text())) == ["agent-room"]
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_default_hooks_path_is_gemini_config(env):
    assert capture_agy.default_hooks_path() == env / "home" / ".gemini" / "config" / "hooks.json"


def test_claude_installer_is_still_the_default(tmp_path, capsys):
    assert cli.main(["capture", "install-hook", "--print"]) == 0
    assert "SessionEnd" in json.loads(capsys.readouterr().out)["hooks"]


def test_status_reports_both_hooks(env, isolated_home, capsys):
    f = env / "home" / ".gemini" / "config" / "hooks.json"
    f.parent.mkdir(parents=True)
    f.write_text(REAL_SHAPE)
    assert cli.main(["capture", "status"]) == 0
    out = capsys.readouterr().out
    assert "Antigravity Stop hook" in out and "not installed" in out.split("Antigravity Stop hook")[1].splitlines()[0]
    cli.main(["capture", "install-hook", "--agent", "antigravity", "--apply"])
    capsys.readouterr()
    cli.main(["capture", "status"])
    line = capsys.readouterr().out.split("Antigravity Stop hook")[1].splitlines()[0]
    assert "installed" in line and "not installed" not in line


# -- room mcp --------------------------------------------------------------------------------------------

# sha256 of TOOLS[3:5] with each description and the 'agent' property removed, recorded before this change.
WORKING_TOOLS_REST_SHA = "a1f12d4155b02494b3d4179dfe3ace271d849ccbc1e6a9a0b8fdd41b42b03cd3"


def test_working_tools_change_only_in_agent_and_descriptions():
    tools = copy.deepcopy(mcp_server.TOOLS[3:5])
    assert tools[1]["inputSchema"]["properties"]["agent"]["enum"] == ["claude-code", "antigravity"]
    assert tools[1]["inputSchema"]["properties"]["agent"]["default"] == "claude-code"
    assert "agent" in tools[0]["inputSchema"]["properties"] and "agent" not in tools[0]["inputSchema"].get("required", [])
    for t in tools:
        assert "Antigravity" in t["description"]
        t.pop("description")
        t["inputSchema"]["properties"].pop("agent")
    assert hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest() == WORKING_TOOLS_REST_SHA


def _call(name, **args):
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return resp["result"]["content"][0]["text"], resp["result"].get("isError", False)


def test_mcp_session_latest_and_search_by_agent(env, enabled, isolated_home):
    capture_agy.capture_antigravity(two_turns(env).hook())
    working.upsert("claude-code", "cc-1", "cc-proj", [
        {"key": f"k{i}", "role": r, "ts": "", "text": f"Claude Code Okapi-Lantern note number {i} " * 6}
        for i, r in enumerate(["user", "assistant", "user", "assistant"])])
    text, err = _call("session_latest", agent="antigravity")
    assert not err and "agy-demo" in text and "cc-proj" not in text and "untrusted" in text.lower()
    text, err = _call("session_latest")
    assert not err and "cc-proj" in text and "agy-demo" not in text
    text, _ = _call("working_search", query="Okapi-Lantern")
    assert "agy-demo" in text and "cc-proj" in text
    text, _ = _call("working_search", query="Okapi-Lantern", agent="antigravity")
    assert "agy-demo" in text and "cc-proj" not in text
    text, _ = _call("working_search", query="Okapi-Lantern", agent="claude-code")
    assert "cc-proj" in text and "agy-demo" not in text
    text, err = _call("working_search", query="x", agent="hermes")
    assert err and "'agent' must be one of" in text
    text, err = _call("session_latest", agent="../x")
    assert err


def test_mcp_responses_stay_capped(env, enabled, isolated_home, monkeypatch):
    for i in range(5):
        capture_agy.capture_antigravity(two_turns(env, cid=f"{i}{CID[1:]}").hook())
    text, _ = _call("session_latest", agent="antigravity", n=5)
    assert len(text) <= mcp_server.MAX_RESPONSE_CHARS


def test_note_ids_are_not_redacted_in_tool_output(env, enabled, isolated_home):
    capture_agy.capture_antigravity(two_turns(env).hook())
    [note] = notes(isolated_home)
    assert note["id"].startswith("antigravity-")
    text, _ = _call("session_latest", agent="antigravity")
    assert note["id"] in text and "[REDACTED]" not in text
    text, _ = _call("working_search", query="Okapi-Lantern", agent="antigravity")
    assert note["id"] in text


def test_promote_antigravity_note_keeps_its_source(env, enabled, isolated_home, monkeypatch):
    from room.memory.store import MemoryStore
    from test_working import ScriptedHermes

    capture_agy.capture_antigravity(two_turns(env).hook())
    [note] = notes(isolated_home)
    ScriptedHermes.prompts = []
    monkeypatch.setattr(cli, "agent_factory", lambda name: ScriptedHermes())
    assert cli.main(["working", "promote", note["id"]]) == 0
    with MemoryStore() as store:
        [node] = [n for n in store.all_nodes() if n.name == "Pytest over unittest"]
        [prov] = store.provenance(node.id)
    assert prov["source"] == "antigravity" and prov["session_id"] == note["id"]
    log = (isolated_home / "sessions" / f"{note['id']}.jsonl").read_text().splitlines()
    roles = [(json.loads(line)["agent"], json.loads(line)["role"]) for line in log]
    assert roles == [("antigravity", "user"), ("gemini", "agent"), ("antigravity", "user"), ("gemini", "agent")]
    assert "one README, one changelog" not in ScriptedHermes.prompts[0]  # only the user's own words by default


def test_non_explicit_user_input_still_ends_the_turn(env, enabled, isolated_home):
    t = two_turns(env)
    first_final = "Plan: one README, one changelog"
    t._write({"type": "USER_INPUT", "source": "SYSTEM_GENERATED", "content": "<USER_REQUEST>AUTO-INPUT-TEXT</USER_REQUEST>"})
    t.reply("Reply to the automatic input, which must not replace the previous turn's final reply.")
    _, items = capture_agy.parse_transcript(t.path)
    body = json.dumps(items)
    assert first_final in body and "AUTO-INPUT-TEXT" not in body
    assert "Noted: pytest for Okapi-Lantern" in body  # the earlier final reply is kept


def test_cli_hook_exits_zero_when_stdout_is_closed(enabled, monkeypatch):
    class Closed(io.StringIO):
        def write(self, s):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "stdout", Closed())
    assert cli.main(["capture", "antigravity"]) == 0


def test_worker_payload_is_small_whatever_the_hook_sends(env, enabled, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            self.stdin = io.BytesIO()
            self.stdin.close = lambda: seen.setdefault("payload", self.stdin.getvalue())

    monkeypatch.setattr(capture_agy.subprocess, "Popen", FakePopen)
    t = two_turns(env)
    raw = json.dumps(t.hook(workspacePaths=["/w/" + "p" * 200] * 200, cwd=None))
    assert len(raw) > 32_000
    capture_agy.run_hook(raw)
    assert len(seen["payload"]) < 8192


def test_duplicate_agent_room_groups_are_refused(tmp_path, capsys):
    ours = json.dumps(capture_agy.hook_config()["agent-room"])
    content = '{"agent-room": %s, "agent-room": %s}' % (ours, ours)
    f = tmp_path / "hooks.json"
    f.write_text(content)
    assert cli.main(["capture", "install-hook", "--agent", "antigravity", "--hooks-file", str(f), "--uninstall"]) == 2
    assert "more than one" in capsys.readouterr().err and f.read_text() == content


def test_real_hook_process_exits_zero_with_closed_stdout(isolated_home):
    import subprocess
    capture.set_enabled(True)
    p = subprocess.Popen([sys.executable, "-m", "room.cli", "capture", "antigravity"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ})
    p.stdout.close()
    p.stdin.write(b"{}")
    p.stdin.close()
    assert p.wait(timeout=20) == 0, p.stderr.read()


def test_worker_payload_stays_small_with_non_ascii_paths(env, enabled, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            self.stdin = io.BytesIO()
            self.stdin.close = lambda: seen.setdefault("payload", self.stdin.getvalue())

    monkeypatch.setattr(capture_agy.subprocess, "Popen", FakePopen)
    t = two_turns(env)
    capture_agy.run_hook(json.dumps(t.hook(cwd="/ü" + "😀" * 2000, workspacePaths=["/ü" + "😀" * 2000])))
    assert len(seen["payload"]) <= 16384
    assert json.loads(seen["payload"])["conversationId"] == CID


def _disabled_group_text():
    group = {"enabled": False, **capture_agy.hook_config()["agent-room"]}
    return REAL_SHAPE.rstrip()[:-1].rstrip() + ',\n  "agent-room": ' + json.dumps(group, indent=2).replace("\n", "\n  ") + "\n}\n"


def test_disabled_group_is_reported_and_never_silently_enabled(env, capsys):
    f = env / "home" / ".gemini" / "config" / "hooks.json"
    f.parent.mkdir(parents=True)
    content = _disabled_group_text()
    f.write_text(content)
    assert cli.main(["capture", "status"]) == 0
    line = capsys.readouterr().out.split("Antigravity Stop hook")[1].splitlines()[0]
    assert line.endswith(": installed but disabled")
    assert cli.main(["capture", "install-hook", "--agent", "antigravity", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "disabled" in out and "nothing changed" in out
    assert f.read_text() == content and list(f.parent.glob("hooks.json.bak-*")) == []
    assert cli.main(["capture", "install-hook", "--agent", "antigravity", "--uninstall"]) == 0
    assert f.read_text() == REAL_SHAPE
