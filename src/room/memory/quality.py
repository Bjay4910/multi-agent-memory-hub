"""Deterministic filters around fact extraction.

Before the model (``prefilter_messages``): drop chatter-only messages (greetings,
thanks, "reply with just the word ...", impersonal general-knowledge questions) and
tool noise (code fences, tracebacks, shell prompts, symbol-heavy lines). A leading
"> " is a quote marker and is stripped, not dropped. A line ending in a backslash and the
line after it are a split shell command and are dropped. The indented block under a noise
line belongs to it until the next non-indented line: under a stack trace all of it is
dropped; under other tool output only lines that start with the user speaking ("I ...",
"My ...") are kept. A bare "Warning:"/"Error:" line is kept only if it has a whole-word
first-person pronoun (not "I/O" or "me/utils"). An impersonal definition question ("What
is Apache Kafka?") is dropped; any other impersonal question is dropped unless it names a
multi-word Title Case name (likely the user's own project, "Is Postgres right for Trail
Tracker?").
If nothing is left, the model is not called at all.

After the model (``clean_extraction``): keep only nodes that are
- grounded: their ``evidence`` quote is in the user's own text (nodes with no
  evidence field, from backends without structured output, are kept as before;
  a blank quote is ungrounded),
- worth keeping: not chatter, not the user's question restated, confidence >= 0.5,
- not an open question: a node whose evidence is found only in a question sentence is
  kept only if it is a project or application (the thing the user asks about); a
  decision, task or preference read out of a question is dropped,
- secret-free: any secret pattern in name, summary or evidence drops the node, and so does
  a name or summary that talks about secrets, tokens, passwords or key variables
  ("User set ANTHROPIC_API_KEY"); for "Me" only the summary is emptied,
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
# First person as a whole word: not "I/O", "me/utils", "us-east-1" or "a.my".
_FIRST_PERSON = re.compile(r"(?<![\w/\\.-])(i'm|i've|i'd|i'll|im|i|my|mine|me|we're|we|our|us)(?![\w/\\-]|\.\w)", re.I)
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_NOISE_LINE = re.compile(
    r"^\s*(Traceback \(most recent call last\):|File \".*\", line \d+|[A-Za-z_.]+(Error|Exception|Warning)(:|$)|"
    r"\$ |% |>>> |at [\w.$<>]+\(.*:\d+\)|npm (ERR|WARN)|\[\d{1,2}:\d{2}(:\d{2})?\]|exit (code|status) \d+)")
# A bare "Error:" / "Warning:" line is tool output unless it speaks in the first person.
_BARE_LEVEL = re.compile(r"^\s*(error|exception|warning|warn|fatal)\s*:", re.I)
# Stack-trace lines: anything indented under them is part of the trace.
_TRACE_LINE = re.compile(r"^\s*(Traceback \(most recent call last\):|File \".*\", line \d+|at [\w.$<>]+\(.*:\d+\))")
# A line that opens with the user speaking: "I ...", "I'm ...", "My ..." (not "I/O").
_SPEAKER_START = re.compile(r"^\s*(i|i'm|i've|i'd|i'll|my)(?![\w/\\-]|\.\w)", re.I)
_QUOTE_MARK = re.compile(r"^[ \t]*>[ \t]?(?!>)", re.M)
_MULTIWORD_NAME = re.compile(r"\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*)+\b")
_DEFINITION = re.compile(r"^\s*(what(?:'s|\s+(?:is|are|was|were))|who(?:'s|\s+(?:is|are|was|were))|define|explain|describe|"
                         r"tell me about|how\s+(?:does|do|did)\b.*\bwork)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
QUESTION_ENTITY_TYPES = ("project", "application")
_META_SUMMARY = re.compile(
    r"^\s*(the )?user\b[^.]*\b(asked|asks|is asking|was asking|wants to know|wanted to know|inquired|requested|"
    r"provided the instruction|instructed|greeted|said (hi|hello|thanks)|thanked|is speaking|who wrote|"
    r"who is speaking|seeking advice|is seeking|asked about|is making a decision|is deciding|is trying to decide|"
    r"is wondering|is unsure|is not sure)\b", re.I)
_CHATTER_FACT = re.compile(r"\b(greet(ed|ing|s)?|said (hi|hello)|thank(ed|s)? the|reply with just|the word (hi|hello))\b", re.I)


# -- before the model ---------------------------------------------------------------------------

def _is_symbol_heavy(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 20:
        return False
    letters = sum(ch.isalpha() or ch.isspace() for ch in stripped)
    return letters / len(stripped) < 0.6


def _is_noise(line: str) -> bool:
    if _NOISE_LINE.match(line) or _is_symbol_heavy(line):
        return True
    return bool(_BARE_LEVEL.match(line)) and not _FIRST_PERSON.search(line)


def clean_message(text: str) -> str:
    """The message without code blocks and tool-noise lines; '' if it is only chatter."""
    text = _QUOTE_MARK.sub("", _FENCE.sub("\n", text or ""))
    # The indented block under a noise line belongs to it, until the next non-indented line.
    # Under a stack trace ("    connect(API_TOKEN)") every indented line is noise. Under other
    # tool output ("$ npm test") an indented line is kept only when it starts with the user
    # speaking ("  I switched Trail Tracker to Vitest"); the rest of the block stays noise.
    # A line ending in a backslash, and the line after it, are a shell command split over lines
    # ("$ docker run \\" / "> --rm -it \\" / "> -e MODE=prod web:latest").
    kept, under, continued = [], None, False  # under: None, "trace" or "output"
    for ln in text.splitlines():
        in_command, continued = continued, ln.rstrip().endswith("\\")
        if _is_noise(ln) or continued or in_command:
            under = "trace" if _TRACE_LINE.match(ln) else ("output" if under is None else under)
            continue
        if under and ln.startswith((" ", "\t")):
            if under == "output" and _SPEAKER_START.match(ln):
                kept.append(ln)
            continue
        under = None
        kept.append(ln)
    cleaned = "\n".join(kept).strip()
    if not cleaned or _CHATTER.match(cleaned) or _TEST_PROMPT.match(cleaned):
        return ""
    if _QUESTION.match(cleaned) and not _FIRST_PERSON.search(cleaned):
        rest = cleaned.split(None, 1)[1] if " " in cleaned.strip() else ""
        # A definition question is general knowledge whatever it names ("What is Apache Kafka?");
        # any other impersonal question must name a multi-word Title Case name, probably the user's
        # own project ("Is Postgres right for Trail Tracker?"), to reach the model.
        if _DEFINITION.match(cleaned) or not _MULTIWORD_NAME.search(rest):
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
    if "evidence" not in node:
        return True  # no evidence field (backends without structured output): kept, as before
    evidence = node["evidence"]
    if not isinstance(evidence, str) or not evidence.strip():
        return False  # a blank quote grounds nothing
    ev = _norm(evidence)
    if ev and ev in _norm(source):
        return True
    words = _content_words(evidence)
    return bool(words) and len(words & _content_words(source)) / len(words) >= EVIDENCE_MIN_OVERLAP


def low_value(node: dict) -> bool:
    if node.get("confidence", 1) < MIN_CONFIDENCE:
        return True
    text = f"{node['name']}. {node['summary']}"
    if normalize_name(node["name"]) == "me":
        return False  # handled separately: the person node keeps its links
    return bool(_META_SUMMARY.search(node["summary"]) or _CHATTER_FACT.search(text))


def statements_only(source: str) -> str:
    """``source`` without its question sentences."""
    return "\n".join(s for s in _SENTENCE.split(source) if s.strip() and not s.rstrip().endswith("?"))


def from_question_only(node: dict, source: str, statements: str) -> bool:
    """True if the node's evidence is found in a question of the user and nowhere else."""
    return "evidence" in node and grounded(node, source) and not grounded(node, statements)


# Words about secrets, and environment-variable names for them (ANTHROPIC_API_KEY, GITHUB_TOKEN).
# "the key for Lumen Budget" is not matched: a bare "key" is too common.
_SECRET_TALK = re.compile(
    r"(?i:\b(secrets?|tokens?|api[ _-]?keys?|access[ _-]?keys?|passwords?|passphrases?|credentials?|private[ _-]keys?)\b)"
    r"|\b[A-Z0-9]+(?:_[A-Z0-9]+)*_(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PAT)\b")


def talks_about_secrets(node: dict) -> bool:
    """True if the node's name or summary is about a secret, token or key variable (even with no value)."""
    return bool(_SECRET_TALK.search(f"{node.get('name') or ''} {node.get('summary') or ''}"))


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
    dropped = {"ungrounded": 0, "low_value": 0, "question": 0, "secret": 0, "duplicate": 0, "known": 0}
    statements = statements_only(source)
    kept: dict[str, dict] = {}
    rank = {t: i for i, t in enumerate(TYPE_PRIORITY)}
    for n in data.get("nodes", []):
        is_me = normalize_name(n["name"]) == "me"
        if has_secret(n) or (talks_about_secrets(n) and not is_me):
            dropped["secret"] += 1
            continue
        if not grounded(n, source):
            dropped["ungrounded"] += 1
            continue
        if low_value(n):
            dropped["low_value"] += 1
            continue
        question_only = from_question_only(n, source, statements)
        if question_only and not is_me and n["type"] not in QUESTION_ENTITY_TYPES:
            dropped["question"] += 1  # an open question is not a decision, task or preference
            continue
        node = {k: n[k] for k in ("type", "name", "summary", "confidence")}
        if is_me:
            node["type"] = "person"  # the user is always the person node "Me"
        if is_me and (question_only or talks_about_secrets(node) or _META_SUMMARY.search(node["summary"])
                      or _CHATTER_FACT.search(node["summary"])):
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
