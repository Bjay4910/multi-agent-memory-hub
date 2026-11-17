"""Gemini via the google-genai SDK. Key comes from GEMINI_API_KEY."""

from __future__ import annotations

import asyncio
import os

from ..transcript import Message
from .base import DEFAULT_TIMEOUT_S, Agent, AgentError, build_prompt, redact

DEFAULT_MODEL = "gemini-4-hydrogen"


class GeminiAgent(Agent):
    name = "gemini"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise AgentError("gemini: API key not configured (set GEMINI_API_KEY)")
        if self._client is None:
            from google import genai  # imported lazily: the SDK is slow to import

            self._client = genai.Client(api_key=api_key)
        return self._client

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return f"gemini: request timed out after {self.timeout:.0f}s"
        code = getattr(exc, "code", None)
        prefix = f"gemini: HTTP {code}: " if code else f"gemini: {type(exc).__name__}: "
        return prefix + redact(str(getattr(exc, "message", None) or exc))

    async def reply(self, task: str, transcript: list[Message]) -> str:
        from google.genai import types

        system, user = build_prompt(self.name, task, transcript)
        client = self._get_client()
        try:
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=self.model,
                    contents=user,
                    config=types.GenerateContentConfig(system_instruction=system),
                ),
                timeout=self.timeout,
            )
            text = (resp.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise AgentError(self._describe(exc)) from None
        if not text:
            raise AgentError(f"gemini: empty response from model {self.model}")
        return text

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            client = self._get_client()
            await asyncio.wait_for(client.aio.models.get(model=self.model), timeout=30)
        except AgentError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, self._describe(exc)
        return True, f"model {self.model}"
