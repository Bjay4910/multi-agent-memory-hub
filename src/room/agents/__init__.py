"""Agent adapters and a small registry to build them by name."""

from __future__ import annotations

import os

from .base import Agent, AgentError

# Every agent the room knows about, in default speaking order.
AGENT_NAMES = ("claude", "gemini", "hermes", "muse", "gemini-api")
# Used when `room ask` gets no --agents. Override with ROOM_DEFAULT_AGENTS.
BUILTIN_DEFAULT_AGENTS = ("claude", "gemini")
DEFAULT_AGENTS_ENV = "ROOM_DEFAULT_AGENTS"


def parse_names(value: str) -> list[str]:
    """Parse a comma-separated agent list: lowercase, deduplicated, validated."""
    names = [n.strip().lower() for n in value.split(",") if n.strip()]
    if not names:
        raise ValueError("agent list is empty")
    unknown = [n for n in names if n not in AGENT_NAMES]
    if unknown:
        raise ValueError(f"unknown agent(s): {', '.join(unknown)} (choose from {', '.join(AGENT_NAMES)})")
    return list(dict.fromkeys(names))


def default_agents() -> list[str]:
    """The default agent list: ROOM_DEFAULT_AGENTS if set, else claude,gemini."""
    raw = os.environ.get(DEFAULT_AGENTS_ENV, "").strip()
    if not raw:
        return list(BUILTIN_DEFAULT_AGENTS)
    try:
        return parse_names(raw)
    except ValueError as exc:
        raise ValueError(f"invalid {DEFAULT_AGENTS_ENV}: {exc}") from None


def build_agent(name: str) -> Agent:
    """Instantiate an adapter by name. Construction never touches the network."""
    if name == "claude":
        from .claude_cli import ClaudeCLIAgent

        return ClaudeCLIAgent()
    if name == "gemini":
        from .gemini_agy import AntigravityGeminiAgent

        return AntigravityGeminiAgent()
    if name == "hermes":
        from .hermes import HermesAgent

        return HermesAgent()
    if name == "muse":
        from .muse import MuseAgent

        return MuseAgent()
    if name == "gemini-api":
        from .gemini_api import GeminiAPIAgent

        return GeminiAPIAgent()
    raise ValueError(f"unknown agent '{name}' (choose from {', '.join(AGENT_NAMES)})")


__all__ = ["AGENT_NAMES", "BUILTIN_DEFAULT_AGENTS", "Agent", "AgentError", "build_agent", "default_agents", "parse_names"]
