"""Agent adapters and a small registry to build them by name."""

from __future__ import annotations

from .base import Agent, AgentError

AGENT_NAMES = ("hermes", "gemini", "muse", "claude")


def build_agent(name: str) -> Agent:
    """Instantiate an adapter by name. Construction never touches the network."""
    if name == "hermes":
        from .hermes import HermesAgent

        return HermesAgent()
    if name == "gemini":
        from .gemini import GeminiAgent

        return GeminiAgent()
    if name == "muse":
        from .muse import MuseAgent

        return MuseAgent()
    if name == "claude":
        from .claude_cli import ClaudeCLIAgent

        return ClaudeCLIAgent()
    raise ValueError(f"unknown agent '{name}' (choose from {', '.join(AGENT_NAMES)})")


__all__ = ["AGENT_NAMES", "Agent", "AgentError", "build_agent"]
