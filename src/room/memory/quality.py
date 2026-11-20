"""Deterministic filters around fact extraction.

Before the model (``prefilter_messages``): drop chatter-only messages (greetings,
thanks, "reply with just the word ...", impersonal general-knowledge questions) and
tool noise (code fences, tracebacks, shell prompts, symbol-heavy lines). If nothing
is left, the model is not called at all.

After the model (``clean_extraction``): keep only nodes that are
- grounded: their ``evidence`` quote is in the user's own text (nodes without
  evidence, from backends without structured output, are kept as before),
- worth keeping: not chatter, not the user's question restated, confidence >= 0.5,
- secret-free: any secret pattern in name, summary or evidence drops the node,
- new: not the same name twice in one output, and not a fact the graph already holds
  under another name.
Edges that touch a dropped node are dropped. A "Me" node whose summary only
describes the conversation keeps its links but no summary.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..transcript import ROLE_USER, Message
from .redact import find_secrets
from .store import STOPWORDS, normalize_name

if TYPE_CHECKING:
    from .store import MemoryStore

MIN_CONFIDENCE = 0.5
EVIDENCE_MIN_OVERLAP = 0.8
DUPLICATE_OVERLAP = 0.8
# Order used when one output names the same thing with several types.
TYPE_PRIORITY = ("application", "decision", "project", "preference", "task", "document", "person",
                 "organization", "skill", "fact")

_GREETING = (r"(hi|hello|hey|yo|thanks|thank you|thx|ty|ok|okay|great|cool|nice|perfect|got it|sounds good|"
             r"bye|goodbye|good morning|good night|yes|no|sure|yep|nope|please|test|testing|ping)")
_CHATTER = re.compile(rf"^(?:{_GREETING}[\s,.!?]*)+(?:there|again|so much|a lot)?[\s,.!?]*$", re.I)
_TEST_PROMPT = re.compile(r"^\s*(reply|respond|answer|say)\b.{0,40}\b(just|only)\b.{0,40}$|^\s*say\s+\w+[.!]?\s*$", re.I)
_QUESTION = re.compile(r"^\s*(what|how|why|who|when|where|which|can|could|should|would|is|are|do|does|did|explain|define)\b[^\n]*\?\s*$", re.I)
_FIRST_PERSON = re.compile(r"\b(i|i'm|im|i've|i'd|my|mine|me|we|we're|our|us)\b", re.I)
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_NOISE_LINE = re.compile(
    r"^\s*(Traceback \(most recent call last\):|File \".*\", line \d+|[A-Za-z_.]*(Error|Exception|Warning)(:|$)|"
    r"\$ |% |> |>>> |at [\w.$<>]+\(.*:\d+\)|npm (ERR|WARN)|\[\d{1,2}:\d{2}(:\d{2})?\]|exit (code|status) \d+)")
_META_SUMMARY = re.compile(
    r"^\s*(the )?user\b[^.]*\b(asked|asks|is asking|was asking|wants to know|wanted to know|inquired|requested|"
    r"provided the instruction|instructed|greeted|said (hi|hello|thanks)|thanked|is speaking|who wrote|"
    r"who is speaking|seeking advice|is seeking|asked about)\b", re.I)
_CHATTER_FACT = re.compile(r"\b(greet(ed|ing|s)?|said (hi|hello)|thank(ed|s)? the|reply with just|the word (hi|hello))\b", re.I)


# -- before the model ---------------------------------------------------------------------------

def _is_symbol_heavy(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 20:
        return False
    letters = sum(ch.isalpha() or ch.isspace() for ch in stripped)
    return letters / len(stripped) < 0.6


def clean_message(text: str) -> str:
    """The message without code blocks and tool-noise lines; '' if it is only chatter."""
    text = _FENCE.sub("\n", text or "")
    lines = [ln for ln in text.splitlines() if not _NOISE_LINE.match(ln) and not _is_symbol_heavy(ln)]
    # Lines indented under a traceback ("    connect(API_TOKEN)") are noise too.
    kept, in_trace = [], False
    for ln in text.splitlines():
        if ln not in lines:
            in_trace = True
            continue
        if in_trace and ln.startswith((" ", "\t")):
            continue
        in_trace = False
        kept.append(ln)
    cleaned = "\n".join(kept).strip()
    if not cleaned or _CHATTER.match(cleaned) or _TEST_PROMPT.match(cleaned):
        return ""
    if _QUESTION.match(cleaned) and not _FIRST_PERSON.search(cleaned):
        return ""
    return cleaned


def prefilter_messages(messages: list[Message]) -> list[Message]:
    """User messages cleaned of chatter and noise (dropped if empty); other messages unchanged."""
    out = []
    for m in messages:
        if m.role != ROLE_USER:
            out.append(m)
            continue
        cleaned = clean_message(m.content)
        if cleaned:
            out.append(Message(m.session_id, m.agent, m.round, m.role, cleaned, m.ts))
    return out


# -- after the model ------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w.]+", " ", text.lower())).strip()


def _content_words(text: str) -> set[str]:
    return {w.strip(".") for w in _norm(text).split() if w.strip(".") and w not in STOPWORDS}


def grounded(node: dict, source: str) -> bool:
    norm_source = _norm(source)
    evidence = node.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        ev = _norm(evidence)
        if ev and ev in norm_source:
            return True
        words = _content_words(evidence)
        return bool(words) and len(words & _content_words(source)) / len(words) >= EVIDENCE_MIN_OVERLAP
    return True  # no evidence (backends without structured output): kept, as before


def low_value(node: dict) -> bool:
    if node.get("confidence", 1) < MIN_CONFIDENCE:
        return True
    text = f"{node['name']}. {node['summary']}"
    if normalize_name(node["name"]) == "me":
        return False  # handled separately: the person node keeps its links
    return bool(_META_SUMMARY.search(node["summary"]) or _CHATTER_FACT.search(text))


def has_secret(node: dict) -> bool:
    return bool(find_secrets(" ".join(str(node.get(k) or "") for k in ("name", "summary", "evidence")))
                or "[REDACTED]" in f"{node['name']} {node['summary']}")


def _sentence_words(summary: str) -> list[set[str]]:
    return [w for w in (_content_words(s) for s in re.split(r"(?<=[.!?])\s+", summary)) if w]


def _known_elsewhere(node: dict, store: "MemoryStore | None") -> bool:
    """True if an active node of the same type but another name already says the same."""
    if store is None or not node["summary"].strip():
        return False
    norm = normalize_name(node["name"])
    new = _sentence_words(node["summary"])
    if not new:
        return False
    for other in store.all_nodes():
        if other.type != node["type"] or normalize_name(other.name) == norm:
            continue
        old = _sentence_words(other.summary)
        if all(any(len(s & o) / len(s | o) >= DUPLICATE_OVERLAP for o in old) for s in new):
            return True
    return False


def clean_extraction(data: dict, source: str, store: "MemoryStore | None" = None) -> tuple[dict, dict[str, int]]:
    """Filtered copy of validated extraction ``data`` and how many nodes each rule dropped."""
    dropped = {"ungrounded": 0, "low_value": 0, "secret": 0, "duplicate": 0, "known": 0}
    kept: dict[str, dict] = {}
    rank = {t: i for i, t in enumerate(TYPE_PRIORITY)}
    for n in data.get("nodes", []):
        if has_secret(n):
            dropped["secret"] += 1
            continue
        if not grounded(n, source):
            dropped["ungrounded"] += 1
            continue
        if low_value(n):
            dropped["low_value"] += 1
            continue
        node = {k: n[k] for k in ("type", "name", "summary", "confidence")}
        if normalize_name(node["name"]) == "me":
            node["type"] = "person"  # the user is always the person node "Me"
        if normalize_name(node["name"]) == "me" and (_META_SUMMARY.search(node["summary"]) or _CHATTER_FACT.search(node["summary"])):
            node["summary"] = ""
        key = normalize_name(node["name"])
        if key in kept:
            dropped["duplicate"] += 1
            if rank.get(node["type"], 99) < rank.get(kept[key]["type"], 99):
                kept[key] = node
            continue
        if _known_elsewhere(node, store):
            dropped["known"] += 1
            continue
        kept[key] = node
    removed = {normalize_name(n["name"]) for n in data.get("nodes", [])} - set(kept)
    edges = [e for e in data.get("edges", [])
             if normalize_name(e["src"]) not in removed and normalize_name(e["dst"]) not in removed]
    return {"nodes": list(kept.values()), "edges": edges}, dropped
