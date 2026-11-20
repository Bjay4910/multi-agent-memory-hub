"""Fact-extraction evaluation: a fixed set of made-up, redacted transcripts with the facts a
human would expect, scored against what the real extraction pipeline stores.

Run it with the local extractor (Hermes through Ollama; no other network calls):

    python -m room.memory.extraction_eval --runs 3

Every case runs in its own temporary AGENT_ROOM_HOME and memory database, so the
user's real memory is never read or written.

Metrics (over all cases):
- recall: expected facts found / expected facts. A fact is found when one stored
  node's name + summary contains all of its keywords (whole words, any case).
- precision: useful nodes / stored nodes (the "Me" node included). A node is useful
  when it matches an expected or allowed keyword set, is supported, and holds no
  forbidden keyword.
- unsupported: nodes whose name + summary content words are mostly (over 40%) absent
  from the user's text: invented details, general knowledge, guesses.
- forbidden: nodes that contain a forbidden keyword (chatter, the question restated,
  tool noise, an assistant's guess); plurals count.
- secret_leaks: files under the temporary home that contain the fake secret.
- duplicates: near-duplicate nodes from one run (names sharing most words), extra
  nodes with an existing node's name, and near-duplicate sentences inside a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..agents import build_agent
from ..agents.base import Agent
from ..transcript import ROLE_FINAL, ROLE_USER, Message
from .evaluation import default_eval_dir
from .store import STOPWORDS, MemoryStore, Provenance, normalize_name

CASES_FILE = "extraction_cases.json"
FAKE_SECRET = "gh" "p_" + "E7v4L9q2W8x1R5t3Y6u0I2o4P8a1S3d5F7g9"


def load_cases(path: Path | None = None) -> list[dict]:
    return json.loads((path or default_eval_dir() / CASES_FILE).read_text(encoding="utf-8"))


def case_messages(case: dict, secret: str = FAKE_SECRET) -> list[Message]:
    sid = f"eval-{case['id']}"
    msgs = [Message(sid, "user", 0, ROLE_USER, text.replace("{FAKE_SECRET}", secret)) for text in case["user"]]
    if case.get("answer"):
        msgs.append(Message(sid, "claude", 1, ROLE_FINAL, case["answer"]))
    return msgs


# Words a summary may use without the source saying them.
GENERIC = STOPWORDS | frozenset(
    "user user's users person project projects app application applications program side using use used uses "
    "s first next new one two last week".split())
SUPPORT_MIN = 0.6


def _has(text: str, keyword: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(keyword.lower()) + r"(?:s|es)?(?!\w)", text) is not None


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _content(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[\w.]+", text.lower().replace("'s", "")) if w.strip(".") and w not in GENERIC}


def supported(node: dict, source: str) -> bool:
    words = _content(f"{node['name']} {node['summary']}")
    if not words:
        return True
    return len(words & _content(source)) / len(words) >= SUPPORT_MIN


def _same_thing(a: dict, b: dict) -> bool:
    wa, wb = _content(a["name"]), _content(b["name"])
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.75


def _matches(text: str, keyword_sets: list[list[str]]) -> bool:
    return any(all(_has(text, k) for k in ks) for ks in keyword_sets)


def _words(sentence: str) -> set[str]:
    return set(re.findall(r"\w+", sentence.lower()))


def near_duplicate_sentences(summary: str, threshold: float = 0.8) -> int:
    """Sentences whose words overlap an earlier sentence of the same summary by at least ``threshold``."""
    seen: list[set[str]] = []
    dupes = 0
    for sentence in re.split(r"(?<=[.!?])\s+", summary.strip()):
        words = _words(sentence)
        if not words:
            continue
        if any(len(words & s) / len(words | s) >= threshold for s in seen):
            dupes += 1
        seen.append(words)
    return dupes


@dataclass
class CaseResult:
    case: str
    nodes: list[dict]
    expected: int
    found: int
    counted_nodes: int
    useful_nodes: int
    forbidden: int
    secret_leaks: int
    duplicates: int
    status: str
    unsupported: int = 0
    missed: list[list[str]] = field(default_factory=list)
    junk: list[str] = field(default_factory=list)


def score_case(case: dict, nodes: list[dict], secret_leaks: int = 0, status: str = "stored") -> CaseResult:
    texts = [(n, f"{n['name']} {n['summary']}".lower()) for n in nodes]
    expected = case.get("expected", [])
    source = " ".join(case["user"])
    missed = [ks for ks in expected if not any(_matches(t, [ks]) for _, t in texts)]
    counted = texts
    forbidden = [n for n, t in texts if any(_has(t, k) for k in case.get("forbidden", []))]
    unsupported = [n for n, _ in texts if not supported(n, source)]
    useful = [n for n, t in counted if _matches(t, expected + case.get("allowed", []))
              and n not in forbidden and n not in unsupported]
    fresh = [n for n in nodes if not any(normalize_name(n["name"]) == normalize_name(e["name"]) for e in case.get("existing", []))]
    duplicates = sum(1 for i, n in enumerate(fresh) if any(_same_thing(n, m) for m in fresh[:i]))
    for existing in case.get("existing", []):
        same = [n for n in nodes if normalize_name(n["name"]) == normalize_name(existing["name"])]
        duplicates += max(0, len(same) - 1) + sum(near_duplicate_sentences(n["summary"]) for n in same)
    junk = [n["name"] for n, _ in counted if n not in useful]
    return CaseResult(case["id"], nodes, len(expected), len(expected) - len(missed), len(counted), len(useful),
                      len(forbidden), secret_leaks, duplicates, status, unsupported=len(unsupported),
                      missed=missed, junk=sorted(set(junk)))


@dataclass
class Report:
    cases: list[CaseResult]

    @property
    def recall(self) -> float:
        total = sum(c.expected for c in self.cases)
        return sum(c.found for c in self.cases) / total if total else 1.0

    @property
    def precision(self) -> float:
        total = sum(c.counted_nodes for c in self.cases)
        return sum(c.useful_nodes for c in self.cases) / total if total else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def totals(self) -> dict:
        return {"recall": round(self.recall, 3), "precision": round(self.precision, 3), "f1": round(self.f1, 3),
                "nodes": sum(c.counted_nodes for c in self.cases),
                "forbidden": sum(c.forbidden for c in self.cases),
                "unsupported": sum(c.unsupported for c in self.cases),
                "secret_leaks": sum(c.secret_leaks for c in self.cases),
                "duplicates": sum(c.duplicates for c in self.cases),
                "queued": sum(c.status == "queued" for c in self.cases)}


def _leaks(home: Path, secret: str) -> int:
    needle = secret.encode()
    return sum(1 for p in home.rglob("*") if p.is_file() and needle in p.read_bytes())


async def run_case(case: dict, agent_factory: Callable[[str], Agent], home: Path) -> CaseResult:
    from . import extractor

    db = home / "memory.db"
    with MemoryStore(db) as store:
        if case.get("existing"):
            store.apply_extraction({"nodes": case["existing"], "edges": []}, Provenance("eval-existing", "eval", "manual"))
    result = await extractor.extract_session(f"eval-{case['id']}", case_messages(case), agent_factory, store_path=db)
    with MemoryStore(db) as store:
        nodes = [{"type": n.type, "name": n.name, "summary": n.summary} for n in store.all_nodes()]
    return score_case(case, nodes, _leaks(home, FAKE_SECRET), result.status)


async def run_all(cases: list[dict], agent_factory: Callable[[str], Agent] = build_agent) -> Report:
    results = []
    saved = {k: os.environ.get(k) for k in ("AGENT_ROOM_HOME",)}
    agents: dict[str, Agent] = {}

    def shared(name: str) -> Agent:  # one client per run, closed below inside the same event loop
        if name not in agents:
            agents[name] = agent_factory(name)
        return agents[name]

    try:
        for case in cases:
            with tempfile.TemporaryDirectory(prefix="extraction-eval-") as tmp:
                os.environ["AGENT_ROOM_HOME"] = tmp
                results.append(await run_case(case, shared, Path(tmp)))
    finally:
        for agent in agents.values():
            client = getattr(agent, "_client", None)
            if client is not None and hasattr(client, "close"):
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return Report(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score fact extraction on the fixed evaluation set.")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--out", type=Path, help="write every run's details as JSON")
    parser.add_argument("--rescore", type=Path, help="score the nodes saved by an earlier --out instead of running")
    args = parser.parse_args(argv)
    os.environ.setdefault("MEMORY_EXTRACTOR", "hermes")
    cases = load_cases()
    by_id = {c["id"]: c for c in cases}
    saved = json.loads(args.rescore.read_text()) if args.rescore else None
    runs = []
    for i in range(len(saved) if saved else args.runs):
        if saved:
            report = Report([score_case(by_id[r["case"]], r["nodes"], r["secret_leaks"], r["status"]) for r in saved[i]])
        else:
            report = asyncio.run(run_all(cases))
        runs.append(report)
        print(f"run {i + 1}: {json.dumps(report.totals())}")
        for c in report.cases:
            print(f"   {c.case:24} found {c.found}/{c.expected}  nodes {c.counted_nodes} useful {c.useful_nodes}"
                  f"  unsupported {c.unsupported}  forbidden {c.forbidden}  dup {c.duplicates}  leaks {c.secret_leaks}  {c.status}"
                  + (f"  missed {c.missed}" if c.missed else "") + (f"  junk {c.junk}" if c.junk else ""))
    for key in ("recall", "precision", "f1"):
        values = [r.totals()[key] for r in runs]
        print(f"{key:10} mean {statistics.mean(values):.3f}  min {min(values):.3f}  max {max(values):.3f}")
    for key in ("nodes", "unsupported", "forbidden", "secret_leaks", "duplicates", "queued"):
        print(f"{key:10} per run {[r.totals()[key] for r in runs]}")
    if args.out:
        args.out.write_text(json.dumps([[asdict(c) for c in r.cases] for r in runs], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
