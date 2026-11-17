import json

import pytest

from room.agents.base import Agent, AgentError
from room.memory.store import MemoryStore, Provenance


class ScriptedExtractor(Agent):
    """Returns queued raw outputs from complete(); records every prompt it got."""

    def __init__(self, name, outputs):
        self.name = name
        self.outputs = list(outputs)
        self.prompts = []

    async def complete(self, system, prompt, *, json_output=False, json_schema=None):
        self.prompts.append((system, prompt, json_output))
        self.schemas = getattr(self, "schemas", []) + [json_schema]
        if not self.outputs:
            raise AgentError(f"{self.name}: no more scripted outputs")
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out)

    async def healthcheck(self):
        return True, "scripted"


@pytest.fixture
def factory():
    """Build a fake agent factory: factory({"hermes": [...outputs], "claude": [...]})."""
    def make(script):
        created = {}

        def build(name):
            if name not in script:
                raise AgentError(f"{name}: not scripted")
            created.setdefault(name, ScriptedExtractor(name, script[name]))
            return created[name]
        build.created = created
        return build

    return make


@pytest.fixture
def mem(isolated_home):
    s = MemoryStore()
    yield s
    s.close()


def add(store, nodes, edges=(), session="s", ts=None):
    return store.apply_extraction({"nodes": list(nodes), "edges": list(edges)}, Provenance(session, "test"), ts=ts)
