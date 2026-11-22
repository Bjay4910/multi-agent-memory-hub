"""Gap 3 fuzz: every exact example of the docs' "Handled forms (tested)" table, each with 3 substituted fake values at
the row's minimum length and within its case limit, plus the 45 hostile inputs of the dashboard API safety test,
captured three ways into the working layer and read back through every output. Fake values only.

The examples and their secret values come from the docs table and from the stage 8-13 test modules (the same
mapping the stage 14 substitution check used); no secret is typed here by hand.
"""

import base64
import random
import re
from pathlib import Path

import pytest

from memory import test_extraction_round3 as r3
from memory import test_extraction_stage8 as s8
from memory import test_extraction_stage9 as s9
from memory import test_extraction_stage10 as s10
from memory import test_extraction_stage11 as s11
from memory import test_extraction_stage12 as s12
from memory import test_extraction_stage13 as s13
from memory.test_working_redaction import Env, leaks_for

DOCS = Path(__file__).resolve().parents[2] / "docs" / "MEMORY.md"


def _pairs():
    """example text -> secret value, from the test modules that verify each table row."""
    pairs = {}
    add = pairs.setdefault
    for d in (r3.PREFIXLESS, r3.PLAIN_WORD):
        for v in d.values():
            if isinstance(v, tuple) and len(v) >= 2:
                add(v[0], v[1])
    for d in (s8.GAP1, s8.GAP2, s9.NEXT_LINE, s9.NORMALIZED, s9.BASE64):
        for v in d.values():
            add(v[0], v[1])
    for k, t in s9.MORE_LABELS.items():
        add(t, s9.MORE_LABEL_VALUES[k])
    for v in s9.STRUCTURED.values():
        add(v[0], v[1][0])
    for v in s9.USERINFO.values():
        add(v[0], v[2])
    for v in list(s10.QUOTED.values()) + list(s10.WRAPPED.values()):
        add(v[0], v[1])
    for _, t, v, _m in s10.B_SHAPES + s11.A_CASES + s11.B_CASES + s12.C_CASES + s13.A_CASES:
        add(t, v)
    # table cells that abbreviate or describe their example: the full text from the test that verifies the row
    sas = next(x for x in s10.B_SHAPES if "sig=Ab3dEf" in x[1])
    manual = {
        "password:\n\n  amber22": ("password:\n\n  amber22", "amber22"),
        "https://api.example.com/v2/items?api_key=lilac7x9&page=2": (None, "lilac7x9"),
        "https://api.example.com/search?token=violet94&filter[a]=1": (None, "violet94"),
        "Authorization: Bearer eyJhbGci...": r3.PREFIXLESS["bearer"][:2],
        "Trail Tracker mails through SendGrid with SG.<22 chars>.<43 chars> in prod.":
            (f"Trail Tracker mails through SendGrid with {s10.FAKE_SG} in prod.", s10.FAKE_SG),
        "...?sv=2021-08-06&sig=Ab3dEf%2BgHi9jKl%3D&se=2026": (sas[1], sas[2]),
        "Webhook secret whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw is set": (None, "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"),
        "My config:\n~~~\npassword = fir23\n~~~\nTrail Tracker deploys nightly.": (None, "fir23"),
        "Trail Tracker uses token=violet94&filter[a]=1 for search": (None, "violet94"),
        "Trail Tracker key Xq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4 rotates weekly":
            (None, "Xq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4"),
        'secret: "hunter22x"': (None, "hunter22x"),
    }
    return pairs, manual


def _rows():
    """(form, [examples], minimum-length text) for each table row, read from docs/MEMORY.md."""
    lines = DOCS.read_text(encoding="utf-8").split("\n")
    start, end = lines.index("#### Handled forms (tested)"), lines.index("#### Known gaps")
    rows = []
    for line in lines[start:end]:
        if not line.startswith("| ") or line.startswith("| form "):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        examples = []
        for m in re.findall(r"``\s?(.+?)\s?``|`([^`]+)`", cells[1]):
            e = (m[0] or m[1]).replace("\\|", "|").replace("\\n", "\n")
            if "\\u" in e:
                e = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), e)
            if e != "FAKE_SG":
                examples.append(e)
        rows.append((cells[0], examples, cells[3] if len(cells) > 3 else ""))
    return rows


def _alnum(rng, n, mixed=False):
    chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789" if mixed or n > 12 else "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        v = "".join(rng.choice(chars) for _ in range(n))
        if re.search(r"\d", v) and re.search(r"[a-z]", v) and (not mixed or re.search(r"[A-Z]", v[1:])):
            return v


def _b64(rng, nbytes, pad=None):
    """Base64 of ``nbytes`` random bytes with an upper-case letter after the first character, a lower-case letter
    and a digit (inside every row's case limit)."""
    while True:
        v = base64.b64encode(bytes(rng.randrange(256) for _ in range(nbytes))).decode()
        core = v.rstrip("=")
        if (re.search(r"[A-Z]", core[1:]) and re.search(r"[a-z]", core) and re.search(r"\d", core)
                and (pad is None or v.count("=") == pad)):
            return v


def _variants(form, example, secret, rng):
    """Three values of the secret's kind, at the row's minimum length and within its case limit."""
    if form.startswith("base64 of 32+"):  # 32+ base64 characters (row 19)
        return [_b64(rng, n) for n in (24, 30, 36)]
    pad = secret.count("=") if re.fullmatch(r"[A-Za-z0-9+/]{4,}={1,2}", secret) else 0
    if re.search(r"\bbasic\b", example, re.I) and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", secret):  # 8+ (row 24)
        sizes = {0: (6, 9, 15), 1: (8, 11, 17), 2: (7, 13, 19)}[pad]
        return [_b64(rng, n, pad) for n in sizes]
    if pad:  # "=" or "==" padded base64 after a label: 4+ characters before the padding (rows 20, 21)
        return [_b64(rng, n, pad) for n in ({1: (5, 11, 17), 2: (4, 10, 16)}[pad])]
    if secret.startswith("Xq7Lp2"):  # row 49's 32+ key example
        return [_alnum(rng, n, mixed=True) for n in (32, 38, 48)]
    for prefix, minimum in (("whsec_test_", 11), ("whsec_", 16), ("glpat-", 20), ("pypi-", 16), ("hvs.", 20)):
        if secret.startswith(prefix):
            return [prefix + _alnum(rng, minimum + extra, mixed=True) for extra in (0, 6, 16)]
    if secret.startswith("SG."):  # both parts 16+
        head = secret.rsplit(".", 1)[0]
        return [head + "." + _alnum(rng, n, mixed=True) for n in (16, 22, 32)]
    if secret.count(".") == 2 and len(secret) > 50:  # Discord-style: third segment 27-64; JWT: any
        head = secret.rsplit(".", 1)[0]
        return [head + "." + _alnum(rng, n, mixed=True) for n in (27, 33, 43)]
    if "sig=" in example:  # 8+
        return [_alnum(rng, n, mixed=True) for n in (8, 14, 24)]
    return [_alnum(rng, n) for n in (4, 10, 24)]  # rows with no minimum: 4, 10 and 24 characters


def table_corpus():
    pairs, manual = _pairs()
    rng = random.Random(3)
    corpus = []
    for row, (form, examples, _minimum) in enumerate(_rows(), 1):
        for example in examples:
            text, secret = manual.get(example, (None, None))
            text, secret = text or example, secret or pairs.get(example)
            if secret is None:
                raise AssertionError(f"no secret known for table example {example!r}")
            for value in _variants(form, text, secret, rng):
                corpus.append((f"row{row}", text.replace(secret, value), value))
    return corpus


def hostile_corpus():
    """The 45 hostile inputs of the dashboard API safety test (stage 8 and 9 corpora)."""
    corpus = [s8.GAP1[k] for k in s8.GAP1] + [s8.GAP2[k] for k in s8.GAP2] + [s9.NORMALIZED[k] for k in s9.NORMALIZED]
    corpus += [(s9.MORE_LABELS[k], s9.MORE_LABEL_VALUES[k]) for k in s9.MORE_LABELS]
    corpus += [(s9.USERINFO[k][0], s9.USERINFO[k][2]) for k in s9.USERINFO]
    return [("hostile", t, v) for t, v in corpus]


def test_corpus_sizes():
    assert len(hostile_corpus()) == 45
    rows = _rows()
    examples = sum(len(e) for _, e, _ in rows)
    assert len(table_corpus()) == 3 * examples and examples > 150


@pytest.mark.parametrize("group", ["table", "hostile"])
def test_working_layer_fuzz_never_leaks(group, tmp_path, monkeypatch):
    corpus = table_corpus() if group == "table" else hostile_corpus()
    leaks, refused = [], []
    for i, (row, text, value) in enumerate(corpus):
        env = Env(tmp_path / f"c{i}", monkeypatch)
        statuses, found = leaks_for(env, text, value)
        if found:
            leaks.append((row, text[:80], value, found))
        if "created" not in statuses:
            refused.append((row, text[:60], statuses))
    print(f"working fuzz ({group}): {len(corpus)} inputs x 3 capture paths, {len(leaks)} leaks, "
          f"{len(refused)} with nothing stored")
    for r in refused:
        print("  nothing stored:", r)
    assert not leaks, f"{len(leaks)} leaks, first: {leaks[:10]}"
