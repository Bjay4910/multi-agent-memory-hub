"""The working layer: raw, temporary, redacted session notes.

Memory has two layers:

- **Working** (this module): what was said in a session, kept as captured
  (after redaction), one JSON file per session under
  ``~/.agent-room/working/``. Notes expire after ``DEFAULT_TTL_DAYS`` and are
  never sent to an extractor or the graph automatically.
- **Long-term** (``memory.db``): curated nodes and edges. The only way from
  working to long-term is ``room working promote <id>``, which runs the normal
  extraction pipeline.

Safety: the directory is 0700 and files 0600; the store refuses to live in a git
work tree, inside this repository or near the key folder (the same rules as the
vault). Text is redacted before every write, and a note in which a secret
pattern still matches is not written at all. Reading functions only open files
for reading: they never create, change or delete anything (expired notes are
just skipped until ``purge``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .. import paths
from ..fsutil import atomic_write_json
from ..transcript import ROLE_AGENT, ROLE_USER, Message, new_session_id
from .paths import RepoWriteError, ensure_outside_repo
from . import quality
from .redact import REPLACEMENT, find_secrets, redact_secrets

SOURCES = ("claude-code", "antigravity")
_ASSISTANT_AGENT = {"claude-code": "claude", "antigravity": "gemini"}  # agent name for the assistant's turns
DEFAULT_TTL_DAYS = 14
MAX_NOTE_CHARS = 8000  # total turn text kept per note; the oldest turns are dropped first
MIN_USER_TURNS = 2
MIN_CHARS = 400
ID_RE = re.compile(r"[a-z][a-z-]*-\d{8}T\d{6}Z-[0-9a-f]{6}")
FORMAT_VERSION = 1


class WorkingSafetyError(RuntimeError):
    """The working store would live somewhere unsafe; nothing was written."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def working_dir() -> Path:
    return paths.room_home() / "working"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# -- location and writing ------------------------------------------------------------

def check_location(path: Path) -> Path:
    """Refuse a git work tree, this repository, and anything overlapping the key folder."""
    from .vault import VaultSafetyError, _check_not_keys_dir, git_work_tree

    path = Path(path).expanduser().resolve()
    try:
        _check_not_keys_dir(path)
    except VaultSafetyError:
        raise WorkingSafetyError(f"refusing to keep working notes at {path}: it overlaps the key folder") from None
    tree = git_work_tree(path)
    if tree is not None:
        raise WorkingSafetyError(f"refusing to keep working notes in a git work tree ({tree}); "
                                 "transcripts could be committed. Set AGENT_ROOM_HOME outside any repository.")
    try:
        ensure_outside_repo(path)
    except RepoWriteError as exc:
        raise WorkingSafetyError(str(exc)) from None
    return path


def ensure_dir() -> Path:
    directory = check_location(working_dir())
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    """One writer at a time across processes (two sessions ending together, or a resume)."""
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def redact_for_note(text: str, withheld: "set[str] | frozenset[str]" = frozenset()) -> str:
    """The masking every working-note text gets, in extraction's order: the extraction filter's credential mask
    on the raw text first (quality.mask_credentials), then redact_secrets (piece by piece between markers), then
    every withheld value of this text and of ``withheld`` replaced by [REDACTED] (see _withheld and _scrub)."""
    return _scrub(_masked(text), set(withheld) | _withheld(text))


def _masked(text: str) -> str:
    """The strong mask on the raw text, then redact_secrets (so redact.py never half-masks a token first).
    redact_secrets runs on each piece between [REDACTED] markers: run over a marker it reads
    "api_key=[REDACTED]&page=2" as one value and swallows "&page=2"; a masked value cannot span a marker."""
    masked = _mask_credentials(text)
    return REPLACEMENT.join(redact_secrets(piece) for piece in masked.split(REPLACEMENT))


_FLAGS = frozenset({"on", "off", "true", "false", "yes", "no", "none", "null", "nil", "enabled", "disabled"})


def _is_flag(value: str) -> bool:
    """A setting, not a secret: the extraction filter's value starts with a flag word and every word after it is a
    plain word of letters only ("foreign_keys = ON at connection setup", "password: false done"). "on SzFDVb2" is
    not a flag clause."""
    words = [w.strip(_EDGE) for w in value.split()]
    return bool(words) and words[0].lower() in _FLAGS and all(re.fullmatch(r"[A-Za-z]*", w) for w in words[1:])


# Which of the extraction filter's matches the working layer keeps. Its labels "key", "token", "auth" and "secret" are
# weak on their own: they name everyday things ("the key point is", "token expiry", "auth module", "secret weapon").
# Every other label of quality.SECRET_LABELS is strong, as is a variable name (a label with a "NAME_" prefix:
# GITHUB_TOKEN, api_key, CLIENT_SECRET) and a weak label after api / access / auth / bearer / secret / refresh
# ("access token", "refresh token", "auth token").
_WEAK_LABELS = frozenset({"key", "token", "auth", "secret"})
_STRONG_BEFORE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:api|access|auth|bearer|secret|refresh)[ \t_-]?$")
# quality's label pattern with the longest labels tried first, so "secret-code" is read as "secret code", not "secret"
_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:[A-Za-z0-9]+_)*(?:"
    + "|".join(l.replace(" ", "[ _-]?") for l in sorted(quality.SECRET_LABELS, key=len, reverse=True))
    + r")|(?:[A-Za-z0-9]+_)+pat)(?![A-Za-z0-9])" + quality._COMPOUND, re.I)
# A strong label directly followed by one of these nouns names a thing, not a credential ("API key header is named
# X-Api-Key", "password field is required"); it is then read as a weak label followed by a noun.
_THING = re.compile(r"(?i)[ \t_-]*(?:headers?|fields?|names?|formats?|lengths?|prefix(?:es)?|columns?|params?"
                    r"|parameters?|variables?|settings?|rules?|requirements?|hints?|prompts?|inputs?|flows?|resets?"
                    r"|rotations?|expiry|strength|types?|files?|modules?)\b")
# A weak label directly followed by another word is a compound noun ("key point", "auth module", "secret santa"),
# unless that word is one of these (the filler of a real credential sentence: "the token of my admin account is",
# "the secret for Harbor Log equals", "token on staging =", "token as x").
_FILLER_START = frozenset("""for of on in at to from with by as via under used we i you my our your his her their the
a an this that these those here now today again also still currently then too is are was were be equals""".split())
# (a [REDACTED] marker counts as a word, so the refusal check, which reads the masked text, decides as the mask did)
_NEXT_WORD = re.compile(r"[\"'`*]{0,2}[ \t_-]+(\[REDACTED\]|[A-Za-z]+)")
# kinds of quality match that come from a label written before the value (the rest are fixed forms: token shapes,
# Authorization headers, curl -u, cookies, command-line flags, user:pass in URLs, upper-case variable names, and
# "arr", a JSON list under a plural key)
_LABELLED = ("a", "h", "wu", "ct", "ps", "xml")


# A snake_case label is a credential name only with one of these prefixes before _key, _token, _secret or _password
# (api_key, client_secret, access_token, my_api_key); foreign_key, primary_key, sort_key or cache_key are not.
# An upper-case variable name (GITHUB_TOKEN, DB_PASSWORD) is always one.
_CREDENTIAL_PREFIXES = frozenset("""api access secret private auth bearer client session signing encryption ssh jwt
aws refresh webhook""".split())
_CREDENTIAL_SUFFIXES = frozenset({"key", "token", "secret", "password"})


def _openers(text: str) -> "dict[int, tuple[str, re.Match]]":
    """For each labelled quality match in ``text``: the original start of its value -> (normalized text, the label
    that opened the match). Built from quality's own pattern, so it is the label of that match, not another label
    in its filler ("the password for auth module is x" opens with "password", not "auth")."""
    norm, origin = quality._normalized(text)
    out = {}
    for m in quality._CREDENTIAL.finditer(norm):
        kind = next((k for k, v in m.groupdict().items() if v), None)
        if kind is None or not kind.startswith(_LABELLED) or kind == "arr" or m.start(kind) >= len(origin):
            continue
        label = _LABEL_RE.search(norm, m.start(), m.start(kind))
        if label:
            out[origin[m.start(kind)]] = (norm, label, m.start(kind))
    return out


def _label_before(text: str, start: int, openers: "dict | None" = None) -> "tuple[str, str] | None":
    """(label, kind) of the label that opened the quality match whose value starts at ``start``: kind "strong",
    "weak" (a weak label on its own) or "noun" (a label followed by a noun: "key point", "API key header"; also a
    snake_case name that is not a credential name: "foreign_key")."""
    found = (openers if openers is not None else _openers(text)).get(start)
    if found is None:
        return None
    norm, m, value_at = found
    # a weak label directly followed by another label is read as that label ("auth token" -> "token" after "auth")
    if m.group(0).lower() in _WEAK_LABELS:
        nxt = _LABEL_RE.match(norm, m.end() + len(norm[m.end():value_at]) - len(norm[m.end():value_at].lstrip(" \t_-")),
                              value_at)
        if nxt:
            m = nxt
    raw = m.group(0)
    label = re.sub(r"[ _-]+", " ", raw.lower()).strip()
    if "_" in raw.strip("_"):
        if not re.search(r"[a-z]", raw):
            return label, "strong"  # an upper-case variable name: GITHUB_TOKEN, DB_SECRET
        parts = raw.lower().strip("_").split("_")
        credential = parts[-1] in _CREDENTIAL_SUFFIXES and parts[-2] in _CREDENTIAL_PREFIXES
        return label, ("strong" if credential else "noun")
    if label not in _WEAK_LABELS:
        return label, ("noun" if _THING.match(norm, m.end(), value_at) else "strong")
    if _STRONG_BEFORE.search(norm[:m.start()]):
        return label, "strong"  # access token, refresh token
    nxt = _NEXT_WORD.match(norm, m.end(), value_at)  # a word between the label and the value, never the value
    return label, ("noun" if nxt and nxt.group(1).lower() not in _FILLER_START else "weak")


def _keep(text: str, start: int, value: str, kind: str, openers: "dict | None" = None) -> bool:
    """Whether the working layer keeps a quality match:
    - never a flag value (see _is_flag);
    - always a fixed form (a token shape, a header, a command-line flag, user:pass, ...);
    - under a strong label, always;
    - under a weak label on its own ("the token is x", "using token x", "secret: x"), when the value is one word,
      has a word with letters and digits ("h5se today"), or has a secret-shaped word or piece;
    - under a label followed by a noun ("key point", "secret weapon", "API key header"), only when the value has a
      secret-shaped word or piece."""
    if _is_flag(value):
        return False
    if kind == "arr" or not kind.startswith(_LABELLED):
        return True
    found = _label_before(text, start, openers)
    if found is None or found[1] == "strong":
        return True
    pieces = {value} | set(re.split(r"\s+", value)) | set(quality._PIECE_SPLIT.split(value))
    if any(_secret_shaped(p.strip(_EDGE)) for p in pieces if p.strip(_EDGE)):
        return True
    if found[1] == "noun":
        return False
    words = [w.strip(_EDGE) for w in value.split()]
    return len(words) == 1 or any(re.search(r"[A-Za-z]", w) and re.search(r"\d", w) for w in words)


def _credential_spans(text: str) -> list[tuple[int, int]]:
    """quality.credential_spans, minus what the working layer does not keep (see _keep); overlapping spans merged."""
    merged: list[tuple[int, int]] = []
    openers = _openers(text)
    for s, e, value, kind in quality._credential_matches(text):
        if not _keep(text, s, value, kind, openers):
            continue
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


def _mask_credentials(text: str) -> str:
    out, last = [], 0
    for s, e in _credential_spans(text):
        out += [text[last:s], REPLACEMENT]
        last = e
    return "".join(out) + text[last:]


def _regions(text: str) -> set[str]:
    """The pieces of ``text`` this pipeline masks: the extraction filter's spans (code blocks included), and
    what redact_secrets masks between them."""
    regions = {text[s:e] for s, e in _credential_spans(text)}
    for piece in _mask_credentials(text).split(REPLACEMENT):
        regions |= quality.redact_masked_substrings(piece)
    return {r for r in regions if r and r != REPLACEMENT}


_EDGE = ".,;:!?()[]{}<>'\"`"


def _secret_shaped(value: str) -> bool:
    """Whether a masked value is scrubbed from the note's other turns. Judged on the normalized value (NFKC,
    zero-width characters removed, Cyrillic lookalikes read as Latin), so "qvwr\u0410mzkp" is read as "qvwrAmzkp".

    - a token or key form always counts (quality's token shapes, or a redact.py pattern: sk-, ghp_, AKIA, a JWT,
      a PEM block), even with spaces in it;
    - otherwise one word (no whitespace) of 8+ characters that is not ordinary (see _ordinary), and that has a
      letter with a digit or a symbol (not "_"), or is 8+ digits, or has letters and digits or symbols only,
      or mixes upper and lower case without being a plain CamelCase identifier, or had a Cyrillic lookalike or
      zero-width character inside Latin letters (written to hide it).
    Never phrases, plain words, numbers under 8 digits, or values of 7 or fewer characters."""
    v = quality._normalized(value)[0].strip()
    if len(v) <= 7:
        return False
    if quality._TOKEN_SHAPES.fullmatch(v) or find_secrets(v):
        return True
    if re.search(r"\s", v) or _ordinary(v):
        return False
    if re.search(r"\d", v) or re.search(r"[^\w]", v):
        return True  # letters with digits or symbols, 8+ digits, or digits with symbols (dates, IPs and versions excluded)
    if value.strip() != v and re.search(r"[A-Za-z]", v):
        return True  # a lookalike or zero-width character hidden in a Latin word
    return bool(re.search(r"[A-Z]", v[1:])) and bool(re.search(r"[a-z]", v)) and not _identifier(v)


_DATE = re.compile(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})")
_ORDINARY = re.compile(
    r"v?\d+(?:\.\d+){1,3}(?:-[0-9a-z.]+)?(?:\+[0-9a-z.]+)?"  # versions (2.4.1-beta) and IPv4 addresses
    r"|(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?"  # 127.0.0.1:5432
    r"|[a-z0-9][a-z0-9.-]*:\d{1,5}"  # localhost:8080, db.internal:5432
    r"|https?://[^\s@:/]+(?::\d{1,5})?(?:/[^\s@]*)?"  # a URL without a user:password part
    r"|[A-Za-z]+(?:-[A-Za-z]+)+"  # hyphenated words of letters only, any case: read-only, Not-Found, X-Request-Id
    r"|[A-Za-z]+'[A-Za-z]{1,2}"  # contractions: shouldn't, wouldn't
    r"|[A-Za-z][a-z]*(?:/[A-Za-z][a-z]*)+|[A-Z]+(?:/[A-Z]+)+")  # words joined by "/": True/False, input/output


def _ordinary(v: str) -> bool:
    """A date, version, IP, host:port, plain URL, hyphenated lowercase words, a contraction, slash-joined words, or
    a dotted code expression (str.lower, os.path.join) whose parts are plain words or identifiers."""
    m = _DATE.fullmatch(v)
    if m:
        try:
            datetime(int(m[1]), int(m[2]), int(m[3]))
            return True
        except ValueError:
            pass
    if _ORDINARY.fullmatch(v):
        return True
    parts = v.removesuffix("()").split(".")
    return len(parts) > 1 and all(re.fullmatch(r"[a-z_]+|[A-Z_]+", p) or _identifier(p) for p in parts)


def _identifier(v: str) -> bool:
    """A plain camelCase or PascalCase identifier made of word-like parts (KeyError, defaultValue, maxStrengths):
    each part 2+ letters with a vowel. "qvwrAmzkp" and "kAmezovqr" are not."""
    if not re.fullmatch(r"[a-z]+(?:[A-Z][a-z]+)+|(?:[A-Z][a-z]+)+", v):
        return False
    return all(len(p) >= 2 and re.search(r"[aeiouy]", p)
               for p in (s.lower() for s in re.findall(r"[A-Z]?[a-z]+", v)))


def _withheld(text: str) -> set[str]:
    """Secret-shaped values to scrub from every turn of a note, from the raw ``text`` of one turn: each masked
    region whole, its unescaped form, its user:PASS and login user/PASS parts, the pieces between brackets, and
    its words and pieces, kept only if _secret_shaped. The marker itself is never a value."""
    values: set[str] = set()
    for region in _regions(text):
        for form in {region, re.sub(r"\\(.)", r"\1", region)}:
            form = form.replace(REPLACEMENT, " ").strip()
            if not form:
                continue
            candidates = {form}
            if ":" in form and not re.search(r"\s", form):
                candidates.add(form.split(":", 1)[1])
            if "/" in form:
                candidates.add(form.rsplit("/", 1)[1])
            if form[0] in "(<[{":
                candidates |= set(re.split(r"[()<>\[\]{}]+", form))
            candidates |= set(re.split(r"\s+", form)) | set(quality._PIECE_SPLIT.split(form))
            values |= {c.strip().strip(_EDGE) for c in candidates}
    return {v for v in values if v and v != REPLACEMENT and _secret_shaped(v)}


_MARKER = re.compile(re.escape(REPLACEMENT))


def _scrub(text: str, values: "set[str] | frozenset[str]") -> str:
    """Replace each value (2+ characters) by [REDACTED], case-insensitively, on the normalized text (quality's
    NFKC, zero-width removal and folding of its Cyrillic lookalikes) mapped back to the original characters. A
    match counts when at least one of its ends is not glued to a letter or digit (underscore and other symbols
    count as a boundary: "tok_<s>_end", "xx@<s>yy"); a match that overlaps an existing marker is skipped."""
    wanted = sorted({n for n in (quality._normalized(v)[0].strip() for v in values) if len(n) >= 2}, key=len, reverse=True)
    if not text or not wanted:
        return text
    norm, origin = quality._normalized(text)
    markers = [m.span() for m in _MARKER.finditer(text)]
    spans = []
    for value in wanted:
        for m in re.finditer(re.escape(value), norm, re.I):
            left = m.start() == 0 or not norm[m.start() - 1].isalnum() or not value[0].isalnum()
            right = m.end() == len(norm) or not norm[m.end()].isalnum() or not value[-1].isalnum()
            if not (left or right):
                continue
            s, e = origin[m.start()], quality._end(text, origin, m.end())
            if not any(s < me and ms < e for ms, me in markers):
                spans.append((s, e))
    if not spans:
        return text
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, last = [], 0
    for s, e in merged:
        out += [text[last:s], REPLACEMENT]
        last = e
    return "".join(out) + text[last:]


def tag(project: str) -> str:
    """A project name made safe for a note: letters, digits, space, "_", "." and "-", at most 40 characters."""
    return re.sub(r"[^A-Za-z0-9 _.\-]+", "-", project).strip(" -.")[:40] or "unknown"


def _still_secret(text: str) -> bool:
    """A secret pattern still matches: redact.py's (checked on each piece between [REDACTED] markers, as
    redact_secrets is run, so a label followed by a marker is not read as a labelled value), or the extraction
    filter's (stronger) one."""
    # "Authorization: Bearer [REDACTED]" is masked already; the extraction pattern would read "Bearer" as the value.
    text = _SCHEME_THEN_MARKER.sub(REPLACEMENT, text)
    # the cut notice is this module's own text, not the user's: dropped, so "token: " before it is a label whose
    # value was cut away, not a label with the value "[... 12 characters cut]"
    text = _CUT_NOTICE.sub("", text)
    return any(find_secrets(piece) for piece in text.split(REPLACEMENT)) or bool(_credential_spans(text))


_SCHEME_THEN_MARKER = re.compile(r"(?i)\b(?:bearer|basic|token)\s+" + re.escape(REPLACEMENT))
_CUT_NOTICE = re.compile(r"\n\[\.\.\. [\d,]+ characters cut\]")


def _cut(text: str, limit: int) -> int:
    """Where to cut ``text`` to at most ``limit`` characters: before a [REDACTED] marker the limit falls inside,
    so a stored note never holds a partial marker ("[REDACTE")."""
    for m in _MARKER.finditer(text, max(0, limit - len(REPLACEMENT))):
        if m.start() < limit < m.end():
            return m.start()
    return limit


def _raw(item: dict) -> str:
    """The text as captured, before any masking (the capture paths pass it as "raw"; it is never written)."""
    return item["raw"] if isinstance(item.get("raw"), str) else item["text"]


def _clean_turn(turn: dict, withheld: "set[str] | frozenset[str]" = frozenset()) -> dict | None:
    """Mask the turn's raw text, scrub every withheld value of the note, and only then cut it (to ``cut``
    characters if the capture asked for that, and to MAX_NOTE_CHARS), so a secret across a cut is masked whole."""
    role = turn.get("role")
    if role not in ("user", "assistant") or not isinstance(turn.get("text"), str):
        return None
    text = _scrub(_masked(_raw(turn).strip()), withheld)
    limit = turn.get("cut")
    if isinstance(limit, int) and len(text) > limit:
        at = _cut(text, limit)
        text = text[:at] + f"\n[... {len(text) - at:,} characters cut]"
    text = text[:_cut(text, MAX_NOTE_CHARS)]
    if not text:
        return None
    out = {"role": role, "ts": str(turn.get("ts") or "")[:40], "text": text}
    if turn.get("key"):
        out["key"] = _hash(str(turn["key"]))  # lets a later update rebuild this turn from the raw transcript text
    return out


def upsert(source: str, source_key: str, project: str, items: list[dict], *,
           min_user_turns: int = MIN_USER_TURNS, min_chars: int = MIN_CHARS) -> tuple[str, dict | None]:
    """Add a session's turns to its working note, storing only turns not seen before.

    ``items`` are ``{"key", "role", "ts", "text"}`` dicts in order; ``key`` is stable per
    turn (so a resumed session adds only its new turns). Returns ``(status, note)`` with
    status ``created``, ``updated``, ``unchanged``, ``too-short`` or ``refused-secret``.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown working source {source!r}")
    directory = ensure_dir()
    key_hash = _hash(f"{source}:{source_key}")
    now = utcnow()
    with _lock(directory):
        existing = next((n for n in _read_all(directory)
                         if n.get("source") == source and n.get("source_key") == key_hash), None)
        live = existing is not None and not _expired(existing, now)
        seen = set(existing.get("turn_keys", [])) if existing else set()
        new_items = [i for i in items if isinstance(i, dict) and i.get("key") and i.get("role") != "draft"
                     and _hash(str(i["key"])) not in seen]
        valid = [i for i in items if isinstance(i, dict) and i.get("role") in ("user", "assistant")
                 and isinstance(i.get("text"), str)]
        new_valid = [i for i in valid if i.get("key") and _hash(str(i["key"])) not in seen]
        old_turns = existing["turns"] if live else []
        # Withheld values come from each turn on its own, from its raw text: every turn the capture passed (it
        # re-reads the whole transcript, so a resumed session or a later Stop hook still sees the first turns,
        # unmasked, in memory only), replies a later reply replaced ("draft" items), the turns already stored,
        # and the raw project folder name. They are scrubbed from every turn and the project name.
        drafts = [i for i in items if isinstance(i, dict) and i.get("role") == "draft" and isinstance(i.get("raw"), str)]
        withheld: set[str] = set()
        for text in [_raw(i) for i in valid] + [d["raw"] for d in drafts] + [t["text"] for t in old_turns] + [project]:
            withheld |= _withheld(text)
        raw = [_raw(i) for i in new_valid]
        new_turns = [t for t in (_clean_turn(i, withheld) for i in new_valid) if t]
        if not new_turns:
            return "unchanged", existing if live else None
        # The stored turns are rebuilt from their raw text when the capture passed it again (so a secret that
        # straddled the 2,000-character cut and is labelled only now is masked whole before the cut), otherwise
        # masked again in full (a note written by older code gets the strong mask too). A stored Antigravity reply
        # that a later reply of the same turn replaced comes back as a "draft" with its key and raw text.
        raw_by_key = {_hash(str(d["key"])): {**d, "role": "assistant", "text": d["raw"]} for d in drafts if d.get("key")}
        raw_by_key |= {_hash(str(i["key"])): i for i in valid if i.get("key")}
        rebuilt = []
        for t in old_turns:
            again = _clean_turn(raw_by_key[t["key"]], withheld) if t.get("key") in raw_by_key else None
            rebuilt.append(again or {**t, "text": _scrub(_masked(t["text"]), withheld)})
        old_turns = rebuilt
        turns = old_turns + new_turns
        if not live:
            # judged on the text as captured, so masking a long secret clause does not change what is kept
            users = sum(1 for t in turns if t["role"] == "user")
            if users < min_user_turns or sum(len(t.strip()) for t in raw) < min_chars:
                return "too-short", None
        dropped = existing.get("dropped_turns", 0) if live else 0
        while len(turns) > 1 and sum(len(t["text"]) for t in turns) > MAX_NOTE_CHARS:
            turns.pop(0)
            dropped += 1
        # nothing is written if a secret pattern still matches after redaction and masking (checked before the
        # withheld-value scrub, which is an extra layer on top)
        # each text on its own: joined, a turn ending in "My staging password:" would take the next turn as a value
        check = [_masked(project)] + [_masked(t.strip()) for t in raw] + [t["text"] for t in old_turns]
        if any(_still_secret(c) for c in check):
            return "refused-secret", None
        project = tag(_scrub(_masked(project), withheld))  # masked and scrubbed, then made safe and cut
        note = {
            "version": FORMAT_VERSION,
            "id": existing["id"] if existing else f"{source}-{new_session_id()}",
            "source": source,
            "project": project,
            "source_key": key_hash,
            "captured_at": existing["captured_at"] if live else iso(now),
            "updated_at": iso(now),
            "expires_at": iso(now + timedelta(days=DEFAULT_TTL_DAYS)),
            "promoted_at": existing.get("promoted_at") if live else None,
            "dropped_turns": dropped,
            "turn_keys": sorted(seen | {_hash(str(i["key"])) for i in new_items}),
            "turns": turns,
        }
        atomic_write_json(directory / f"{note['id']}.json", note)  # mkstemp: 0600
        return ("updated" if live else "created"), note


def _mark(note_id: str, field: str) -> None:
    directory = ensure_dir()
    with _lock(directory):
        path = directory / f"{note_id}.json"
        note = _read(path)
        if note is not None:
            note[field] = iso(utcnow())
            atomic_write_json(path, note)


def mark_promoted(note_id: str) -> None:
    """Extraction finished and stored the note in long-term memory."""
    _mark(note_id, "promoted_at")


def mark_promote_queued(note_id: str) -> None:
    """Promotion started but extraction only queued or failed; ``promote`` can be retried."""
    _mark(note_id, "promote_queued_at")


def purge(everything: bool = False) -> int:
    """Delete expired notes (or all notes). Returns how many were removed."""
    directory = working_dir()
    if not directory.is_dir():
        return 0
    check_location(directory)
    now, removed = utcnow(), 0
    with _lock(directory):
        for path in sorted(directory.glob("*.json")):
            note = _read(path)
            if everything or note is None or _expired(note, now):
                path.unlink(missing_ok=True)
                removed += 1
    return removed


# -- reading (never writes) -------------------------------------------------------------

def _read(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(note, dict) or note.get("id") != path.stem or not isinstance(note.get("turns"), list):
        return None
    return note


def _read_all(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    return [n for n in (_read(p) for p in sorted(directory.glob("*.json"))) if n is not None]


def _expired(note: dict, now: datetime) -> bool:
    try:
        return parse_ts(note["expires_at"]) <= now
    except (KeyError, TypeError, ValueError):
        return True  # unreadable expiry: treat as expired, never return it


def list_notes() -> list[dict]:
    """Live (unexpired) notes, most recently updated first."""
    now = utcnow()
    notes = [n for n in _read_all(working_dir()) if not _expired(n, now)]
    return sorted(notes, key=lambda n: n.get("updated_at", ""), reverse=True)


def get_note(note_id: str) -> dict | None:
    if not ID_RE.fullmatch(note_id or ""):
        raise ValueError("not a working note id")
    note = _read(working_dir() / f"{note_id}.json")
    return note if note is not None and not _expired(note, utcnow()) else None


def latest(source: str, n: int = 1) -> list[dict]:
    return [note for note in list_notes() if note.get("source") == source][:n]


def _words(query: str) -> list[str]:
    return sorted({w for w in re.findall(r"\w+", query.lower()) if len(w) >= 2})


def search(query: str, limit: int = 5, source: str | None = None) -> list[tuple[dict, list[str]]]:
    """Live notes containing ALL words of ``query`` (case-insensitive), with short snippets.

    ``source`` limits the search to one capture source (``claude-code``, ``antigravity``)."""
    words = _words(query)
    if not words:
        return []
    results = []
    for note in list_notes():
        if source is not None and note.get("source") != source:
            continue
        body = "\n".join([note.get("project", "")] + [t["text"] for t in note["turns"]])
        lower = body.lower()
        if all(w in lower for w in words):
            snippets = []
            for w in words[:3]:
                i = lower.find(w)
                snippets.append("..." + " ".join(body[max(0, i - 80): i + 80].split()) + "...")
            results.append((note, snippets))
    return results[:limit]


# -- formatting -------------------------------------------------------------------------

def _day(value: str | None) -> str:
    return (value or "?")[:10]


def format_list(notes: list[dict]) -> str:
    lines = []
    for n in notes:
        chars = sum(len(t["text"]) for t in n["turns"])
        promoted = f", promoted {_day(n['promoted_at'])}" if n.get("promoted_at") else ""
        lines.append(f"{n['id']}  {n.get('project', '?')}: {len(n['turns'])} turns, {chars:,} chars, "
                     f"updated {_day(n.get('updated_at'))}, expires {_day(n.get('expires_at'))}{promoted}")
    return "\n".join(lines)


def format_note(note: dict) -> str:
    extra = []
    if note.get("promoted_at"):
        extra.append(f"promoted {_day(note['promoted_at'])}")
    if note.get("dropped_turns"):
        extra.append(f"{note['dropped_turns']} earlier turns dropped (size cap)")
    head = (f"{note['id']}  [{note.get('source')}, project {note.get('project', '?')}]\n"
            f"captured {_day(note.get('captured_at'))}, updated {_day(note.get('updated_at'))}, "
            f"expires {_day(note.get('expires_at'))}" + "".join(f", {e}" for e in extra))
    return head + "\n\n" + "\n\n".join(f"[{t['role']}] {t['text']}" for t in note["turns"])


def format_search(results: list[tuple[dict, list[str]]]) -> str:
    blocks = []
    for note, snippets in results:
        blocks.append(f"{note['id']}  {note.get('project', '?')}, updated {_day(note.get('updated_at'))}, "
                      f"expires {_day(note.get('expires_at'))}\n" + "\n".join(f"    {s}" for s in snippets))
    return "\n".join(blocks)


# -- promotion ---------------------------------------------------------------------------

def promotion_messages(note: dict) -> tuple[str, list[Message]]:
    """The note as a session log: the user's turns as ``user``, the assistant's as ``agent``.

    The default extraction scope then reads only the user's own words, as for room sessions.
    """
    sid = note["id"]
    messages, first_user = [], True
    for i, turn in enumerate(note["turns"]):
        if turn["role"] == "user":
            text = turn["text"]
            if first_user:
                text, first_user = f"Project: {note.get('project', '?')}\n\n{text}", False
            messages.append(Message(sid, note["source"], i, ROLE_USER, text))
        else:
            messages.append(Message(sid, _ASSISTANT_AGENT.get(note["source"], "claude"), i, ROLE_AGENT, turn["text"]))
    return sid, messages
