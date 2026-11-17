"""The Agent interface shared by every model adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..transcript import Message, format_transcript

DEFAULT_TIMEOUT_S = 180.0

SYSTEM_PROMPT = (
    "You are {name}, one participant in a small discussion room with other AI "
    "assistants. Everyone answers the same task and can read the "
    "transcript of earlier replies. Be concise and concrete. If others already "
    "answered, build on, correct or refine their points instead of repeating them. "
    "Reply in plain text only."
)


class AgentError(Exception):
    """A clear, user-facing error from an adapter. Must never contain secrets."""


class Agent(ABC):
    """An LLM participant in the room."""

    name: str

    @abstractmethod
    async def reply(self, task: str, transcript: list[Message]) -> str:
        """Answer ``task`` given everything said so far. Raise AgentError on failure."""

    @abstractmethod
    async def healthcheck(self) -> tuple[bool, str]:
        """Return (reachable, short human-readable detail). Must not raise."""

    def skip_reason(self) -> str | None:
        """Return why this agent must not be called right now (e.g. budget spent), else None.

        The orchestrator checks this before every call and records the reason as
        a transcript note instead of calling the agent.
        """
        return None


def build_prompt(name: str, task: str, transcript: list[Message]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for one agent's turn."""
    system = SYSTEM_PROMPT.format(name=name)
    history = format_transcript(transcript)
    if history:
        user = f"Task:\n{task}\n\nTranscript so far:\n{history}\n\nYour reply as {name}:"
    else:
        user = f"Task:\n{task}\n\nYou are the first to answer. Your reply as {name}:"
    return system, user



SECRET_ENV_VARS = ("GEMINI_API_KEY", "MUSE_API_KEY")


def redact(text: str, limit: int = 300) -> str:
    """Strip any configured key value from ``text`` and truncate it.

    Adapters pass every error message through this before surfacing it, so a
    provider echoing a request header can never leak a key into the terminal,
    transcript or session log.
    """
    import os

    for var in SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
