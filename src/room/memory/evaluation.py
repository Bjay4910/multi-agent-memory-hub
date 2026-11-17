"""Offline evaluation of recall over a fixed synthetic graph (no LLM calls).

The fixture (``tests/eval/graph.json``) is a list of pre-extracted batches that
are applied in order through the normal store logic, so dedupe and supersede
are exercised too. Each question in ``tests/eval/questions.json`` lists the
node names that should be recalled ("gold"). Some questions also carry a
``stale_text`` that belongs to a superseded fact and must never appear in the
injected memory block, and a ``current_text`` that must.

Metrics (averaged over questions):

* recall@k     = |gold in top k| / |gold|
* precision@3  = |gold in top 3| / 3 (bounded by the gold-set sizes; the
  report shows the highest achievable value next to it)
* MRR          = 1 / rank of the first gold node (0 if none in the top 8)
* stale facts  = stale-fact questions whose memory block contains the stale text
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .paths import project_root
from .recall import DEFAULT_TOKEN_BUDGET, rank, render
from .store import MemoryStore, Provenance, normalize_name


def default_eval_dir() -> Path:
    root = project_root()
    if root and (root / "tests" / "eval").is_dir():
        return root / "tests" / "eval"
    return Path.cwd() / "tests" / "eval"


@dataclass
class EvalReport:
    questions: int
    active_nodes: int
    recall_at_3: float
    recall_at_8: float
    precision_at_3: float
    precision_at_3_max: float
    mrr: float
    stale_checks: int
    stale_violations: int
    current_missing: int
    misses: list[str] = field(default_factory=list)

    def format(self, verbose: bool = False) -> str:
        lines = [
            f"Memory recall evaluation: {self.questions} questions over a fixed synthetic graph "
            f"({self.active_nodes} active nodes, no LLM calls)",
            f"  recall@3      {self.recall_at_3:.3f}",
            f"  recall@8      {self.recall_at_8:.3f}",
            f"  precision@3   {self.precision_at_3:.3f}  (highest possible with these gold sets: {self.precision_at_3_max:.3f})",
            f"  MRR           {self.mrr:.3f}",
            f"  stale facts   {self.stale_violations}/{self.stale_checks} superseded facts leaked into memory "
            f"({'PASS' if self.stale_violations == 0 else 'FAIL'}); "
            f"current fact missing in {self.current_missing}/{self.stale_checks}",
        ]
        if verbose and self.misses:
            lines.append("  questions with gold nodes outside the top 8:")
            lines.extend(f"    - {m}" for m in self.misses)
        return "\n".join(lines)


def load_fixture(directory: Path) -> tuple[dict, list[dict]]:
    graph = json.loads((directory / "graph.json").read_text(encoding="utf-8"))
    questions = json.loads((directory / "questions.json").read_text(encoding="utf-8"))
    return graph, questions


def build_store(graph: dict, db_path: Path) -> MemoryStore:
    store = MemoryStore(db_path)
    for batch in graph["batches"]:
        data = {"nodes": batch["nodes"], "edges": batch["edges"]}
        store.apply_extraction(data, Provenance(batch["session_id"], "fixture", "manual"), ts=batch["ts"])
    return store


def evaluate(directory: Path | None = None) -> EvalReport:
    directory = directory or default_eval_dir()
    graph, questions = load_fixture(directory)
    now = datetime.fromisoformat(graph["now"].replace("Z", "+00:00")).astimezone(timezone.utc)
    r3 = r8 = p3 = p3_max = mrr = 0.0
    stale_checks = stale_violations = current_missing = 0
    misses = []
    with tempfile.TemporaryDirectory(prefix="agent-room-eval-") as tmp:
        with build_store(graph, Path(tmp) / "memory.db") as store:
            active = store.stats()["nodes_active"]
            for q in questions:
                ranked = rank(store, q["question"], k=8, now=now)
                names = [normalize_name(s.node.name) for s in ranked]
                gold = {normalize_name(g) for g in q["gold"]}
                top3, top8 = names[:3], names[:8]
                r3 += len(gold & set(top3)) / len(gold)
                r8 += len(gold & set(top8)) / len(gold)
                p3 += len(gold & set(top3)) / 3
                p3_max += min(3, len(gold)) / 3
                first = next((i for i, nm in enumerate(top8, 1) if nm in gold), None)
                mrr += 1 / first if first else 0.0
                missing = gold - set(top8)
                if missing:
                    misses.append(f"Q{q['id']} {q['question']!r}: missing {', '.join(sorted(missing))}")
                if "stale_text" in q:
                    stale_checks += 1
                    block = render(store, ranked, DEFAULT_TOKEN_BUDGET).block
                    stale_violations += q["stale_text"] in block
                    current_missing += q["current_text"] not in block
    n = len(questions)
    return EvalReport(n, active, r3 / n, r8 / n, p3 / n, p3_max / n, mrr / n, stale_checks, stale_violations, current_missing, misses)
