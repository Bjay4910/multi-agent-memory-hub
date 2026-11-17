import pytest

from room.agents.base import Agent, AgentError


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.agent-room."""
    monkeypatch.setenv("AGENT_ROOM_HOME", str(tmp_path / "agent-room"))
    return tmp_path / "agent-room"


class FakeAgent(Agent):
    """Records every call; optionally fails replies or healthchecks."""

    def __init__(self, name, healthy=True, fail_rounds=(), fail_all=False, calls=None):
        self.name = name
        self.healthy = healthy
        self.fail_rounds = set(fail_rounds)
        self.fail_all = fail_all
        self.calls = calls if calls is not None else []
        self._n = 0

    async def reply(self, task, transcript):
        self._n += 1
        self.calls.append((self.name, task, [(m.agent, m.role, m.content) for m in transcript]))
        if self.fail_all or self._n in self.fail_rounds:
            raise AgentError(f"{self.name}: boom")
        return f"{self.name} reply {self._n}"

    async def healthcheck(self):
        return (self.healthy, "ok" if self.healthy else f"{self.name} is down")
