"""Hermes running locally through Ollama's OpenAI-compatible endpoint."""

from __future__ import annotations

import os

from .openai_compat import OpenAICompatAgent

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "hermes3:8b"


class HermesAgent(OpenAICompatAgent):
    def __init__(self) -> None:
        super().__init__(
            name="hermes",
            base_url=os.environ.get("HERMES_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("HERMES_MODEL", DEFAULT_MODEL),
            api_key="ollama",  # Ollama ignores the key, but the client requires one
            supports_json_mode=True,  # Ollama honours response_format=json_object
        )
