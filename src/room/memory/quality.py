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

import difflib
import re
import unicodedata
from typing import TYPE_CHECKING

from ..transcript import ROLE_USER, Message
from .redact import REPLACEMENT, find_secrets, redact_secrets
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
_FENCE = re.compile(r"(```|~~~).*?(?:\1|\Z)", re.S)  # ``` and ~~~ code fences
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
                 "password", "passwd", "pwd", "secret", "token", "key", "pass", "pw", "pin", "otp", "2fa code",
                 "recovery code", "session cookie", "cookie", "psk", "secret code", "seed phrase", "auth header",
                 "auth", "credentials", "mot de passe", "contraseña", "passwort", "密码", "パスワード", "jwt", "totp",
                 "hmac", "license key")
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
_WRAP = r"[\"'`*]{0,2}"  # a label's closing wrapper: "**Password** for x is", "`api_key` for x ="
_FILLER = r"(?:[ \t]+[^\s,;:=.!?]+(?:\.[^\s,;:=.!?]+)*){0,6}?"
# The connector: "=", ":", "->", "=>", an em or en dash, "is", "was", "are", "is now", "set to", "is set to",
# "equals", "is equal to", "will be", or 是 / は after a Chinese or Japanese label. A key in quotes may close
# before ":" ('"password": "x"'), as may Markdown ("**Password**: x", "`api_key`: x"); the value may sit up to
# 4 lines down ("password:\n\n  x"), also after a YAML block indicator ("password: |\n  x", "key: >-\n  x").
_CONNECT = (r"(?:[\"'`*]{0,2}[ \t]*(?:[:=]|->|=>|[\u2014\u2013])[ \t]*(?:[|>][0-9]?[+-]?[0-9]?[ \t]*(?=\n))?(?:\n[ \t]*){0,4}"
            r"|[ \t]*[是は][ \t]*"
            r"|[\"'`*]{0,2}[ \t]+(?:(?:is|was)[ \t]+equal[ \t]+to|(?:(?:is|was)[ \t]+)?set[ \t]+to|equals|will[ \t]+be|"
            r"(?:is|was|are)(?:[ \t]+now)?)"
            r"(?:[ \t]+|[ \t]*\n[ \t]*))")
# The value runs to the end of the clause, so a passphrase ("blue sky mining") is masked whole. It also
# ends at a quote, a closing bracket, or "&" before another URL parameter ("?api_key=x&v=1"); an opening
# quote before it is not part of it. There is no status-word exception: "the token is valid for one hour"
# is masked too (security over recall), and this can over-mask the rest of a clause, which is accepted.
_VALUE_END = r"""(?=[ \t]*(?:[,;"'\)\]\}]|&(?=[^\s&=]+=)|[.!?](?:\s|$)|\n|$))"""
def _balanced(open_: str, close: str, depth: int = 3) -> str:
    """A span wrapped in ``open_`` ... ``close``, nested up to ``depth`` levels ("(outer(inner))")."""
    o, c = re.escape(open_), re.escape(close)
    inner = rf"[^{o}{c}\n]*"
    for _ in range(depth - 1):
        inner = rf"(?:[^{o}{c}\n]|{o}{inner}{c})*"
    return rf"{o}{inner}{c}"


# A quoted value runs to its matching closing quote, backslash escapes included, so , ; ) and \" inside it
# do not end it ('{"password": "alpha,beta99"}'); only the inside is masked. A value wrapped in ( ) < > [ ] or
# { }, nested up to 3 levels ("(outer(inner))"), or written after "=" signs ("==garnet55"), is masked with its
# wrapper; [ and { followed by a quote open JSON, not a wrapper. Otherwise the value is unquoted: it
# never starts with "=", so "my token QWxh==" is not read as filler "QWxh", connector "=", value "=".
_CLAUSE = ("(?:"
           r'"(?P<{name}q1>(?:\\.|[^"\\\n])+)"'
           r"|'(?P<{name}q2>(?:\\.|[^'\\\n])+)'"
           r"|`(?P<{name}q3>(?:\\.|[^`\\\n])+)`"
           rf"|(?P<{{name}}w1>{_balanced('(', ')')})|(?P<{{name}}w2>{_balanced('<', '>')})"
           rf"|(?P<{{name}}w4>(?=\[(?![ \t]*[\"']))" + _balanced('[', ']') + ")"
           rf"|(?P<{{name}}w5>(?=\{{(?![ \t]*[\"']))" + _balanced('{', '}') + ")"
           r"""|(?P<{name}w3>=+[^\s,;"'\)\]\}=][^\s,;"'\)\]\}]*)"""
           r"""|["'`]?(?P<{name}>[^\s,;"'`\)\]\}\{\[\(<=][^,;"'`\)\]\}\n]*?)""" + _VALUE_END + ")")
def _flag_value(name: str) -> str:
    """A command-line flag's value: "double quoted", 'single quoted' (the inside is masked) or bare."""
    return (rf"""(?:"(?P<{name}q>(?:\\.|[^"\\\n])+)"|'(?P<{name}s>(?:\\.|[^'\\\n])+)'"""
            rf"""|(?P<{name}>[^\s"']+))""")


_TOKEN_VALUE = r"[A-Za-z0-9._~+/=-]*[A-Za-z0-9_~+/=-]"  # a header token; a trailing "." ends the sentence
_B64_END = r"(?![A-Za-z0-9+/=_-])"
_CREDENTIAL = re.compile(
    rf"{_LABEL}{_WRAP}{_FILLER}{_CONNECT}{_CLAUSE.replace('{name}', 'a')}"   # the password for Harbor Log is x; KEY=x
    rf"|{_LABEL}{_FILLER}[ \t]+(?P<h>[A-Za-z0-9+/]{{4,}}={{1,2}}){_B64_END}"  # my token QWxhZGRpbjpvcGVu==
    rf"|\bauthorization[\"']?\s*:\s*[\"']?(?:bearer\s+|basic\s+|token\s+)?(?P<d>{_TOKEN_VALUE})"  # Authorization: Bearer x
    r"|\bbearer\s+(?P<e>(?=[A-Za-z0-9._~+/=-]{8,})" + _TOKEN_VALUE + ")"
    r"|\b(?P<sg>SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})"                # SendGrid: the whole token
    r"|\b(?P<gl>glpat-[A-Za-z0-9_-]{20,})|\b(?P<wh>whsec_[A-Za-z0-9+/=_]{16,})"  # GitLab PAT, webhook secret
    r"|[?&]sig=(?P<sas>[A-Za-z0-9%+/=]{8,})"                                    # Azure SAS signature
    # curl -u [user]:PASS / --user, attached (-ubot:x) or quoted ("bot:a b"); -b / --cookie 'name=VALUE'
    r'''|(?<!\S)(?-i:-u|--user)(?:[ \t]+|=)?"[^":\n]*:(?P<cuq>[^"\n]+)"'''
    r"""|(?<!\S)(?-i:-u|--user)(?:[ \t]+|=)?'[^':\n]*:(?P<cus>[^'\n]+)'"""
    r"""|(?<!\S)(?-i:-u|--user)(?:[ \t]+|=)?[^\s:"']*:(?P<cu>[^\s"']+)"""
    r"""|(?<!\S)(?-i:-b|--cookie)(?:[ \t]+|=)?"[^=;"\n]+=(?P<ckq>[^;"\n]+)"""
    r"""|(?<!\S)(?-i:-b|--cookie)(?:[ \t]+|=)?'[^=;'\n]+=(?P<cks>[^;'\n]+)"""
    r"""|(?<!\S)(?-i:-b|--cookie)(?:[ \t]+|=)?[^\s=;"']+=(?P<ck>[^\s;"']+)"""
    # redis-cli -a PASS, mysql -pPASS, docker login -p PASS, --password=PASS, sshpass -p PASS: each value may be
    # quoted ('x', "a b") and attached (-p'x') or spaced
    rf"""|\bredis-cli\b[^\n]*?(?<!\S)(?-i:-a)[ \t]+{_flag_value('cr')}"""
    rf"""|\bmysql(?:dump|admin)?\b[^\n]*?(?<!\S)(?-i:-p){_flag_value('cm')}"""
    rf"""|\bdocker[ \t]+login\b[^\n]*?(?<!\S)(?-i:-p)(?:[ \t]+)?{_flag_value('cd')}"""
    rf"""|(?<!\S)--password(?:=|[ \t]+){_flag_value('cf')}"""
    rf"""|\bsshpass\b[^\n]*?(?<!\S)(?-i:-p)(?:[ \t]+)?{_flag_value('cs')}"""
    # "with password X" / "using token X": a quoted value, or an unquoted one with a digit or symbol
    # ("with password protection" stays)
    rf"""|\b(?:with|using)[ \t]+(?:(?:the|a|an|my|our)[ \t]+)?{_LABEL}[ \t]+"""
    r"""(?:"(?P<wuq>(?:\\.|[^"\\\n])+)"|'(?P<wus>(?:\\.|[^'\\\n])+)'|`(?P<wub>[^`\n]+)`"""
    r"""|(?P<wu>(?=[^\s,;"'`]*[^A-Za-z\s,;"'`])[^\s,;"'`]*[^\s,;"'`.!?]))"""
    # login: user/PASS (only the part after "/"; a bare login is a user name, not a secret)
    rf"""|\blogin{_FILLER}{_CONNECT}["']?[^\s/,;"']+[ \t]*/[ \t]*(?P<lg>[^\s,;"']*[^\s,;"'.!?])"""
    # "changed / set / updated / reset / rotated the <label> to <value>"
    rf"""|\b(?:changed|set|updated|reset|rotated)[ \t]+(?:(?:the|my|our|their|your)[ \t]+)?{_LABEL}{_FILLER}[ \t]+to"""
    rf"""[ \t]+{_CLAUSE.replace('{name}', 'ct')}"""
    # "<label>'s <value>" and "<label> as <value>": only a value with a digit or symbol ("the token's expiry")
    rf"""|{_LABEL}(?:'s|[ \t]+as)[ \t]+(?P<ps>(?=[^\s,;"']*[^A-Za-z\s,;"'])[^\s,;"']*[^\s,;"'.!?])"""
    # a glued upper-case variable name ending in PASSWORD / PASSWD / SECRET / TOKEN / KEY (PGPASSWORD=x)
    rf"""|(?-i:(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:PASSWORD|PASSWD|SECRET|TOKEN|KEY))[ \t]*=[ \t]*{_flag_value('ev')}"""
    # a JSON or Python list under a plural key: every quoted item ('"secrets": ["a", "b"]')
    r"""|(?<![A-Za-z0-9])["']?(?:[A-Za-z0-9]+_)*(?:secrets|passwords|passwds|tokens|keys|api[_-]?keys|access[_-]?keys"""
    r"""|credentials|passphrases)["']?[ \t]*[:=][ \t]*\[(?P<arr>[^\]\n]*)\]"""
    # XML: <password>x</password>
    rf"""|<{_LABEL}>(?P<xml>[^<\n]+)</"""
    # Basic dXNlcjpw...: 8+ characters with a digit or "=", or 12+ with an uppercase letter after the first and a
    # lowercase one ("Basic YWFhOmFhYWFhYWFh"; "Basic Authentication" stays)
    rf"|\bbasic\s+(?P<f>(?:(?=[A-Za-z0-9+/]*[0-9=])[A-Za-z0-9+/]{{8,}}"
    rf"|(?-i:(?=[A-Za-z0-9+/][A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z]))[A-Za-z0-9+/]{{12,}})={{0,2}}){_B64_END}"
    r"|\b[a-z][a-z0-9+.-]*://(?P<u>[^\s/@:\"'<>]*:[^\s/@\"'<>]+)(?=@)"  # postgres://admin:x@host (user:pass)
    rf"|(?<![A-Za-z0-9+/=_-])(?P<g>[A-Za-z0-9+/]{{32,}}={{0,2}}){_B64_END}",  # a 32+ char token, checked below
    re.I)

# Token shapes are also matched in a second, independent pass, so a token that a label match took as a
# filler word ("Webhook secret whsec_... is set") is still masked.
_TOKEN_SHAPES = re.compile(
    r"\b(?P<sg>SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})|\b(?P<gl>glpat-[A-Za-z0-9_-]{20,})"
    r"|\b(?P<wh>whsec_[A-Za-z0-9+/=_]{16,})|\b(?P<pp>pypi-[A-Za-z0-9_-]{16,})|\b(?P<hv>hvs\.[A-Za-z0-9_-]{20,})"
    r"|(?<![A-Za-z0-9_.-])(?P<dc>[A-Za-z0-9_-]{24,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,64})(?![A-Za-z0-9_-])"
    rf"|(?<![A-Za-z0-9+/=_-])(?P<g>[A-Za-z0-9+/]{{32,}}={{0,2}}){_B64_END}")

# Matching runs on a normalized copy (NFKC, so fullwidth : and = become ASCII; zero-width characters
# removed; every Unicode space a space; a few Cyrillic lookalikes as Latin letters). Spans map back to the
# original, which is what gets masked; the normalized copy is never stored.
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff")
_LOOKALIKE = {"\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x",
              "\u0410": "A", "\u0415": "E", "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0425": "X"}


def _normalized(text: str) -> tuple[str, "list[int] | range"]:
    """The normalized copy of ``text`` and, for each of its characters, the index it came from."""
    if text.isascii():  # nothing to normalize; the fast path for almost every message
        return text, range(len(text))
    out, origin = [], []
    for i, ch in enumerate(text):
        if ch in _ZERO_WIDTH:
            continue
        norm = _LOOKALIKE.get(ch) or unicodedata.normalize("NFKC", ch)
        for c in norm:
            out.append(" " if unicodedata.category(c) == "Zs" else c)
            origin.append(i)
    return "".join(out), origin


def _looks_random(token: str) -> bool:
    return any(c.isupper() for c in token) and any(c.islower() for c in token) and any(c.isdigit() for c in token)
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


def _credential_matches(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, normalized value, kind) of every credential value in ``text``; start and end index
    the original text."""
    norm, origin = _normalized(text or "")
    found = []
    for m in _CREDENTIAL.finditer(norm):
        kind = next(k for k, v in m.groupdict().items() if v)
        value = m.group(kind)
        if value == "[REDACTED]" or (kind == "g" and not _looks_random(value)):
            continue
        start, end = m.span(kind)
        if kind == "arr":  # each quoted item of the list, not the list
            for item in re.finditer(r'"((?:\\.|[^"\\])+)"|\'((?:\\.|[^\'\\])+)\'', value):
                g = 1 if item.group(1) is not None else 2
                s, e = start + item.start(g), start + item.end(g)
                if item.group(g) != "[REDACTED]":
                    found.append((origin[s], origin[e - 1] + 1, item.group(g), kind))
            continue
        found.append((origin[start], origin[end - 1] + 1, value, kind))
    for m in _TOKEN_SHAPES.finditer(norm):
        kind = next(k for k, v in m.groupdict().items() if v)
        if kind == "g" and not _looks_random(m.group(kind)):
            continue
        start, end = origin[m.start(kind)], origin[m.end(kind) - 1] + 1
        if not any(s <= start and end <= e for s, e, _, _ in found):
            found.append((start, end, m.group(kind), kind))
    return sorted(found)


def credential_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every credential value in ``text``, overlapping spans merged. These patterns drive
    the prompt mask, the withheld values and the node filter; withheld_values also adds what redact_secrets
    masks in the extraction prompt (see redact_masked_substrings)."""
    merged: list[tuple[int, int]] = []
    for s, e, _, _ in _credential_matches(text):  # sorted; overlapping spans are merged
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


def has_credential(text: str) -> bool:
    return bool(credential_spans(text))


def _holds_withheld(fields: str, withheld: "set[str] | frozenset[str]") -> bool:
    """True if ``fields`` (as written, or normalized) contains a withheld value of 3+ characters."""
    folded = (fields.lower(), _normalized(fields)[0].lower())
    return any(len(v) >= 3 and any(v.lower() in f for f in folded) for v in withheld)


def mask_credentials(text: str) -> str:
    """``text`` with each credential value replaced by [REDACTED]; the rest of the line is kept."""
    text = text or ""
    out, last = [], 0
    for start, end in credential_spans(text):
        out += [text[last:start], "[REDACTED]"]
        last = end
    return "".join(out) + text[last:]


def credential_values(text: str) -> set[str]:
    """Credential values written in ``text`` ("hunter2" in "my password is hunter2"), exactly as masked."""
    return {(text or "")[s:e] for s, e in credential_spans(text)}


def scrub_withheld(text: str, withheld: "set[str] | frozenset[str]") -> str:
    """``text`` with credential values masked and every withheld value of 3+ characters replaced by
    [REDACTED], longest first; for text that does not come from the user, such as an error message that
    quotes the model's output."""
    text = mask_credentials(text)
    for value in sorted((v for v in withheld if len(v) >= 3), key=len, reverse=True):
        text = re.sub(re.escape(value), "[REDACTED]", text, flags=re.I)
    return text


def redact_masked_substrings(text: str) -> set[str]:
    """The substrings of ``text`` that redact_secrets replaces, found by comparing the text before and after
    it (redact.py is not changed): its output, split at each [REDACTED], gives the kept pieces in order, and
    what lies between them in ``text`` was masked."""
    text = text or ""
    after = redact_secrets(text)
    if after == text:
        return set()
    pieces = after.split(REPLACEMENT)
    m = re.fullmatch("(.+?)".join(re.escape(p) for p in pieces), text, re.S)
    if m:
        found = set(m.groups())
    else:  # a piece repeats in a way the split cannot place; fall back to a character diff
        ops = difflib.SequenceMatcher(None, text, after, autojunk=False).get_opcodes()
        found = {text[i1:i2] for tag, i1, i2, _, _ in ops if tag in ("replace", "delete")}
    return {v for v in found if v and v != REPLACEMENT}


def premask_text(text: str) -> str:
    """The text the mask runs on in clean_message: code blocks cut, "> " quote marks removed."""
    return _QUOTE_MARK.sub("", _FENCE.sub("\n", text or ""))


def withheld_values(text: str) -> set[str]:
    """Every credential value the mask takes out of ``text`` (found on the raw text and on the text
    clean_message masks, with the same rules), every substring redact_secrets masks in the extraction prompt,
    plus each word of them that the model never sees, so a node that repeats only part of an over-masked
    clause is dropped too."""
    prepared = premask_text(text)
    values = set()
    for source in (text or "", prepared):
        for s, e, norm, kind in _credential_matches(source):
            values |= {source[s:e], norm}
            if kind == "u":  # the password part of user:pass on its own
                values.add(norm.split(":", 1)[1])
            if "\\" in norm:  # a quoted value also as written unescaped
                values.add(re.sub(r"\\(.)", r"\1", norm))
    # What redact_secrets masks in the extraction prompt is withheld too. The prompt is
    # redact_secrets(clean_message(text)); the masked text before clean_message drops lines is checked too.
    for source in (clean_message(text), mask_credentials(prepared)):
        values |= redact_masked_substrings(source)
    visible = {w.strip(".,;:!?()<>='\"`").lower() for w in mask_credentials(prepared).split()}
    words = {w.strip(strip) for v in values for w in v.split() for strip in (".,;:!?()'\"", ".,;:!?()<>='\"`")}
    # and each piece of 5+ characters between punctuation ("post77" in pre\"post77, the id in SG.id.secret)
    pieces = {p for v in values for p in re.split(r"[\s\"'\\,;:()<>\[\]{}=/@&.`]+", v) if len(p) >= 5}
    # inside a bracket-wrapped value, every piece of 3+ characters between brackets ("y179" in "<x<y179>>")
    pieces |= {p for v in values if v[:1] in "(<[{" for p in re.split(r"[()<>\[\]{}]+", v) if len(p) >= 3}
    return (values | {w for w in words if len(w) >= 3 and w.lower() not in visible}
            | {p for p in pieces if p.lower() not in visible})


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
    text = mask_credentials(premask_text(text))
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
    return bool(has_credential(text) or _SECRET_VAR.search(text) or _OWNED_SECRET.search(text)
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
        if (has_secret(n) or (talks_about_secrets(n) and not is_me) or has_credential(fields)
                or _holds_withheld(fields, withheld)):
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
