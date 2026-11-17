"""Claude via the local Claude Code CLI (`claude -p`), using the user's existing login.

No API key is involved. The CLI is invoked as a plain chat model:

* ``--tools ""``                 no built-in tools (no shell, no file reads or edits)
* ``--strict-mcp-config``        ignore every configured MCP server
* ``--disable-slash-commands``   no skills or slash commands
* ``--no-session-persistence``   do not save the conversation as a resumable session
* ``--system-prompt``            replace the coding-agent prompt with a chat prompt

It runs in a temporary empty directory so no project CLAUDE.md is picked up,
and the room's provider keys are removed from its environment.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

from ..transcript import Message
from .base import DEFAULT_TIMEOUT_S, SECRET_ENV_VARS, Agent, AgentError, build_prompt, redact


def build_command(executable: str, system: str, prompt: str, model: str | None = None) -> list[str]:
    cmd = [
        executable,
        "-p",
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "text",
        "--system-prompt", system,
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


class ClaudeCLIAgent(Agent):
    name = "claude"

    def __init__(self, timeout: float | None = None) -> None:
        self.executable = os.environ.get("CLAUDE_BIN", "claude")
        self.model = os.environ.get("CLAUDE_MODEL") or None
        self.timeout = timeout or float(os.environ.get("CLAUDE_TIMEOUT", DEFAULT_TIMEOUT_S))

    def _resolve(self) -> str:
        path = shutil.which(self.executable)
        if not path:
            raise AgentError(f"claude: '{self.executable}' not found on PATH (install Claude Code)")
        return path

    @staticmethod
    def _child_env() -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS}

    async def _run(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory(prefix="agent-room-claude-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=self._child_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                proc.kill()
                await proc.wait()
                raise
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def reply(self, task: str, transcript: list[Message]) -> str:
        system, user = build_prompt(self.name, task, transcript)
        cmd = build_command(self._resolve(), system, user, self.model)
        try:
            code, out, err = await self._run(cmd, self.timeout)
        except asyncio.TimeoutError:
            raise AgentError(f"claude: CLI timed out after {self.timeout:.0f}s") from None
        except OSError as exc:
            raise AgentError(f"claude: failed to start CLI: {redact(str(exc))}") from None
        if code != 0:
            detail = redact(err or out) or "no output"
            raise AgentError(f"claude: CLI exited with code {code}: {detail}")
        text = out.strip()
        if not text:
            raise AgentError("claude: CLI returned an empty answer")
        return text

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            exe = self._resolve()
            code, out, err = await self._run([exe, "--version"], timeout=20)
        except AgentError as exc:
            return False, str(exc)
        except asyncio.TimeoutError:
            return False, "claude: 'claude --version' timed out"
        except OSError as exc:
            return False, f"claude: {redact(str(exc))}"
        if code != 0:
            return False, f"claude: 'claude --version' failed: {redact(err or out)}"
        # --version does not prove the login works; `room ask` surfaces auth errors.
        return True, f"Claude Code CLI {out.strip()} (login not verified)"
