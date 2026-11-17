import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from room import presence
from room.presence import PresenceEntry, read_presence, working, write_presence


def test_write_presence_fields(isolated_home):
    path = write_presence("hermes", "working", "x" * 500)
    data = json.loads(path.read_text())
    assert path == isolated_home / "presence" / "hermes.json"
    assert set(data) == {"agent", "status", "task", "pid", "updated_at"}
    assert data["agent"] == "hermes" and data["status"] == "working"
    assert len(data["task"]) == presence.TASK_MAX_CHARS
    assert data["pid"] == os.getpid()
    assert data["updated_at"].endswith("Z")
    datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))


def test_write_is_atomic_rename(monkeypatch, isolated_home):
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((os.path.basename(src), os.path.basename(dst)))
        # the target must not exist in a half-written state before rename
        assert json.loads(open(src).read())["agent"] == "gemini"
        real_replace(src, dst)

    monkeypatch.setattr(presence.os, "replace", spy)
    write_presence("gemini", "idle")
    assert len(replaced) == 1
    tmp_name, target = replaced[0]
    assert target == "gemini.json" and tmp_name.startswith(".gemini.") and tmp_name.endswith(".tmp")
    # no temp files are left behind
    assert sorted(p.name for p in (isolated_home / "presence").iterdir()) == ["gemini.json"]


def test_failed_write_leaves_no_temp_file(monkeypatch, isolated_home):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(presence.os, "replace", boom)
    with pytest.raises(OSError):
        write_presence("muse", "working", "t")
    assert list((isolated_home / "presence").iterdir()) == []


def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        write_presence("muse", "busy")


def test_working_context_sets_working_then_idle():
    async def scenario():
        async with working("claude", "session s: round 1/2", heartbeat_interval=60):
            during = read_presence()
        return during, read_presence()

    during, after = asyncio.run(scenario())
    assert [(e.agent, e.status, e.task) for e in during] == [("claude", "working", "session s: round 1/2")]
    assert [(e.agent, e.status, e.task) for e in after] == [("claude", "idle", "")]


def test_working_context_sets_idle_on_error():
    async def scenario():
        async with working("hermes", "t", heartbeat_interval=60):
            raise RuntimeError("model crashed")

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())
    assert read_presence()[0].status == "idle"


def test_heartbeat_refreshes_updated_at(monkeypatch):
    writes = []
    real_write = presence.write_presence

    def spy(agent, status, task="", directory=None):
        writes.append(status)
        return real_write(agent, status, task, directory)

    monkeypatch.setattr(presence, "write_presence", spy)

    async def scenario():
        async with working("gemini", "t", heartbeat_interval=0.02):
            await asyncio.sleep(0.15)

    asyncio.run(scenario())
    assert writes[0] == "working" and writes[-1] == "idle"
    assert writes.count("working") >= 3  # initial write + heartbeats


def _entry(status, age_s):
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return PresenceEntry("a", status, "t", 1, now - timedelta(seconds=age_s)), now


def test_fresh_working_entry_is_working():
    e, now = _entry("working", 119)
    assert not e.is_stale(now) and e.effective_status(now) == "working"


def test_working_entry_older_than_120s_is_stale_idle():
    e, now = _entry("working", 121)
    assert e.is_stale(now) and e.effective_status(now) == "idle"


def test_idle_entry_is_never_stale():
    e, now = _entry("idle", 10_000)
    assert not e.is_stale(now) and e.effective_status(now) == "idle"


def test_read_presence_skips_garbage(isolated_home):
    write_presence("hermes", "idle")
    d = isolated_home / "presence"
    (d / "broken.json").write_text("{not json")
    (d / ".hermes.abc.tmp").write_text("partial")
    assert [e.agent for e in read_presence()] == ["hermes"]


def test_read_presence_missing_dir():
    assert read_presence() == []
