"""Extraction: turn a session (or a manual note) into graph nodes and edges.

Flow for one session:

1. Build the source text (task + final answer, or the manual note), redact
   secret-looking strings, and cap its length.
2. Ask the extractor agent (``MEMORY_EXTRACTOR``, default ``hermes``, local and
   free) for strict JSON and validate it against the schema.
3. If invalid, send one repair prompt listing the errors.
4. Still invalid (or the agent failed)? Try ``MEMORY_EXTRACTOR_FALLBACK`` if set.
5. Otherwise queue the session in ``~/.agent-room/memory_queue/`` so that
   ``room memory process`` can retry later.

While this runs, the presence file for the agent ``memory`` says ``working``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import paths, presence
from ..agents import build_agent
from ..agents.base import Agent, redact
from ..fsutil import atomic_write_json
from ..transcript import ROLE_AGENT, ROLE_FINAL, ROLE_USER, Message, load_session, utc_now_iso
from .paths import ensure_outside_repo, queue_dir
from .redact import redact_secrets
from .store import NODE_TYPES, RELATIONS, MemoryStore, Provenance

EXTRACTORS = ("hermes", "claude", "gemini")
DEFAULT_EXTRACTOR = "hermes"
MAX_SOURCE_CHARS = 8000
MAX_NODES = 40
MAX_EDGES = 80
NAME_MAX_CHARS = 120
SUMMARY_MAX_CHARS = 600
DEFAULT_TIMEOUT_S = 300.0

SYSTEM_PROMPT = f"""You extract long-term memory for a personal knowledge graph.
Record durable facts about the user and their world: who they are, their projects and results,
skills, decisions, preferences, applications (jobs, programs, grants) and their status, open
tasks, documents, and the people and organizations involved.

Only record what the text actually states. The USER section is the user speaking about
themselves: those are facts. The ANSWER section is advice from AI assistants: never record its
suggestions, examples or guesses as facts about the user. Do not invent names, organizations,
numbers or dates. Ignore general knowledge, definitions and small talk. Never include passwords,
API keys or tokens.

Output ONLY one JSON object, no prose, no code fences, exactly in this shape:
{{"nodes": [{{"type": "...", "name": "...", "summary": "...", "confidence": 0.0}}],
  "edges": [{{"src": "<node name>", "relation": "...", "dst": "<node name>"}}]}}

Rules:
- type is one of: {", ".join(NODE_TYPES)}
- relation is one of: {", ".join(RELATIONS)}
- name: a short natural name in Title Case with spaces (e.g. "Garden Planner", never
  "garden_planner"), at most {NAME_MAX_CHARS} characters; the user is the person node "Me"
- summary: one or two factual sentences, at most {SUMMARY_MAX_CHARS} characters
- confidence: a number from 0 to 1 (how sure the text makes you)
- an application node is named "<organization> <program or role> application" and links to the
  organization with applied_to; only create organizations the text names
- edges may only use node names from your "nodes" list
- if there is nothing worth remembering, output {{"nodes": [], "edges": []}}"""

# JSON Schema for backends with structured outputs (Ollama). Validation below
# still checks every output, whichever backend produced it.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "maxItems": MAX_NODES,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(NODE_TYPES)},
                    "name": {"type": "string", "minLength": 1, "maxLength": NAME_MAX_CHARS},
                    "summary": {"type": "string", "maxLength": SUMMARY_MAX_CHARS},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["type", "name", "summary", "confidence"],
                "additionalProperties": False,
            },
        },
        "edges": {
            "type": "array",
            "maxItems": MAX_EDGES,
            "items": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "minLength": 1},
                    "relation": {"type": "string", "enum": list(RELATIONS)},
                    "dst": {"type": "string", "minLength": 1},
                },
                "required": ["src", "relation", "dst"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["nodes", "edges"],
    "additionalProperties": False,
}

USER_TEMPLATE = "Text to extract memory from:\n<<<\n{text}\n>>>\n\nJSON:"

REPAIR_TEMPLATE = """Your previous output was not valid. Problems:
{errors}

Previous output:
<<<
{previous}
>>>

Return ONLY the corrected JSON object in the required shape, with no other text."""


# -- validation ---------------------------------------------------------------

def parse_json_object(raw: str) -> object:
    """Parse the model output as JSON, tolerating code fences or text around one object."""
    text = raw.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def validate_extraction(obj: object) -> tuple[dict | None, list[str]]:
    """Check ``obj`` against the extraction schema. Returns (clean data, errors)."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return None, ["top level must be a JSON object"]
    for key in ("nodes", "edges"):
        if not isinstance(obj.get(key), list):
            errors.append(f'"{key}" must be a list')
    extra = set(obj) - {"nodes", "edges"}
    if extra:
        errors.append(f"unexpected top-level keys: {', '.join(sorted(extra))}")
    if errors:
        return None, errors
    if len(obj["nodes"]) > MAX_NODES:
        errors.append(f"too many nodes (max {MAX_NODES})")
    if len(obj["edges"]) > MAX_EDGES:
        errors.append(f"too many edges (max {MAX_EDGES})")

    nodes = []
    for i, n in enumerate(obj["nodes"]):
        where = f"nodes[{i}]"
        if not isinstance(n, dict):
            errors.append(f"{where} must be an object")
            continue
        t = n.get("type")
        name, summary, conf = n.get("name"), n.get("summary"), n.get("confidence")
        if not isinstance(t, str) or t.strip().lower() not in NODE_TYPES:
            errors.append(f"{where}.type {t!r} is not one of {', '.join(NODE_TYPES)}")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}.name must be a non-empty string")
        elif len(name) > NAME_MAX_CHARS:
            errors.append(f"{where}.name is longer than {NAME_MAX_CHARS} characters")
        if not isinstance(summary, str):
            errors.append(f"{where}.summary must be a string")
        elif len(summary) > SUMMARY_MAX_CHARS * 2:
            errors.append(f"{where}.summary is too long")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            errors.append(f"{where}.confidence must be a number between 0 and 1")
        nodes.append(n)

    edges = []
    for i, e in enumerate(obj["edges"]):
        where = f"edges[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{where} must be an object")
            continue
        for key in ("src", "dst"):
            if not isinstance(e.get(key), str) or not e[key].strip():
                errors.append(f"{where}.{key} must be a non-empty string")
        rel = e.get("relation")
        if not isinstance(rel, str) or rel.strip().lower() not in RELATIONS:
            errors.append(f"{where}.relation {rel!r} is not one of {', '.join(RELATIONS)}")
        edges.append(e)

    if errors:
        return None, errors
    clean = {
        "nodes": [
            {
                "type": n["type"].strip().lower(),
                "name": redact_secrets(n["name"].strip()),
                "summary": redact_secrets(n["summary"].strip())[:SUMMARY_MAX_CHARS],
                "confidence": float(n["confidence"]),
            }
            for n in nodes
        ],
        "edges": [
            {"src": e["src"].strip(), "relation": e["relation"].strip().lower(), "dst": e["dst"].strip()}
            for e in edges
        ],
    }
    # A redacted name would be meaningless memory; drop such nodes.
    clean["nodes"] = [n for n in clean["nodes"] if n["name"] != "[REDACTED]"]
    return clean, []


def parse_and_validate(raw: str) -> tuple[dict | None, list[str]]:
    try:
        obj = parse_json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, [f"output is not valid JSON ({exc.msg if hasattr(exc, 'msg') else exc})"]
    return validate_extraction(obj)


# -- source text ----------------------------------------------------------------

def source_text(messages: list[Message]) -> str:
    """The part of a session worth extracting from: the task(s) and the final answer.

    Falls back to the last round of replies when there is no final answer.
    Notes and recalled memory are never included.
    """
    tasks = [m.content for m in messages if m.role == ROLE_USER]
    final = [m.content for m in messages if m.role == ROLE_FINAL]
    if not final:
        replies = [m for m in messages if m.role == ROLE_AGENT]
        last_round = max((m.round for m in replies), default=0)
        final = [f"{m.agent}: {m.content}" for m in replies if m.round == last_round]
    parts = []
    if tasks:
        parts.append("USER (the user speaking about themselves):\n" + "\n\n".join(tasks))
    if final:
        parts.append("ANSWER (advice from AI assistants; may contain guesses):\n" + "\n\n".join(final))
    return redact_secrets("\n\n".join(parts))[:MAX_SOURCE_CHARS]


def session_source(messages: list[Message]) -> str:
    return "manual" if any(m.agent == "manual" for m in messages) else "session"


# -- config -------------------------------------------------------------------

def configured_extractors() -> tuple[list[str], str | None]:
    """[primary, optional fallback] from the environment, or an error message."""
    primary = (os.environ.get("MEMORY_EXTRACTOR") or DEFAULT_EXTRACTOR).strip().lower()
    fallback = (os.environ.get("MEMORY_EXTRACTOR_FALLBACK") or "").strip().lower()
    for var, value in (("MEMORY_EXTRACTOR", primary), ("MEMORY_EXTRACTOR_FALLBACK", fallback)):
        if value and value not in EXTRACTORS:
            return [], f"invalid {var}={value!r} (choose from {', '.join(EXTRACTORS)})"
    names = [primary] + ([fallback] if fallback and fallback != primary else [])
    return names, None


def extract_timeout() -> float:
    try:
        return float(os.environ.get("MEMORY_EXTRACT_TIMEOUT", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


# -- queue ----------------------------------------------------------------------

def queue_path(session_id: str) -> Path:
    return queue_dir() / f"{session_id}.json"


def enqueue(session_id: str, source: str, error: str) -> Path:
    path = ensure_outside_repo(queue_path(session_id))
    previous = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
    atomic_write_json(path, {
        "session_id": session_id,
        "source": source,
        "queued_at": previous.get("queued_at", utc_now_iso()),
        "attempts": int(previous.get("attempts", 0)) + 1,
        "last_error": redact(redact_secrets(error), limit=500),
    })
    return path


def queued_sessions() -> list[dict]:
    items = []
    directory = queue_dir()
    if not directory.is_dir():
        return items
    for path in sorted(directory.glob("*.json")):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return items


# -- running --------------------------------------------------------------------

@dataclass
class ExtractionResult:
    session_id: str
    status: str  # "stored" | "queued" | "empty"
    agent: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.status == "stored":
            c = self.counts
            return (f"stored by {self.agent} in {self.elapsed_s:.1f}s: {c.get('created', 0)} new, "
                    f"{c.get('merged', 0)} merged, {c.get('superseded', 0)} superseded, {c.get('edges', 0)} edges")
        if self.status == "empty":
            return "nothing to extract"
        reason = self.errors[-1] if self.errors else "unknown error"
        return f"queued for `room memory process` after {self.elapsed_s:.1f}s ({reason})"


async def extract_with(agent: Agent, text: str) -> tuple[dict | None, list[str]]:
    """One extraction attempt plus at most one repair prompt."""
    raw = await agent.complete(SYSTEM_PROMPT, USER_TEMPLATE.format(text=text), json_output=True, json_schema=EXTRACTION_SCHEMA)
    data, errors = parse_and_validate(raw)
    if data is not None:
        return data, []
    repair = REPAIR_TEMPLATE.format(errors="\n".join(f"- {e}" for e in errors[:12]), previous=raw[:4000])
    raw = await agent.complete(SYSTEM_PROMPT, repair, json_output=True, json_schema=EXTRACTION_SCHEMA)
    data, repair_errors = parse_and_validate(raw)
    return (data, []) if data is not None else (None, [f"invalid JSON after repair: {repair_errors[0]}"])


async def extract_session(
    session_id: str,
    messages: list[Message],
    agent_factory: Callable[[str], Agent] = build_agent,
    store_path: Path | None = None,
    timeout: float | None = None,
) -> ExtractionResult:
    """Extract one session into the memory store. Never raises; failures are queued."""
    started = time.monotonic()
    source = session_source(messages)
    result = ExtractionResult(session_id, "queued")
    try:
        text = source_text(messages)
        if not text.strip():
            result.status = "empty"
            _dequeue(session_id)
            return result
        names, config_error = configured_extractors()
        if config_error:
            result.errors.append(config_error)
        timeout = timeout or extract_timeout()
        async with presence.working("memory", f"memory extraction: session {session_id}"[:120]):
            for name in names:
                try:
                    data, errors = await asyncio.wait_for(extract_with(agent_factory(name), text), timeout)
                except asyncio.TimeoutError:
                    data, errors = None, [f"{name}: extraction timed out after {timeout:.0f}s"]
                except Exception as exc:  # noqa: BLE001 - an extractor failure must never fail the room
                    msg = str(exc) or type(exc).__name__
                    data, errors = None, [redact(msg if msg.startswith(f"{name}:") else f"{name}: {msg}")]
                if data is None:
                    result.errors.extend(f"{name}: {e}" if not e.startswith(f"{name}:") else e for e in errors)
                    continue
                with MemoryStore(store_path) as store:
                    result.counts = store.apply_extraction(data, Provenance(session_id, name, source))
                result.status, result.agent = "stored", name
                _dequeue(session_id)
                return result
    except Exception as exc:  # noqa: BLE001
        result.errors.append(redact(f"{type(exc).__name__}: {exc}"))
    finally:
        result.elapsed_s = time.monotonic() - started
    try:
        enqueue(session_id, source, result.errors[-1] if result.errors else "unknown error")
    except Exception as exc:  # noqa: BLE001
        result.errors.append(redact(f"could not queue session: {exc}"))
    return result


def _dequeue(session_id: str) -> None:
    try:
        queue_path(session_id).unlink()
    except FileNotFoundError:
        pass


def session_log_path(session_id: str) -> Path:
    return paths.sessions_dir() / f"{session_id}.jsonl"


async def process_queue(agent_factory: Callable[[str], Agent] = build_agent, on_result: Callable[[ExtractionResult], None] | None = None) -> list[ExtractionResult]:
    """Retry every queued session. Sessions whose log is gone are dropped from the queue."""
    results = []
    for item in queued_sessions():
        sid = item.get("session_id", "")
        log = session_log_path(sid)
        if not sid or not log.exists():
            _dequeue(sid)
            res = ExtractionResult(sid, "empty", errors=["session log not found; dropped from queue"])
        else:
            res = await extract_session(sid, load_session(log), agent_factory)
        results.append(res)
        if on_result:
            on_result(res)
    return results
