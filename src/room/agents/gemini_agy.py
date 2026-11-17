"""Gemini via the Antigravity CLI (`agy -p`), using the user's Google sign-in.

No API key is involved. The CLI is invoked so that it can only answer in text:

* headless print mode (``-p``) auto-denies every tool that needs a permission
  prompt (file reads outside the workspace, writes, shell), and we never pass
  ``--dangerously-skip-permissions``
* ``--settings '{"permissions":{"deny":["*"]}}'``  an explicit deny-all rule on top
* ``--sandbox``                    terminal restrictions, in case a tool slips through
* ``--disable-slash-commands``     no slash commands or skills
* it runs in an empty temporary directory, with stdin closed and the room's
  provider keys removed from its environment

``agy`` has no system-prompt flag, so the room's instructions are prepended to
the prompt. A hard timeout (``AGY_TIMEOUT``, default 120 s) kills the whole
process group.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

from ..transcript import Message
from .base import Agent, AgentError, build_prompt, redact
from .subprocess_cli import clean_output, run_cli

DEFAULT_TIMEOUT_S = 120.0
DENY_ALL_SETTINGS = json.dumps({"permissions": {"deny": ["*"]}})
# Status lines agy prints itself (normally on stderr). Kept narrow so answer text is never dropped.
NOISE_PREFIXES = ("jetski:", "warning: --", "fetching available models")


def default_executable() -> str:
    found = shutil.which("agy")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/agy")


def build_command(executable: str, prompt: str, model: str | None = None) -> list[str]:
    cmd = [
        executable,
        "--sandbox",
        "--disable-slash-commands",
        "--settings", DENY_ALL_SETTINGS,
        "--output-format", "text",
    ]
    if model:
        cmd += ["--model", model]
    # -p takes the prompt as its value, so it must come last.
    cmd += ["-p", prompt]
    return cmd


class AntigravityGeminiAgent(Agent):
    name = "gemini"

    def __init__(self, timeout: float | None = None) -> None:
        self.executable = os.environ.get("AGY_BIN") or default_executable()
        self.model = os.environ.get("AGY_MODEL") or None
        self.timeout = timeout or float(os.environ.get("AGY_TIMEOUT", DEFAULT_TIMEOUT_S))

    def _resolve(self) -> str:
        path = shutil.which(self.executable)
        if not path:
            raise AgentError(f"gemini: Antigravity CLI '{self.executable}' not found (install agy)")
        return path

    async def reply(self, task: str, transcript: list[Message]) -> str:
        system, user = build_prompt(self.name, task, transcript)
        cmd = build_command(self._resolve(), f"{system}\n\n{user}", self.model)
        try:
            code, out, err = await run_cli(cmd, self.timeout)
        except asyncio.TimeoutError:
            raise AgentError(f"gemini: agy timed out after {self.timeout:.0f}s") from None
        except OSError as exc:
            raise AgentError(f"gemini: failed to start agy: {redact(str(exc))}") from None
        text = clean_output(out, NOISE_PREFIXES)
        if code != 0:
            raise AgentError(f"gemini: agy exited with code {code}: {redact(clean_output(err) or text) or 'no output'}")
        if not text:
            # e.g. a tool call was auto-denied in headless mode; agy explains on stderr.
            raise AgentError(f"gemini: agy returned no answer: {redact(clean_output(err)) or 'no output'}")
        return text

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            exe = self._resolve()
            code, out, err = await run_cli([exe, "--version"], timeout=20)
        except AgentError as exc:
            return False, str(exc)
        except asyncio.TimeoutError:
            return False, "gemini: 'agy --version' timed out"
        except OSError as exc:
            return False, f"gemini: {redact(str(exc))}"
        version = clean_output(out)
        if code != 0 or not version:
            return False, f"gemini: 'agy --version' failed: {redact(clean_output(err) or version)}"
        # --version does not prove the sign-in works; `room ask` surfaces auth errors.
        return True, f"Antigravity CLI {version.split()[0]} (sign-in not verified)"
