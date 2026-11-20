"""Deterministic filters around fact extraction.

Before the model (``prefilter_messages``): drop chatter-only messages (greetings,
thanks, "reply with just the word ...", impersonal general-knowledge questions) and
tool noise (code fences, tracebacks, shell prompts, symbol-heavy lines). A leading
"> " is a quote marker and is stripped, not dropped. A line ending in " \\" and the line
after it are a split shell command and are dropped. git, Go-panic, log-level and
source-location lines are noise. The indented block under a noise line (blank lines
included) belongs to it: under a stack trace all of it is dropped, and a new "$ " prompt
ends the trace; under other tool output only lines that open with the user speaking ("I am
...", "My ...", or "We ..." naming a multi-word Title Case name) are kept. A bare level line
("Warning:", "INFO:") is kept only if the text after the colon opens with the user speaking. An impersonal definition question ("What
is Apache Kafka?") is dropped; any other impersonal question is dropped unless it names a
multi-word Title Case name (likely the user's own project, "Is Postgres right for Trail
Tracker?").
If nothing is left, the model is not called at all.

After the model (``clean_extraction``): keep only nodes that are
- grounded: their ``evidence`` quote is in the user's own text (nodes with no
  evidence field, from backends without structured output, are kept as before;
  a blank quote is ungrounded),
- worth keeping: not chatter, not the user's question restated, confidence >= 0.5,
- not an open question: a node whose evidence is found only in a question is kept only if
  it is a project or application (the thing the user asks about) and the question is not
  hypothetical (_HYPOTHETICAL: "If I build Trail Tracker, ..."); a decision, task or
  preference read out of a question is dropped. A clause before the last comma of a
  question is a statement only if it opens with a factual marker (since, because, as a,
  now that, given that ...) and has no conditional word (if, suppose, would ...),
- secret-free: any secret pattern in name, summary or evidence drops the node, and so does
  a credential value the pre-filter withheld, or a name or summary that holds a credential
  value, names a credential variable ("User set ANTHROPIC_API_KEY"), or says the user owns
  or holds a secret ("the user's password"); for "Me" only the summary is emptied. Plain
  technical words ("JWT tokens", "password manager") are kept,
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
# One pronoun list for every first-person rule. _FIRST_PERSON finds a pronoun anywhere, as a whole
# word (not "I/O", "me/utils", "us-east-1" or "a.my"); _SPEAKER_START / _GROUP_START need it to open
# the text and be followed by a word ("I am ...", not "I: ..." or "I [main] ...").
_SPEAKER_WORDS = r"i'm|i've|i'd|i'll|im|i|my"
_GROUP_WORDS = r"we're|we've|we|our"
_FIRST_PERSON = re.compile(rf"(?<![\w/\\.-])({_SPEAKER_WORDS}|mine|me|{_GROUP_WORDS}|us)(?![\w/\\-]|\.\w)", re.I)
_SPEAKER_START = re.compile(rf"^\s*({_SPEAKER_WORDS})\s+[a-z]", re.I)
_GROUP_START = re.compile(rf"^\s*({_GROUP_WORDS})\s+[a-z]", re.I)
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_NOISE_LINE = re.compile(
    r"^\s*(Traceback \(most recent call last\):|File \".*\", line \d+|[A-Za-z_.]+(Error|Exception|Warning)(:|$)|"
    r"\$ |% |>>> |at [\w.$<>]+\(.*:\d+\)|npm (ERR|WARN)|\[\d{1,2}:\d{2}(:\d{2})?\]|exit (code|status) \d+|"
    # git status / git log output
    r"On branch \S+\s*$|Your branch is (up to date|ahead|behind)|Changes (not staged for commit|to be committed)|"
    r"Untracked files:|nothing to commit|no changes added to commit|"
    r"(modified|new file|deleted|renamed|both modified|typechange):\s+\S|commit [0-9a-f]{7,40}\b|"
    r"(Author|Signed-off-by|Co-authored-by|Reviewed-by):\s|"
    # a source location ("main.go:12 +0x1d", "main.c:12:5:") or a bare call frame ("main.main()")
    r"[\w./\\-]+\.[A-Za-z]\w*:\d+(:\d+)?(:|\s+\+0x)|[\w./*()-]+\.\w+\([^)]*\)\s*$)")
# A bare "Error:" / "Warning:" line is tool output unless it speaks in the first person.
_BARE_LEVEL = re.compile(r"^\s*(error|exception|warning|warn|fatal|critical|info|debug|trace|notice)\s*:", re.I)
# Stack-trace lines (Python, JS/Java, Go panics): anything indented under them is part of the trace.
_TRACE_LINE = re.compile(r"^\s*(Traceback \(most recent call last\):|File \".*\", line \d+|at [\w.$<>]+\(.*:\d+\)|"
                         r"panic: |goroutine \d+ \[)")
# A new shell prompt starts a new block of tool output (it ends a stack trace).
_PROMPT_LINE = re.compile(r"^\s*(\$|%) ")
# A trailing backslash after a space continues a shell command ("docker run \\"), not "C:\\Users\\alex\\".
_CONTINUES = re.compile(r"\s\\\s*$")
_QUOTE_MARK = re.compile(r"^[ \t]*>[ \t]?(?!>)", re.M)
_MULTIWORD_NAME = re.compile(r"\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*)+\b")
# Only openers that _QUESTION also accepts can matter here ("Describe X?" is not a _QUESTION).
_DEFINITION = re.compile(r"^\s*(what(?:'s|\s+(?:is|are|was|were))|who(?:'s|\s+(?:is|are|was|were))|define|explain|"
                         r"how\s+(?:does|do|did)\b.*\bwork)\b", re.I)
# -- secrets without a recognizable prefix ("my password is hunter2", "client_secret = xyz") --
# Labels, written with spaces; any one space may also be "_" or "-" ("api key", "api_key", "api-key").
SECRET_LABELS = ("client secret", "secret key", "api key", "access key", "private key", "passphrase", "passcode",
                 "password", "passwd", "pwd", "secret", "token", "key")
# Credential variable names, as suffixes (ANTHROPIC_API_KEY, GITHUB_TOKEN, DB_PASSWD, GH_PAT; PAT needs a prefix).
SECRET_VAR_SUFFIXES = ("API_KEY", "ACCESS_KEY", "SECRET_KEY", "PRIVATE_KEY", "CLIENT_SECRET", "SECRET", "TOKEN",
                       "PASSWORD", "PASSWD", "PAT")
_LABEL_ALTS = "|".join(label.replace(" ", "[ _-]?") for label in SECRET_LABELS)
# A label directly followed by one of these nouns names a thing, not a credential ("token limit is 4096",
# "password policy is strict", "password manager"); the same list as _SECRET_NOUN below, plus policy.
_COMPOUND = r"(?![ \t_-]*(?:managers?|limits?|counts?|budgets?|windows?|polic(?:y|ies))\b)"
_LABEL = (rf"(?<![A-Za-z0-9])(?:(?:[A-Za-z0-9]+_)*(?:{_LABEL_ALTS})|(?:[A-Za-z0-9]+_)+pat)(?![A-Za-z0-9])"
          rf"{_COMPOUND}")
# Up to 6 words may sit between the label and the connector ("the password for Harbor Log is ...",
# "the password (for prod) is ...", "the token for harbor.log is ..."); a word holds no clause or sentence end.
_FILLER = r"(?:[ \t]+[^\s,;:=.!?]+(?:\.[^\s,;:=.!?]+)*){0,6}?"
# The connector: "=", ":", "is", "was", "is now", "set to", "is set to", "equals", "is equal to".
_CONNECT = (r"(?:[ \t]*[:=][ \t]*"
            r"|[ \t]+(?:(?:is|was)[ \t]+equal[ \t]+to|(?:(?:is|was)[ \t]+)?set[ \t]+to|equals|(?:is|was)(?:[ \t]+now)?)"
            r"[ \t]+)")
# The value runs to the end of the clause, so a passphrase ("blue sky mining") is masked whole. There is
# no status-word exception: "the token is valid for one hour" is masked too (security over recall), and
# this can over-mask the rest of a clause, which is accepted.
_CLAUSE = r"[^\s,;][^,;\n]*?(?=\s*(?:[,;]|[.!?](?:\s|$)|\n|$))"
_CREDENTIAL = re.compile(
    rf"{_LABEL}{_FILLER}{_CONNECT}(?P<a>{_CLAUSE})"                    # the password for Harbor Log is x; KEY=x
    r"|\bauthorization\s*:\s*(?:bearer\s+|basic\s+|token\s+)?(?P<d>\S+)"      # Authorization: Bearer x
    r"|\bbearer\s+(?P<e>[A-Za-z0-9._~+/=-]{8,})", re.I)
_SECRET_VAR = re.compile(r"\b(?:[A-Z0-9]+_)*(?:" + "|".join(v for v in SECRET_VAR_SUFFIXES if v != "PAT")
                         + r")\b|\b(?:[A-Z0-9]+_)+PAT\b")
_SECRET_NOUN = (r"(?:client[ _-]?secrets?|secret[ _-]?keys?|api[ _-]?keys?|access[ _-]?keys?|private[ _-]?keys?|"
                r"passphrases?|passcodes?|passwords?|passwd|secrets?|tokens?|credentials?)"
                r"(?!\s*(?:manager|managers|limit|limits|count|counts|budget|window)\b)")
_OWNED_SECRET = re.compile(rf"\b(?:my|our|their|his|her|your|user's|users')\s+(?:\w+\s+)?{_SECRET_NOUN}\b", re.I)
_USER_HOLDS_SECRET = re.compile(
    r"\b(?:the user|user|i|we|they)\s+(?:\w+\s+)?(?:has|have|had|stores?|stored|sets?|keeps?|kept|saves?|saved|"
    r"rotated?|rotates|shares?|shared|entered|leaked|pasted|changed|reset)\s+"
    rf"(?:an?\s+|the\s+|their\s+|my\s+|our\s+|his\s+|her\s+)?(?:\w+\s+)?{_SECRET_NOUN}\b", re.I)


def mask_credentials(text: str) -> str:
    """``text`` with each credential value replaced by [REDACTED]; the rest of the line is kept."""
    def mask(m: re.Match) -> str:
        name = next(k for k, v in m.groupdict().items() if v)
        start, end = m.span(name)
        return m.group(0)[: start - m.start()] + "[REDACTED]" + m.group(0)[end - m.start():]
    return _CREDENTIAL.sub(mask, text or "")


def credential_values(text: str) -> set[str]:
    """Credential values written in ``text`` ("hunter2" in "my password is hunter2"), for the post-filter."""
    values = set()
    for m in _CREDENTIAL.finditer(text or ""):
        value = next(v for v in m.groups() if v).strip(".,;:)'\"")
        if value and value != "[REDACTED]":
            values.add(value)
    return values


def withheld_values(text: str) -> set[str]:
    """Credential values in ``text``, plus each word of them that the model never sees (absent from
    the masked text), so a node that repeats only part of an over-masked clause is dropped too."""
    values = credential_values(text)
    visible = {w.strip(".,;:!?()'\"").lower() for w in mask_credentials(text).split()}
    words = {w.strip(".,;:!?()'\"") for v in values for w in v.split()}
    return values | {w for w in words if len(w) >= 3 and w.lower() not in visible}


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


def _speaks(text: str) -> bool:
    """True if ``text`` opens with the user speaking: "I ...", "My ...", or "We ..." naming a
    multi-word Title Case name ("We migrated Harbor Log ...", not "we expected 3 but got 4")."""
    if _SPEAKER_START.match(text):
        return True
    if _GROUP_START.match(text):
        rest = text.strip().split(None, 1)
        return len(rest) > 1 and bool(_MULTIWORD_NAME.search(rest[1]))
    return False


def _is_noise(line: str) -> bool:
    if _NOISE_LINE.match(line) or _TRACE_LINE.match(line) or _is_symbol_heavy(line):
        return True
    level = _BARE_LEVEL.match(line)  # "Warning: I am moving ..." is the user; "Warning: region us failed" is not
    return bool(level) and not _speaks(line[level.end():])


def clean_message(text: str) -> str:
    """The message without code blocks and tool-noise lines; '' if it is only chatter."""
    # Credential values are masked first, so they never reach the model whatever the line is.
    text = mask_credentials(_QUOTE_MARK.sub("", _FENCE.sub("\n", text or "")))
    # The indented block under a noise line belongs to it, blank lines included, until the next
    # non-indented, non-blank line. Under a stack trace ("    connect(API_TOKEN)") every indented
    # line is noise. Under other tool output ("$ npm test") an indented line is kept only when it
    # opens with the user speaking (see _speaks); the rest of the block stays noise. A new shell
    # prompt starts a new output block, so it ends a stack trace.
    # A line ending in " \\" and the line after it are a shell command split over lines
    # ("$ docker run \\" / "> --rm -it \\" / "> -e MODE=prod web:latest").
    kept, under, continued = [], None, False  # under: None, "trace" or "output"
    for ln in text.splitlines():
        in_command, continued = continued, bool(_CONTINUES.search(ln))
        if _is_noise(ln) or continued or in_command:
            if _TRACE_LINE.match(ln):
                under = "trace"
            elif _PROMPT_LINE.match(ln) or under is None:
                under = "output"
            continue
        if under and not ln.strip():
            continue
        if under and ln.startswith((" ", "\t")):
            if under == "output" and _speaks(ln):
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


# The last comma before a question that runs to the end of the sentence:
# "Since I prefer Rust over Go for CLI tools, | which argument parser should I pick?"
_QUESTION_TAIL = re.compile(r",\s*(?=(?:what|how|why|who|when|where|which|can|could|should|would|is|are|do|does|did)"
                            r"\b[^,]*\?\s*$)", re.I)


# A premise is a fact only if it opens with a factual marker and holds no conditional or
# hypothetical word; when unsure, it is not kept ("If I joined Google, ..." is not a fact).
_FACTUAL_OPENER = re.compile(r"^\s*(since|because|as an?|as someone who|now that|given that|seeing that|"
                             r"considering that)\b", re.I)
_HYPOTHETICAL = re.compile(r"\b(if|suppose|supposing|assume|assuming|imagine|hypothetically|say|would|could|might|"
                           r"were|in case|unless|whether|provided)\b", re.I)


def _factual_premise(clause: str) -> bool:
    return bool(_FACTUAL_OPENER.match(clause)) and not _HYPOTHETICAL.search(clause)


def statements_only(source: str) -> str:
    """``source`` without its questions. In a question sentence, a clause before the last comma is kept
    when the rest is the question and the clause is a factual premise ("Since I am joining Corvid
    Analytics, what should I read?" keeps "Since I am joining Corvid Analytics"; "If I joined Google,
    what should I learn?" keeps nothing). The question itself is never a statement."""
    out = []
    for sentence in _SENTENCE.split(source):
        if not sentence.strip():
            continue
        if sentence.rstrip().endswith("?"):
            parts = _QUESTION_TAIL.split(sentence, maxsplit=1)
            if len(parts) == 2 and _factual_premise(parts[0]):
                out.append(parts[0])
            continue
        out.append(sentence)
    return "\n".join(out)


def hypothetical_questions(source: str) -> str:
    """The question sentences of ``source`` that hold a conditional or hypothetical word (_HYPOTHETICAL)."""
    return "\n".join(s for s in _SENTENCE.split(source) if s.rstrip().endswith("?") and _HYPOTHETICAL.search(s))


def from_question_only(node: dict, source: str, statements: str) -> bool:
    """True if the node's evidence is found in a question of the user and nowhere else."""
    return "evidence" in node and grounded(node, source) and not grounded(node, statements)


def talks_about_secrets(node: dict) -> bool:
    """True if the node's name or summary holds a credential value, names a credential variable,
    or says the user owns or holds a secret. Plain technical words ("JWT tokens", "password
    manager", "token limit", "PRIMARY_KEY") are not secret talk."""
    text = f"{node.get('name') or ''} {node.get('summary') or ''}"
    return bool(_CREDENTIAL.search(text) or _SECRET_VAR.search(text) or _OWNED_SECRET.search(text)
                or _USER_HOLDS_SECRET.search(text))


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


def clean_extraction(data: dict, source: str, store: "MemoryStore | None" = None,
                     withheld: "set[str] | frozenset[str]" = frozenset()) -> tuple[dict, dict[str, int]]:
    """Filtered copy of validated extraction ``data`` and how many nodes each rule dropped.

    ``withheld``: credential values the pre-filter removed from the user's text; a node that
    contains one is dropped as a secret, even if no secret pattern matches it."""
    dropped = {"ungrounded": 0, "low_value": 0, "question": 0, "secret": 0, "duplicate": 0, "known": 0}
    statements = statements_only(source)
    hypotheticals = hypothetical_questions(source)
    kept: dict[str, dict] = {}
    rank = {t: i for i, t in enumerate(TYPE_PRIORITY)}
    for n in data.get("nodes", []):
        is_me = normalize_name(n["name"]) == "me"
        fields = " ".join(str(n.get(k) or "") for k in ("name", "summary", "evidence"))
        # Any node, "Me" too, whose name, summary or evidence holds a label, a connector and a value is
        # dropped ("The password for Harbor Log is x"): losing the fact is the price.
        if (has_secret(n) or (talks_about_secrets(n) and not is_me) or _CREDENTIAL.search(fields)
                or any(len(v) >= 3 and v.lower() in fields.lower() for v in withheld)):
            dropped["secret"] += 1
            continue
        if not grounded(n, source):
            dropped["ungrounded"] += 1
            continue
        if low_value(n):
            dropped["low_value"] += 1
            continue
        question_only = from_question_only(n, source, statements)
        # The project a plain question is about is kept ("Is Harbor Log ready for Postgres?"); from a
        # hypothetical question ("If I build Trail Tracker, ...") nothing is, not even a project.
        in_hypothetical = question_only and bool(hypotheticals) and grounded(n, hypotheticals)
        if question_only and not is_me and (n["type"] not in QUESTION_ENTITY_TYPES or in_hypothetical):
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
