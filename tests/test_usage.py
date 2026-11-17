import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from conftest import FakeAgent
from room.agents import AGENT_NAMES
from room.agents.base import AgentError
from room.agents.muse import DEFAULT_DAILY_TOKEN_LIMIT, MuseAgent, parse_limit
from room.orchestrator import Room
from room.transcript import ROLE_AGENT, ROLE_NOTE
from room.usage import DailyBudget, UsageTracker, utc_today


def test_tracker_adds_per_day(isolated_home):
    t = UsageTracker("muse")
    assert t.used() == 0
    assert t.add(100) == 100
    assert t.add(50) == 150
    t.add(999, day="2000-01-01")
    assert t.used() == 150 and t.used("2000-01-01") == 999
    data = json.loads((isolated_home / "usage" / "muse.json").read_text())
    assert data["agent"] == "muse"
    assert data["days"] == {utc_today(): 150, "2000-01-01": 999}
    assert data["updated_at"].endswith("Z")


def test_tracker_ignores_negative_and_corrupt_file(isolated_home):
    t = UsageTracker("muse")
    (isolated_home / "usage").mkdir(parents=True)
    t.path.write_text("{not json")
    assert t.used() == 0
    assert t.add(-5) == 0
    assert t.add(7) == 7


def test_tracker_concurrent_adds_are_not_lost():
    t = UsageTracker("muse")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: t.add(1), range(64)))
    assert t.used() == 64


def test_budget_reason_only_at_or_over_limit():
    t = UsageTracker("muse")
    b = DailyBudget(t, limit=100)
    t.add(99)
    assert b.exceeded_reason() is None
    t.add(1)
    reason = b.exceeded_reason()
    assert "daily token limit reached" in reason and "100/100" in reason


def test_yesterdays_usage_does_not_count():
    t = UsageTracker("muse")
    t.add(10_000, day="2000-01-01")
    assert DailyBudget(t, limit=100).exceeded_reason() is None


@pytest.mark.parametrize("raw,expected", [(None, DEFAULT_DAILY_TOKEN_LIMIT), ("", DEFAULT_DAILY_TOKEN_LIMIT), ("5000", 5000), ("1_000", 1000), ("0", 0)])
def test_parse_limit(raw, expected):
    assert parse_limit(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5"])
def test_parse_limit_invalid(raw):
    with pytest.raises(ValueError):
        parse_limit(raw)


def test_default_limit_is_200k(monkeypatch):
    monkeypatch.delenv("MUSE_DAILY_TOKEN_LIMIT", raising=False)
    assert MuseAgent().budget.limit == 200_000


def test_invalid_limit_fails_closed(monkeypatch):
    monkeypatch.setenv("MUSE_DAILY_TOKEN_LIMIT", "lots")
    agent = MuseAgent()
    assert "invalid MUSE_DAILY_TOKEN_LIMIT" in agent.skip_reason()
    ok, _ = asyncio.run(agent.healthcheck())
    assert not ok


def test_muse_over_budget_raises_without_network(monkeypatch):
    monkeypatch.setenv("MUSE_API_KEY", "fake-key-for-tests")
    monkeypatch.setenv("MUSE_BASE_URL", "http://127.0.0.1:9/v1")  # would fail if contacted
    monkeypatch.setenv("MUSE_DAILY_TOKEN_LIMIT", "10")
    UsageTracker("muse").add(10)
    agent = MuseAgent()
    with pytest.raises(AgentError, match="daily token limit reached"):
        asyncio.run(agent.reply("q", []))


def test_muse_records_reported_usage(monkeypatch):
    monkeypatch.setenv("MUSE_DAILY_TOKEN_LIMIT", "1000")
    agent = MuseAgent()
    agent._record_usage(SimpleNamespace(usage=SimpleNamespace(total_tokens=321)), 0, "")
    assert UsageTracker("muse").used() == 321


def test_muse_estimates_usage_when_not_reported():
    agent = MuseAgent()
    agent._record_usage(SimpleNamespace(usage=None), prompt_chars=396, text="x" * 4)
    assert UsageTracker("muse").used() == 101  # (396 + 4) // 4 + 1


def test_muse_is_in_default_agents():
    assert "muse" in AGENT_NAMES


class BudgetedFake(FakeAgent):
    def __init__(self, name, reason, **kw):
        super().__init__(name, **kw)
        self.reason = reason

    def skip_reason(self):
        return self.reason


def test_orchestrator_notes_budget_and_skips_call(isolated_home):
    a = FakeAgent("a")
    muse = BudgetedFake("muse", "muse: daily token limit reached (200000/200000 tokens used today, UTC)")
    result = asyncio.run(Room([muse, a], synthesizer=a, rounds=2).run("q", session_id="s"))
    assert muse.calls == []
    assert [m.agent for m in result.transcript if m.role == ROLE_AGENT] == ["a", "a"]
    notes = [m for m in result.transcript if m.role == ROLE_NOTE]
    assert [n.round for n in notes] == [1, 2]
    assert all("muse skipped this round: muse: daily token limit reached" in n.content for n in notes)
    # no presence file: the agent never started working
    assert not (isolated_home / "presence" / "muse.json").exists()


def test_budgeted_synthesizer_falls_back():
    a = FakeAgent("a")
    synth = BudgetedFake("muse", "muse: daily token limit reached")
    result = asyncio.run(Room([a], synthesizer=synth, rounds=1).run("q"))
    assert synth.calls == [] and result.final.agent == "a"
