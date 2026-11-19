"""Remove secret-looking strings before text reaches an extractor or the memory store."""

from __future__ import annotations

import math
import os
import re
from collections import Counter

from ..agents.base import SECRET_ENV_VARS

REPLACEMENT = "[REDACTED]"

# Each pattern matches the secret itself; ``label_value`` patterns keep the label.
_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),  # Google API keys
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # OpenAI / Anthropic style
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),  # GitHub tokens
    re.compile(r"\bLLM\|[^\s\"']{8,}"),  # Meta-style keys
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}", re.I),
]
_LABEL_VALUE = re.compile(
    r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|access[_-]?key|client[_-]?secret)\s*[:=]\s*)"
    r"([\"']?)[^\s\"']{6,}\2"
)
# Long unbroken runs that look random (no '/', '.', so paths and URLs survive).
_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9_\-+=]{32,}")
# The room's own session ids (see transcript.new_session_id), optionally with a note prefix.
# Exempt from the high-entropy rule only when the WHOLE candidate run is exactly one id, so a
# secret glued onto an id or a prefix still forms a longer run and is still caught.
_OWN_SESSION_ID = re.compile(r"(?:(?:manual|clipboard|mcp|claude-code)-)?\d{8}T\d{6}Z-[0-9a-f]{6}")


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _looks_random(token: str) -> bool:
    if _OWN_SESSION_ID.fullmatch(token):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return has_digit and has_alpha and _entropy(token) >= 3.5


def redact_secrets(text: str) -> str:
    """Return ``text`` with secret-looking substrings replaced by ``[REDACTED]``."""
    if not text:
        return text
    for var in SECRET_ENV_VARS:  # the actual configured key values, whatever they look like
        value = os.environ.get(var)
        if value and len(value) >= 8:
            text = text.replace(value, REPLACEMENT)
    for pattern in _PATTERNS:
        text = pattern.sub(REPLACEMENT, text)
    text = _LABEL_VALUE.sub(lambda m: m.group(1) + REPLACEMENT, text)
    return _TOKEN_CANDIDATE.sub(lambda m: REPLACEMENT if _looks_random(m.group(0)) else m.group(0), text)



def find_secrets(text: str) -> list[str]:
    """Names of the secret patterns that still match ``text`` (``[REDACTED]`` itself is ignored).

    Used as a last check before writing files that leave the database, such as
    vault notes: anything listed here means "do not write".
    """
    if not text:
        return []
    found = []
    for var in SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value and len(value) >= 8 and value in text:
            found.append(var)
    for i, pattern in enumerate(_PATTERNS):
        if pattern.search(text):
            found.append(f"pattern-{i}")
    if any(m.group(0)[len(m.group(1)):].strip("\"'") != REPLACEMENT for m in _LABEL_VALUE.finditer(text)):
        found.append("labelled-value")
    if any(_looks_random(m.group(0)) for m in _TOKEN_CANDIDATE.finditer(text)):
        found.append("high-entropy-token")
    return found
