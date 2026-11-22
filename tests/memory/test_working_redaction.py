"""Gap 3: working notes get the extraction filter's strong masking before anything is written.

Every input is captured three ways (a Claude Code transcript through capture_claude, an Antigravity transcript
through capture_antigravity, and a direct working.upsert), then read back through `room working list/show/search`
and the MCP working_search / session_latest tools. The secret, and every word of it, must appear in no output and
in no byte of any file under the room home (working notes, temp files, the lock file, the capture log).
Fake values only. Temp homes only.
"""

import contextlib
import io
import json
import os
import re
from pathlib import Path

import pytest

from memory.test_extraction_stage12 import C_FALSE_POSITIVES
from room import capture, capture_agy, cli, mcp_server
from room.memory import working

PAD_USER = ("Planning the Okapi-Lantern release notes today: the order of the steps, the owners, the open questions "
            "and the review dates for each part of the launch.")
PAD_USER_2 = ("Second message about the Okapi-Lantern plan, covering the rollback steps, who signs off on each part, "
              "and a short list of the risks we already know about and how we plan to watch them. It also lists "
              "the people to tell before the release, the checks to run after it, and the notes to write up later.")

# (a) everyday phrasings: (text, secret value)
EVERYDAY = [
    ("the password is swordfish", "swordfish"),
    ("pw = swordfish", "swordfish"),
    ("password: swordfish", "swordfish"),
    ("my token for Okra Deploy is gx7Lq2pZ9w", "gx7Lq2pZ9w"),
    ("My wifi password is bluebell42", "bluebell42"),
    ("The database password for staging is tidepool73", "tidepool73"),
    ("api_key = ak5Fz8Qw2Lm9", "ak5Fz8Qw2Lm9"),
    ("Here is the secret: ochre-falcon-58", "ochre-falcon-58"),
    ("my passphrase is quiet amber river", "quiet amber river"),
    ("The PIN is 4821", "4821"),
    ("Connect with password marlin62 to the staging box.", "marlin62"),
    ("The service connects to postgres://ops:kestrel64@db.example.com:5432/app at boot.", "kestrel64"),
    ("export GITHUB_TOKEN=ghxfake731", "ghxfake731"),
    ("I changed the password to heron58.", "heron58"),
]

# Text that must reach the note unchanged: the stage 12 false positives plus ordinary uses of the words.
HARMLESS = list(C_FALSE_POSITIVES) + [
    "Reset your password from the settings page.",
    "The password manager was updated last week.",
    "Press the space key to pause the video.",
    "Press any keyboard key to continue.",
    "Grandma's secret sauce has paprika in it.",
    "The token limit for this model is generous.",
    "Use a password manager for every account.",
    "Keep the API key out of the repository.",
    "The meeting covered password policy changes.",
    "Session tokens expire when you log out.",
    "Turn the key and the engine starts.",
    "Lost my house key on the train home.",
    "She kept the surprise party a secret.",
    "Never share your password with anyone.",
    "Rotate the signing key every quarter.",
]


def words_in(value, text):
    """The secret, or a word of it (3+ characters), standing alone in ``text`` (the stage 9+ leak check)."""
    found = [value] if value in text else []
    found += [w for w in re.split(r"[\s:/,;()<>\"\\]+", value)
              if len(w) >= 3 and re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text)]
    return found


class Env:
    """Temp AGENT_ROOM_HOME, CLAUDE_CONFIG_DIR, ANTIGRAVITY_CLI_HOME and HOME, with capture enabled."""

    def __init__(self, root: Path, monkeypatch):
        self.root, self.mp = root, monkeypatch
        self.home = root / "agent-room"
        monkeypatch.setenv("AGENT_ROOM_HOME", str(self.home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "claude-config"))
        monkeypatch.setenv("ANTIGRAVITY_CLI_HOME", str(root / "agy-cli"))
        monkeypatch.setenv("HOME", str(root / "home"))
        (root / "home").mkdir(parents=True, exist_ok=True)
        monkeypatch.delenv("ROOM_SPAWNED", raising=False)
        monkeypatch.setattr(capture_agy, "_ancestor_args", lambda: ["agy", "/bin/zsh"])
        capture.set_enabled(True)

    def claude(self, text, sid="1111aaaa-2222-bbbb-3333-cccc4444dddd", cwd="/Users/someone/Developer/okapi-lantern"):
        path = self.root / "claude-config" / "projects" / "-Users-someone-Developer-okapi-lantern" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            {"type": "user", "message": {"role": "user", "content": f"{text}\n{PAD_USER}"}, "uuid": f"{sid}-u1",
             "sessionId": sid, "cwd": cwd, "entrypoint": "cli", "timestamp": "2026-11-20T06:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": f"Noted: {text}"}]},
             "uuid": f"{sid}-a1", "sessionId": sid, "cwd": cwd, "entrypoint": "cli", "timestamp": "2026-11-20T06:00:05Z"},
            {"type": "user", "message": {"role": "user", "content": PAD_USER_2}, "uuid": f"{sid}-u2",
             "sessionId": sid, "cwd": cwd, "entrypoint": "cli", "timestamp": "2026-11-20T06:01:00Z"},
        ]
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return capture.capture_claude({"session_id": sid, "transcript_path": str(path), "cwd": cwd})

    def agy(self, text, cid="6879392a-cea0-4fc6-9c37-4b81cc475186", cwd="/Users/someone/Developer/okapi-lantern"):
        path = self.root / "agy-cli" / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        steps = [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT",
             "content": f"<USER_REQUEST>\n{text}\n{PAD_USER}\n</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "model": "m", "content": f"Noted: {text}", "thinking": "t"},
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": f"<USER_REQUEST>\n{PAD_USER_2}\n</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "model": "m", "content": "Plan noted for Okapi-Lantern.",
             "thinking": "t"},
        ]
        for i, s in enumerate(steps):
            s.update(step_index=i, created_at=f"2026-11-20T06:{i:02d}:00Z", status="DONE")
        path.write_text("".join(json.dumps(x) + "\n" for x in steps))
        return capture_agy.capture_antigravity({
            "conversationId": cid, "transcriptPath": str(path), "cwd": cwd, "workspacePaths": [cwd],
            "hookEventName": "Stop", "fullyIdle": True, "terminationReason": "model_stop", "lastUserInput": "x",
            "modelName": "auto", "artifactDirectoryPath": "/x"})

    def direct(self, text, key="direct-session"):
        items = [{"key": "u1", "role": "user", "ts": "2026-11-20T06:00:00Z", "text": f"{text}\n{PAD_USER}"},
                 {"key": "a1", "role": "assistant", "ts": "2026-11-20T06:00:05Z", "text": f"Noted: {text}"},
                 {"key": "u2", "role": "user", "ts": "2026-11-20T06:01:00Z", "text": PAD_USER_2}]
        status, _ = working.upsert("claude-code", key, "okapi-lantern", items)
        return status

    def outputs(self, queries):
        """Everything a person or agent can read back: CLI list/show/search and MCP working_search/session_latest."""
        texts = []
        for argv in [["working", "list"]] + [["working", "show", n["id"]] for n in working.list_notes()] + \
                [["working", "search", *q.split()] for q in queries]:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                cli.main(argv)
            texts.append(buf.getvalue())
        for q in queries:
            texts.append(mcp_server.call_tool("working_search", {"query": q})[0])
        for agent in ("claude-code", "antigravity"):
            texts.append(mcp_server.call_tool("session_latest", {"agent": agent, "n": 3})[0])
        return "\n".join(texts)

    def all_bytes(self):
        out = []
        for dirpath, _, names in os.walk(self.home):
            for n in names:
                out.append(Path(dirpath, n).read_bytes().decode("utf-8", "replace"))
        return "\n".join(out)


def capture_three_ways(env, text):
    return env.claude(text), env.agy(text), env.direct(text)


def leaks_for(env, text, value):
    statuses = capture_three_ways(env, text)
    readable = env.outputs(["Okapi-Lantern", value, *value.split()[:2]])
    stored = env.all_bytes()
    found = {f"output:{w}" for w in words_in(value, readable)} | {f"file:{w}" for w in words_in(value, stored)}
    return statuses, sorted(found)


@pytest.mark.parametrize("text, value", EVERYDAY, ids=[t[:28] for t, _ in EVERYDAY])
def test_everyday_secret_never_reaches_a_note_or_an_output(text, value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    statuses, leaks = leaks_for(env, text, value)
    assert statuses == ("created", "created", "created"), statuses
    assert len(working.list_notes()) == 3
    assert leaks == []
    assert "[REDACTED]" in env.all_bytes()


@pytest.mark.parametrize("sentence", HARMLESS)
def test_harmless_sentences_reach_the_note_unchanged(sentence, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert capture_three_ways(env, sentence) == ("created", "created", "created")
    notes = working.list_notes()
    assert len(notes) == 3
    for note in notes:
        texts = [t["text"] for t in note["turns"]]
        assert any(sentence in t for t in texts), (note["source"], texts)
        assert "[REDACTED]" not in "\n".join(texts)


@pytest.mark.xfail(strict=True, reason="dropped by design: see docs (a plain-word secret repeated bare is not scrubbed)")
def test_a_secret_repeated_bare_in_a_later_turn_is_scrubbed_too(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    items = [{"key": "u1", "role": "user", "ts": "t", "text": f"the password is swordfish\n{PAD_USER}"},
             {"key": "a1", "role": "assistant", "ts": "t", "text": "I will not repeat swordfish anywhere."},
             {"key": "u2", "role": "user", "ts": "t", "text": PAD_USER_2}]
    assert working.upsert("claude-code", "k", "okapi-lantern", items)[0] == "created"
    assert "swordfish" not in env.all_bytes()


def test_project_tag_is_masked_in_the_note_and_the_capture_log(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    cwd = "/Users/someone/Developer/password is swordfish"
    assert env.claude("A plain planning message.", cwd=cwd) == "created"
    # the detached worker logs the project tag to capture.log
    sid = "9999aaaa-2222-bbbb-3333-cccc4444dddd"
    env.claude("A plain planning message.", sid=sid, cwd=cwd)  # write the transcript
    working.purge(everything=True)
    path = next((tmp_path / "claude-config" / "projects").rglob(f"{sid}.jsonl"))
    assert capture.run_worker(json.dumps({"session_id": sid, "transcript_path": str(path), "cwd": cwd})) == 0
    log = (env.home / "capture" / "capture.log").read_text()
    assert "created" in log
    assert all("swordfish" not in n["project"] for n in working.list_notes())
    assert "swordfish" not in env.all_bytes()


@pytest.mark.xfail(strict=True, reason="review 4: working.py masks through its own _mask_credentials (flag values such as ON are kept), so patching quality.mask_credentials no longer simulates a miss; the same guarantee is tested in test_working_redaction_review4.py::test_refusal_rule_still_uses_the_stronger_check")
def test_the_refusal_rule_uses_the_stronger_check(tmp_path, monkeypatch):
    """If masking ever misses a credential the extraction filter still sees, nothing is written."""
    from room.memory import quality

    env = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(quality, "mask_credentials", lambda text: text)  # simulate the masking missing it
    assert env.direct("the password is swordfish") == "refused-secret"
    assert working.list_notes() == []
    assert "swordfish" not in env.all_bytes()


def test_redact_for_note_is_the_one_function(tmp_path, monkeypatch):
    Env(tmp_path, monkeypatch)
    assert working.redact_for_note("the password is swordfish") == "the password is [REDACTED]"
    assert working.redact_for_note("Reset your password from the settings page.") == \
        "Reset your password from the settings page."
