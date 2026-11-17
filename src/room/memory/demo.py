"""A small, entirely made-up memory dataset for screenshots and demos.

Every person, company, program and project here is fictional. It is loaded
into a separate database (a temp ``AGENT_ROOM_HOME``), never into real memory.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .store import MemoryStore, Provenance


def _n(type: str, name: str, summary: str, confidence: float = 0.9) -> dict:
    return {"type": type, "name": name, "summary": summary, "confidence": confidence}


def _e(src: str, relation: str, dst: str) -> dict:
    return {"src": src, "relation": relation, "dst": dst}


DEMO_BATCHES = [
    ("demo-2026-09-01", "2026-09-01T10:00:00Z", {
        "nodes": [
            _n("person", "Sam Rivera", "Final-year CS student (fictional demo persona) looking for ML engineering roles and a masters."),
            _n("skill", "Python", "Main programming language; 4 years of use."),
            _n("skill", "TypeScript", "Used for web front ends."),
            _n("project", "Lumen Notes", "Offline-first notes app with sync; 2,000 monthly users."),
            _n("project", "Trail Tracker", "Hiking GPS app; cut battery use by 30% with batched location updates."),
            _n("organization", "Northfield University", "Fictional university offering an MSc in Computer Science."),
            _n("organization", "Brightwave Labs", "Fictional ML startup."),
            _n("application", "Northfield MSc application", "Preparing documents; deadline 15 January."),
            _n("application", "Brightwave Labs ML intern application", "Submitted on 20 August."),
            _n("decision", "Lumen Notes sync storage", "Chose SQLite with a change log for sync."),
            _n("task", "Ask Dr. Mira Chen for a reference", "Request a recommendation letter by December."),
            _n("person", "Dr. Mira Chen", "Fictional professor and project advisor at Northfield University."),
            _n("preference", "Work style", "Prefers hybrid roles with mentoring."),
        ],
        "edges": [
            _e("Sam Rivera", "works_on", "Lumen Notes"), _e("Sam Rivera", "works_on", "Trail Tracker"),
            _e("Lumen Notes", "requires", "TypeScript"), _e("Trail Tracker", "requires", "Python"),
            _e("Northfield MSc application", "applied_to", "Northfield University"),
            _e("Brightwave Labs ML intern application", "applied_to", "Brightwave Labs"),
            _e("Lumen Notes sync storage", "decided_in", "Lumen Notes"),
            _e("Dr. Mira Chen", "part_of", "Northfield University"),
            _e("Ask Dr. Mira Chen for a reference", "mentions", "Dr. Mira Chen"),
        ],
    }),
    ("demo-2026-11-01", "2026-11-01T10:00:00Z", {
        "nodes": [
            _n("application", "Brightwave Labs ML intern application", "Interview scheduled for 3 December."),
            _n("decision", "Lumen Notes sync storage", "Switched to Postgres with row-level sync after load tests."),
            _n("document", "Statement of purpose", "Draft 2 focuses on offline-first systems."),
        ],
        "edges": [_e("Statement of purpose", "relates_to", "Northfield MSc application")],
    }),
]


def load_demo(home: Path | None = None) -> MemoryStore:
    """Create (or recreate) the demo database in ``home`` (default: a new temp dir)."""
    home = Path(home) if home else Path(tempfile.mkdtemp(prefix="agent-room-demo-"))
    db = home / "memory.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    store = MemoryStore(db)
    store.set_meta("demo", "1")
    for session_id, ts, data in DEMO_BATCHES:
        store.apply_extraction(data, Provenance(session_id, "demo", "manual"), ts=ts)
    return store
