"""Shared adapter for OpenAI-compatible chat endpoints (Ollama, Meta Muse)."""

from __future__ import annotations

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ..transcript import Message
from .base import DEFAULT_TIMEOUT_S, Agent, AgentError, build_prompt, redact


class OpenAICompatAgent(Agent):
    def __init__(self, name: str, base_url: str, model: str, api_key: str | None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.name = name
        self.base_url = base_url
        self.model = model
        self._api_key = api_key
        self.timeout = timeout
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if not self._api_key:
            raise AgentError(f"{self.name}: API key not configured")
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self.base_url, timeout=self.timeout, max_retries=1)
        return self._client

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, APITimeoutError):
            return f"{self.name}: request timed out after {self.timeout:.0f}s"
        if isinstance(exc, APIConnectionError):
            return f"{self.name}: cannot connect to {self.base_url}"
        if isinstance(exc, APIStatusError):
            return f"{self.name}: HTTP {exc.status_code} from API: {redact(str(exc.message))}"
        return f"{self.name}: {type(exc).__name__}: {redact(str(exc))}"

    async def reply(self, task: str, transcript: list[Message]) -> str:
        system, user = build_prompt(self.name, task, transcript)
        client = self._get_client()
        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001 - every failure becomes a clean AgentError
            raise AgentError(self._describe(exc)) from None
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        if not text:
            raise AgentError(f"{self.name}: empty response from model {self.model}")
        return text

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            client = self._get_client()
            models = await client.models.list()
            ids = {m.id async for m in models} if hasattr(models, "__aiter__") else {m.id for m in models.data}
        except AgentError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, self._describe(exc)
        if self.model not in ids:
            return False, f"{self.name}: model '{self.model}' not available at {self.base_url}"
        return True, f"model {self.model} at {self.base_url}"
