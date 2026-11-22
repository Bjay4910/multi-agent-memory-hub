"""Gap 3 review: the independent reviewer's leaks in the working-layer redaction, as regression cases.

Checked across Claude Code capture, Antigravity capture, direct working.upsert, resumed Claude sessions and
multi-turn Antigravity Stop hooks, then through `room working list/show/search`, MCP working_search and
session_latest, and every byte under the temp AGENT_ROOM_HOME (notes, temp files, .lock, capture/capture.log).
The leak check here also splits on "." (like quality._PIECE_SPLIT), and the table and hostile corpora are re-run
with it. Fake values only (the reviewer's, verbatim, plus fresh ones from a different seed).
"""

import json
import random
import re

import pytest

from memory import test_extraction_stage10 as s10
from memory import test_extraction_stage11 as s11
from memory import test_working_redaction_fuzz as fz
from memory.test_working_redaction import PAD_USER, PAD_USER_2, Env
from room import capture, capture_agy
from room.memory import quality, working
from room.memory.redact import redact_secrets

CWD = "/Users/someone/Developer/okapi-lantern"


def words_strict(value, text):
    """The value, or a piece of it (3+ characters) split at whitespace and : / , ; ( ) < > " \\ and ".", standing
    alone in ``text`` (as written or normalized: NFKC, zero-width removed, homoglyphs folded)."""
    found = []
    for t in (text, quality._normalized(text)[0]):
        if value in t:
            found.append(value)
        found += [w for w in re.split(r"[\s:/,;()<>\"\\.]+", value)
                  if len(w) >= 3 and re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", t)]
    return sorted(set(found))


def prefixes_in(value, text, shortest=4):
    """A leading fragment of the value (what a cut leaves behind), 4+ characters."""
    return [value[:n] for n in range(len(value), shortest - 1, -1) if value[:n] in text][:1]


def leaks(env, value, queries=(), fragments=False):
    readable = env.outputs(["Okapi-Lantern", value, *queries])
    stored = env.all_bytes()
    found = {f"output:{w}" for w in words_strict(value, readable)} | {f"file:{w}" for w in words_strict(value, stored)}
    if fragments:
        found |= {f"output-prefix:{w}" for w in prefixes_in(value, readable)}
        found |= {f"file-prefix:{w}" for w in prefixes_in(value, stored)}
    if "[[REDACTED]]" in readable or "[[REDACTED]]" in stored:
        found.add("corrupted:[[REDACTED]]")
    return sorted(found)


# -- transcripts that can grow, for resumed sessions and per-turn Stop hooks --------------------------------------

class ClaudeSession:
    def __init__(self, env, sid="5555aaaa-2222-bbbb-3333-cccc4444dddd", cwd=CWD):
        self.env, self.sid, self.cwd, self.n = env, sid, cwd, 0
        self.path = env.root / "claude-config" / "projects" / "-Users-someone-Developer-x" / f"{sid}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, role, text):
        self.n += 1
        content = text if role == "user" else [{"type": "text", "text": text}]
        with self.path.open("a") as fh:
            fh.write(json.dumps({"type": role, "message": {"role": role, "content": content}, "uuid": f"{self.sid}-{self.n}",
                                 "sessionId": self.sid, "cwd": self.cwd, "entrypoint": "cli",
                                 "timestamp": f"2026-11-22T06:{self.n:02d}:00Z"}) + "\n")

    def hook(self):
        return {"session_id": self.sid, "transcript_path": str(self.path), "cwd": self.cwd}

    def capture(self):
        return capture.capture_claude(self.hook())

    def worker(self):
        return capture.run_worker(json.dumps(self.hook()))


class AgySession:
    def __init__(self, env, cid="7777392a-cea0-4fc6-9c37-4b81cc475186", cwd=CWD):
        self.env, self.cid, self.cwd, self.step = env, cid, cwd, 0
        self.path = env.root / "agy-cli" / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, entry):
        entry.update(step_index=self.step, created_at=f"2026-11-22T06:{self.step % 60:02d}:00Z", status="DONE")
        self.step += 1
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def add(self, role, text):
        if role == "user":
            self._write({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>"})
        else:
            self._write({"type": "PLANNER_RESPONSE", "source": "MODEL", "model": "m", "content": text, "thinking": "t"})

    def hook(self):
        return {"conversationId": self.cid, "transcriptPath": str(self.path), "cwd": self.cwd, "workspacePaths": [self.cwd],
                "hookEventName": "Stop", "fullyIdle": True, "terminationReason": "model_stop", "lastUserInput": "x",
                "modelName": "auto", "artifactDirectoryPath": "/x"}

    def capture(self):
        return capture_agy.capture_antigravity(self.hook())

    def worker(self):
        return capture_agy.run_worker(json.dumps(self.hook()))


def first_turns(session, text):
    """A first exchange long enough to be stored: the text, a reply, and a second user message."""
    session.add("user", f"{text}\n{PAD_USER}")
    session.add("assistant", "Noted, thanks.")
    session.add("user", PAD_USER_2)
    session.add("assistant", "Plan noted for Okapi-Lantern.")


# -- 1. redact_secrets before the strong mask left fragments ------------------------------------------------------

ORDER = [
    ("sendgrid", f"Trail Tracker mails through SendGrid with {s10.FAKE_SG} in prod.", s10.FAKE_SG),
    ("discord", *next((t, v) for f, t, v, _ in s11.B_CASES if v.startswith("MTA0"))),
    ("base64-slash", "Trail Tracker uses AbCd/rvw4KOAqoy0S+ayoGm1HxciXjjatyeE1 for sync.",
     "AbCd/rvw4KOAqoy0S+ayoGm1HxciXjjatyeE1"),
]


def fresh_rows_19_25_27():
    """3 fresh values for each example of table rows 19, 25 and 27, at the row's minimum and within its case limit,
    from a different seed than the working fuzz (3)."""
    pairs, manual = fz._pairs()
    rng = random.Random(1925)
    out = []
    for row, (form, examples, _m) in enumerate(fz._rows(), 1):
        if row not in (19, 25, 27):
            continue
        for example in examples:
            text, secret = manual.get(example, (None, None))
            text, secret = text or example, secret or pairs.get(example)
            for value in fz._variants(form, text, secret, rng):
                out.append((f"row{row}", text.replace(secret, value), value))
    return out


ORDER_CASES = ORDER + fresh_rows_19_25_27()


@pytest.mark.parametrize("case, text, value", ORDER_CASES, ids=[f"{c}-{i}" for i, (c, *_) in enumerate(ORDER_CASES)])
def test_token_is_masked_whole_not_half(case, text, value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert (env.claude(text), env.agy(text), env.direct(text)) == ("created",) * 3
    assert leaks(env, value, fragments=True) == []


def test_redacted_marker_is_never_withheld_or_doubled(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    text = "Search uses https://api.example.com/v2/items?api_key=lilac7x9&page=2 for the list."
    assert (env.claude(text), env.agy(text), env.direct(text)) == ("created",) * 3
    stored = env.all_bytes()
    assert "lilac7x9" not in stored and "[[REDACTED]]" not in stored
    assert stored.count("&page=2") >= 3  # the parameter after the masked value is kept in every note
    assert all(v.upper().find("REDACTED") == -1 for v in working._withheld(working.redact_for_note(text)))


# -- 2. a secret straddling the 2,000-character turn cut ----------------------------------------------------------

STRADDLE = [
    ("glpat", "CI uses glpat-AbCdEfGhIjKlMnOpQrSt12 for pushes", "glpat-AbCdEfGhIjKlMnOpQrSt12"),
    ("whsec", "Webhook signing uses whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw in prod", "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"),
    ("base64", "Sync uses Ab3dEf7gHi9jKl1m/No3pQr5sTu7vWx9yZa1bCd3eFg= daily", "Ab3dEf7gHi9jKl1m/No3pQr5sTu7vWx9yZa1bCd3eFg="),
    ("userinfo", "It connects to postgres://admin:sycamore99@db.example.com:5432/prod at boot", "sycamore99"),
    ("xml", "Config: <password>elm207xyz</password> in the file", "elm207xyz"),
    ("json-list", 'Config: {"secrets": ["alder163qq", "birch174qq"]}', "alder163qq"),
    ("quoted-comma", 'Config: {"password": "alpha,beta99zz", "env": "prod"}', "alpha,beta99zz"),
    ("sendgrid", f"Mail via {s10.FAKE_SG} in prod", s10.FAKE_SG),
    ("basic", "curl sends Basic YWFhOmFhYWFhYWFh to the API", "YWFhOmFhYWFhYWFh"),
]


def straddling(example, value):
    """A user turn where ``value`` starts 6 characters before the 2,000-character cut."""
    start = example.index(value)
    filler_len = capture.MAX_TURN_CHARS - 6 - start
    filler = ("plan the release " * 200)[:filler_len - 1] + " "
    return filler + example


@pytest.mark.parametrize("case, example, value", STRADDLE, ids=[c for c, *_ in STRADDLE])
@pytest.mark.parametrize("path", ["claude", "agy"])
def test_secret_across_the_turn_cut_leaves_no_fragment(case, example, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    text = straddling(example, value)
    assert text.index(value) < capture.MAX_TURN_CHARS < text.index(value) + len(value)
    session = (ClaudeSession if path == "claude" else AgySession)(env)
    first_turns(session, text)
    assert session.capture() == "created"
    assert leaks(env, value, fragments=True) == []


# -- 3. a bare repeat in a later turn, a resumed session or a later Stop hook ---------------------------------------

CROSS_TURN = [
    ("password: azurite223", "azurite223", "azurite223"),
    ("api_key=chrysocolla334", "chrysocolla334", "chrysocolla334"),
    ("My config:\n```\npassword = malachite881\n```", "malachite881", "malachite881"),
    ("password: dioptase445 (yes, dioptase445 is active)", "Fine.", "dioptase445"),
    ("the password is alexandrite556", "Still remembering alexandrite556", "alexandrite556"),
    ("aventurine667", "Actually the password is aventurine667", "aventurine667"),
]


@pytest.mark.parametrize("first, later, value", CROSS_TURN, ids=[v for *_, v in CROSS_TURN])
@pytest.mark.parametrize("path", ["claude-resumed", "agy-stop-hooks"])
def test_bare_repeat_in_a_later_capture_is_scrubbed(first, later, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    session = (ClaudeSession if path == "claude-resumed" else AgySession)(env)
    first_turns(session, first)
    assert session.capture() == "created"
    session.add("user", later)
    session.add("assistant", f"Okay: {later}")
    assert session.capture() == "updated"
    assert leaks(env, value) == []


@pytest.mark.parametrize("first, later, value", CROSS_TURN, ids=[v for *_, v in CROSS_TURN])
def test_bare_repeat_in_the_same_direct_save_is_scrubbed(first, later, value, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    items = [{"key": "u1", "role": "user", "ts": "t", "text": f"{first}\n{PAD_USER}"},
             {"key": "u2", "role": "user", "ts": "t", "text": PAD_USER_2},
             {"key": "u3", "role": "user", "ts": "t", "text": later}]
    assert working.upsert("claude-code", "k", "okapi-lantern", items)[0] == "created"
    assert leaks(env, value) == []


# -- 4. the project tag: masked before it is sanitized and cut, and logged as stored ------------------------------

PROJECTS = [
    ("/Users/someone/Developer/password=serpentine326", "serpentine326"),
    ("/Users/someone/Developer/api_key=sugilite437", "sugilite437"),
    ("/Users/someone/Developer/pwd: turquoise548", "turquoise548"),
    ("/Users/someone/Developer/my-super-project-glpat-AbCdEfGhIjKlMnOpQrSt99", "glpat-AbCdEfGhIjKlMnOpQrSt99"),
]


@pytest.mark.parametrize("cwd, value", PROJECTS, ids=[v for _, v in PROJECTS])
@pytest.mark.parametrize("path", ["claude", "agy"])
def test_project_tag_never_carries_a_secret(cwd, value, path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    session = (ClaudeSession if path == "claude" else AgySession)(env, cwd=cwd)
    first_turns(session, "A plain planning message.")
    assert session.worker() == 0
    assert working.list_notes(), "the worker must have stored the note"
    assert "created" in (env.home / "capture" / "capture.log").read_text()
    assert leaks(env, value, fragments=True) == []


@pytest.mark.parametrize("path", ["claude", "agy"])
def test_capture_log_uses_the_tag_scrubbed_with_the_turns_values(path, tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    session = (ClaudeSession if path == "claude" else AgySession)(env, cwd="/Users/someone/Developer/rhodonite779")
    first_turns(session, "password: rhodonite779")
    assert session.worker() == 0
    assert "rhodonite779" not in env.all_bytes()


# -- 5. a homoglyph repeat (Cyrillic а in the bare repeat) -------------------------------------------------------

HOMOGLYPH_LATER = "I typed c\u0430rnelian778 again just now."


def test_homoglyph_repeat_is_scrubbed_in_a_direct_save(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    items = [{"key": "u1", "role": "user", "ts": "t", "text": f"the password is carnelian778\n{PAD_USER}"},
             {"key": "u2", "role": "user", "ts": "t", "text": PAD_USER_2},
             {"key": "u3", "role": "user", "ts": "t", "text": HOMOGLYPH_LATER}]
    assert working.upsert("claude-code", "k", "okapi-lantern", items)[0] == "created"
    assert leaks(env, "carnelian778") == []
    assert "c\u0430rnelian778" not in env.all_bytes()


def test_homoglyph_repeat_is_scrubbed_across_stop_hooks(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    session = AgySession(env)
    first_turns(session, "the password is carnelian778")
    assert session.capture() == "created"
    session.add("user", HOMOGLYPH_LATER)
    session.add("assistant", "Okay.")
    assert session.capture() == "updated"
    assert leaks(env, "carnelian778") == []
    assert "c\u0430rnelian778" not in env.all_bytes()


# -- 6. only user and assistant items count ----------------------------------------------------------------------

def test_items_with_other_roles_do_not_count_towards_the_size_threshold(tmp_path, monkeypatch):
    Env(tmp_path, monkeypatch)
    items = [{"key": "u1", "role": "user", "ts": "t", "text": "short one"},
             {"key": "u2", "role": "user", "ts": "t", "text": "short two"},
             {"key": "s1", "role": "system", "ts": "t", "text": "system words " * 60}]
    assert working.upsert("claude-code", "k", "okapi-lantern", items)[0] == "too-short"


# -- C. ordinary sessions are stored exactly as before ------------------------------------------------------------

ORDINARY = [
    "Let's plan the Okapi-Lantern release for Friday afternoon.",
    "The README needs an install section and a short usage example.",
    "Please keep the changelog to user-visible changes only.",
    "I prefer pytest over unittest for this project.",
    "We moved the build from Make to a single shell script.",
    "The demo crashed because the config file was missing a newline.",
    "Reset your password from the settings page if you forget it.",
    "The password manager was updated last week without issues.",
    "Press any keyboard key to continue the installer.",
    "Rotate the signing key every quarter, as the policy says.",
    "Session tokens expire when you log out of the dashboard.",
    "Keep the API key out of the repository and out of screenshots.",
    "Grandma's secret sauce recipe has paprika and garlic in it.",
    "The token limit for this model is generous enough for us.",
    "Turn the key and the old engine starts after a few tries.",
    "Lost my house key on the train home yesterday evening.",
    "She kept the surprise party a secret until Saturday.",
    "The CI runs on every push and takes about four minutes.",
    "Use UTC timestamps everywhere and format them as ISO dates.",
    "The meeting covered password policy changes for next year.",
]


@pytest.mark.parametrize("sentence", ORDINARY)
def test_ordinary_sessions_are_stored_exactly_as_before(sentence, tmp_path, monkeypatch):
    """Byte for byte what main stored: main's pipeline was redact_secrets only, which leaves these unchanged."""
    env = Env(tmp_path, monkeypatch)
    assert (env.claude(sentence), env.agy(sentence), env.direct(sentence)) == ("created",) * 3
    for note in working.list_notes():
        assert note["project"] == "okapi-lantern"
        first = note["turns"][0]["text"]
        assert first == redact_secrets(f"{sentence}\n{PAD_USER}") == f"{sentence}\n{PAD_USER}"
        assert note["turns"][1]["text"] == f"Noted: {sentence}"


# -- the table and hostile corpora again, with the stricter check --------------------------------------------------

@pytest.mark.parametrize("group", ["table", "hostile"])
def test_working_fuzz_with_the_stricter_check(group, tmp_path, monkeypatch):
    corpus = fz.table_corpus() if group == "table" else fz.hostile_corpus()
    found = []
    for i, (row, text, value) in enumerate(corpus):
        env = Env(tmp_path / f"c{i}", monkeypatch)
        env.claude(text), env.agy(text), env.direct(text)
        bad = leaks(env, value)
        if bad:
            found.append((row, text[:70], value, bad))
    print(f"working fuzz, stricter check ({group}): {len(corpus)} inputs x 3 capture paths, {len(found)} leaks")
    assert not found, f"{len(found)} leaks, first: {found[:8]}"
