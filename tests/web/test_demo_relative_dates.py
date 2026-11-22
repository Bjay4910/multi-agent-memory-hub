"""Rich demo with relative_dates=True: every timestamp shifted by one offset so the newest session is 2 days
before "now". Opt-in; the default keeps the fixed Aug-Oct 2026 timestamps exactly."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from room.memory.readonly import ReadOnlyMemory

NOW = datetime(2026, 11, 22, 15, 30, tzinfo=timezone.utc)
FIXED_SESSION_TS = {
    "room-2026-08-03-a1": "2026-08-03T09:12:00Z", "room-2026-08-10-b2": "2026-08-10T18:40:00Z",
    "room-2026-08-18-c3": "2026-08-18T20:05:00Z", "room-2026-08-27-d4": "2026-08-27T11:30:00Z",
    "room-2026-09-04-e5": "2026-09-04T16:15:00Z", "room-2026-09-12-f6": "2026-09-12T10:45:00Z",
    "room-2026-09-21-g7": "2026-09-21T14:20:00Z", "room-2026-09-29-h8": "2026-09-29T19:55:00Z",
    "room-2026-10-06-i9": "2026-10-06T08:50:00Z", "room-2026-10-14-j10": "2026-10-14T13:05:00Z",
    "room-2026-10-22-k11": "2026-10-22T17:35:00Z", "room-2026-10-29-l12": "2026-10-29T09:10:00Z",
}


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _load(tmp_path, **kw):
    from room.web.demo_data import load_rich_demo

    load_rich_demo(tmp_path, **kw).close()
    return ReadOnlyMemory(tmp_path / "memory.db")


def _session_ts(store):
    rows = store.conn.execute("SELECT session_id, MAX(ts) FROM provenance GROUP BY session_id").fetchall()
    return {r[0]: r[1] for r in rows}


def test_default_timestamps_are_the_fixed_ones(tmp_path):
    with _load(tmp_path) as store:
        assert _session_ts(store) == FIXED_SESSION_TS
        stamps = {n.created_at for n in store.all_nodes(True)} | {n.updated_at for n in store.all_nodes(True)}
        assert stamps <= set(FIXED_SESSION_TS.values())
        assert any("4 August" in n.summary for n in store.all_nodes(True))


def test_relative_dates_put_the_newest_session_two_days_before_now(tmp_path):
    with _load(tmp_path, relative_dates=True, now=NOW) as store:
        sessions = _session_ts(store)
        assert set(sessions) == set(FIXED_SESSION_TS)  # ids unchanged
        newest = max(_ts(t) for t in sessions.values())
        oldest = min(_ts(t) for t in sessions.values())
        assert abs(newest - (NOW - timedelta(days=2))) <= timedelta(hours=1)
        assert timedelta(days=85) <= NOW - oldest <= timedelta(days=95)


def test_relative_dates_keep_one_constant_offset_and_the_same_order(tmp_path):
    with _load(tmp_path, relative_dates=True, now=NOW) as store:
        shifted = _session_ts(store)
    offsets = {_ts(shifted[s]) - _ts(t) for s, t in FIXED_SESSION_TS.items()}
    assert len(offsets) == 1
    assert sorted(shifted, key=shifted.get) == sorted(FIXED_SESSION_TS, key=FIXED_SESSION_TS.get)


def test_relative_dates_keep_every_count(tmp_path):
    with _load(tmp_path, relative_dates=True, now=NOW) as store:
        s = store.stats()
        all_ts = [r[0] for r in store.conn.execute("SELECT ts FROM provenance")]
        edge_ts = [r[0] for r in store.conn.execute("SELECT created_at FROM edges")]
    assert (s["nodes_active"] + s["nodes_superseded"], s["edges"], s["nodes_superseded"], s["sessions"]) == (117, 214, 14, 12)
    assert all(_ts(t) <= NOW for t in all_ts + edge_ts)
    assert not set(all_ts) & set(FIXED_SESSION_TS.values())  # nothing left on the fixed dates


def test_relative_dates_move_dates_written_in_summaries_by_the_same_offset(tmp_path):
    with _load(tmp_path, relative_dates=True, now=NOW) as store:
        summaries = [n.summary for n in store.all_nodes(True)]
    offset = (NOW - timedelta(days=2)) - _ts(FIXED_SESSION_TS["room-2026-10-29-l12"])
    expected = (datetime(2026, 10, 28) + offset).strftime("%-d %B")
    assert any(f"Submitted on {expected};" in s for s in summaries), expected
    assert not any("Submitted on 28 October" in s for s in summaries)


def test_make_server_default_keeps_fixed_dates_and_opt_in_shifts_them():
    import json
    import threading
    import urllib.request

    from room.web import api

    def newest(server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/sessions?limit=1") as r:
                return json.load(r)["sessions"][0]["last_ts"]
        finally:
            server.shutdown()
            server.server_close()

    assert newest(api.make_server("127.0.0.1", 0, mode="demo", demo_set="rich")) == "2026-10-29T09:10:00Z"
    shifted = _ts(newest(api.make_server("127.0.0.1", 0, mode="demo", demo_set="rich", relative_dates=True)))
    assert abs(shifted - (datetime.now(timezone.utc) - timedelta(days=2))) <= timedelta(hours=1)


def test_serve_demo_turns_relative_dates_on(monkeypatch):
    from room.web import api

    seen = {}
    real_home = api.demo_home

    def spy_home(demo_set="small", **kw):
        seen.update(kw, demo_set=demo_set)
        return real_home(demo_set, **kw)

    def stop(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(api, "demo_home", spy_home)
    monkeypatch.setattr(api, "make_server", stop)
    with pytest.raises(KeyboardInterrupt):
        api.serve(port=0, demo=True)
    assert seen == {"relative_dates": True, "demo_set": "rich"}
