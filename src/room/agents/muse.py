"""Muse via the Meta Model API (OpenAI-compatible). Key comes from MUSE_API_KEY.

Muse is a paid API, so it has a daily token budget: usage is tracked in
~/.agent-room/usage/muse.json and calls stop once MUSE_DAILY_TOKEN_LIMIT
tokens (default 200000) have been used on the current UTC day.
"""

from __future__ import annotations

import os

from ..usage import DailyBudget, UsageTracker
from .openai_compat import OpenAICompatAgent

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.1"
DEFAULT_DAILY_TOKEN_LIMIT = 200_000
LIMIT_ENV = "MUSE_DAILY_TOKEN_LIMIT"


def parse_limit(raw: str | None) -> int:
    """Parse the daily limit. Raises ValueError for anything but a non-negative integer."""
    if raw is None or raw.strip() == "":
        return DEFAULT_DAILY_TOKEN_LIMIT
    value = int(raw.strip().replace("_", ""))
    if value < 0:
        raise ValueError("negative")
    return value


class MuseAgent(OpenAICompatAgent):
    def __init__(self) -> None:
        config_error = None
        try:
            limit = parse_limit(os.environ.get(LIMIT_ENV))
        except ValueError:
            # Fail closed: a typo in the budget must not mean "unlimited".
            limit = 0
            config_error = f"muse: invalid {LIMIT_ENV} (expected a non-negative integer); not calling the API"
        super().__init__(
            name="muse",
            base_url=os.environ.get("MUSE_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("MUSE_MODEL", DEFAULT_MODEL),
            api_key=os.environ.get("MUSE_API_KEY"),
            budget=DailyBudget(UsageTracker("muse"), limit),
            config_error=config_error,
        )
