"""Muse via the Meta Model API (OpenAI-compatible). Key comes from MUSE_API_KEY."""

from __future__ import annotations

import os

from .openai_compat import OpenAICompatAgent

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.1"


class MuseAgent(OpenAICompatAgent):
    def __init__(self) -> None:
        super().__init__(
            name="muse",
            base_url=os.environ.get("MUSE_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("MUSE_MODEL", DEFAULT_MODEL),
            api_key=os.environ.get("MUSE_API_KEY"),
        )
