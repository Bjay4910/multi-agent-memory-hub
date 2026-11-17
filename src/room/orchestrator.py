"""Run a discussion room: N rounds of replies, then one synthesized answer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import presence
from .agents.base import Agent, redact
from .transcript import (
    ROLE_AGENT,
    ROLE_FINAL,
    ROLE_MEMORY,
    ROLE_NOTE,
    ROLE_USER,
    Message,
    SessionLog,
    new_session_id,
)

DEFAULT_ROUNDS = 2

SYNTHESIS_TASK = (
    "The discussion is over. Write the single best final answer to the original task "
    "below, combining the strongest points from the transcript and fixing any mistakes. "
    "Do not mention the other agents or the discussion; just give the answer.\n\n"
    "Original task:\n{task}"
)

OnMessage = Callable[[Message], None]


@dataclass
class RoomResult:
    session_id: str
    log_path: Path
    transcript: list[Message] = field(default_factory=list)
    final: Message | None = None


class Room:
    def __init__(
        self,
        agents: list[Agent],
        synthesizer: Agent,
        rounds: int = DEFAULT_ROUNDS,
        on_message: OnMessage | None = None,
        sessions_dir: Path | None = None,
        presence_dir: Path | None = None,
        heartbeat_interval: float = presence.HEARTBEAT_INTERVAL_S,
        check_health: bool = True,
        memory_block: str | None = None,
    ) -> None:
        if rounds < 1:
            raise ValueError("rounds must be >= 1")
        if not agents:
            raise ValueError("at least one agent is required")
        self.agents = agents
        self.synthesizer = synthesizer
        self.rounds = rounds
        self.on_message = on_message
        self.sessions_dir = sessions_dir
        self.presence_dir = presence_dir
        self.heartbeat_interval = heartbeat_interval
        self.check_health = check_health
        self.memory_block = memory_block or None

    async def run(self, task: str, session_id: str | None = None) -> RoomResult:
        session_id = session_id or new_session_id()
        log = SessionLog(session_id, self.sessions_dir)
        result = RoomResult(session_id=session_id, log_path=log.path)

        def record(agent: str, round_: int, role: str, content: str) -> Message:
            msg = Message(session_id=session_id, agent=agent, round=round_, role=role, content=content)
            result.transcript.append(msg)
            log.append(msg)
            if self.on_message:
                self.on_message(msg)
            return msg

        record("user", 0, ROLE_USER, task)

        # Recalled memory is shown to every agent that is actually called, at the
        # start of its prompt. It is not persisted, so it is never re-extracted.
        memory = [Message(session_id, "memory", 0, ROLE_MEMORY, self.memory_block)] if self.memory_block else []

        def view() -> list[Message]:
            return memory + list(result.transcript)

        active = await self._reachable(self.agents, lambda note: record("room", 0, ROLE_NOTE, note))

        for round_ in range(1, self.rounds + 1):
            for agent in active:
                label = f"session {session_id}: round {round_}/{self.rounds}"
                text, error = await self._call(agent, task, view(), label)
                if error:
                    record("room", round_, ROLE_NOTE, f"{agent.name} skipped this round: {error}")
                else:
                    record(agent.name, round_, ROLE_AGENT, text)

        result.final = await self._synthesize(task, result, active, record, view)
        return result

    async def _reachable(self, agents: list[Agent], note: Callable[[str], None]) -> list[Agent]:
        if not self.check_health:
            return list(agents)
        checks = await asyncio.gather(*(self._safe_health(a) for a in agents))
        active = []
        for agent, (ok, detail) in zip(agents, checks):
            if ok:
                active.append(agent)
            else:
                note(f"{agent.name} unreachable, skipped: {detail}")
        return active

    @staticmethod
    async def _safe_health(agent: Agent) -> tuple[bool, str]:
        try:
            return await agent.healthcheck()
        except Exception as exc:  # noqa: BLE001 - a broken adapter must not crash the room
            return False, redact(f"{type(exc).__name__}: {exc}")

    async def _call(self, agent: Agent, task: str, transcript: list[Message], label: str) -> tuple[str, str | None]:
        """Run one reply with presence tracking. Returns (text, error)."""
        try:
            reason = agent.skip_reason()
        except Exception as exc:  # noqa: BLE001
            reason = redact(f"{agent.name}: {type(exc).__name__}: {exc}")
        if reason:
            return "", redact(reason)
        try:
            async with presence.working(agent.name, label, self.presence_dir, self.heartbeat_interval):
                return await agent.reply(task, transcript), None
        except Exception as exc:  # noqa: BLE001
            return "", redact(str(exc)) or type(exc).__name__

    async def _synthesize(self, task, result: RoomResult, active: list[Agent], record, view) -> Message | None:
        # Prefer the configured synthesizer; fall back to agents that replied.
        replied = {m.agent for m in result.transcript if m.role == ROLE_AGENT}
        candidates = [self.synthesizer] + [a for a in active if a.name in replied and a is not self.synthesizer]
        if self.synthesizer not in active and self.check_health:
            ok, detail = await self._safe_health(self.synthesizer)
            if not ok:
                record("room", self.rounds + 1, ROLE_NOTE, f"synthesizer {self.synthesizer.name} unreachable: {detail}")
                candidates = candidates[1:]
        synth_task = SYNTHESIS_TASK.format(task=task)
        for agent in candidates:
            label = f"session {result.session_id}: synthesis"
            text, error = await self._call(agent, synth_task, view(), label)
            if not error:
                return record(agent.name, self.rounds + 1, ROLE_FINAL, text)
            record("room", self.rounds + 1, ROLE_NOTE, f"synthesis by {agent.name} failed: {error}")
        record("room", self.rounds + 1, ROLE_NOTE, "no final answer: every synthesizer failed")
        return None
