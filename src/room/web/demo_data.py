"""A larger, entirely made-up memory dataset for the dashboard demo (``room dashboard-api --demo``).

Every person, organization, project, product and place here is invented. Skills are generic topics, not
products. Nothing in it is, or looks like, a secret. It is built with the same store API as
``room.memory.demo.load_demo`` (which is unchanged and stays the small set), in a separate temp home.

Twelve sessions from three agents over about three months tell one story: a fictional final-year student,
Maya Ellery, works on side projects, applies for roles and a masters, and changes some decisions and
preferences along the way. Later sessions restate some decisions with new content, so the store keeps the
older copy as ``superseded``.
"""

from __future__ import annotations

import copy
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..memory.store import MemoryStore, Provenance

ME = "Maya Ellery"


def _n(type: str, name: str, summary: str, confidence: float = 0.9) -> dict:
    return {"type": type, "name": name, "summary": summary, "confidence": confidence}


def _e(src: str, relation: str, dst: str) -> dict:
    return {"src": src, "relation": relation, "dst": dst}


PEOPLE = [
    _n("person", ME, "Fictional final-year computing student; builds small tools and wants an applied ML or data role.", 0.97),
    _n("person", "Dr. Oren Halvik", "Thesis advisor at Orvane University; reviews drafts every second Friday.", 0.93),
    _n("person", "Lise Varnholt", "Mentor from the Quillfeather Labs alumni circle; senior data engineer.", 0.86),
    _n("person", "Corin Vasquel", "Teammate on Tern Atlas; owns the offline map renderer.", 0.88),
    _n("person", "Anika Threlfall", "Product designer who helps with Mossboard and Quiet Hours layouts.", 0.82),
    _n("person", "Juno Pemberly", "Friend from the robotics club; co-runs the Driftwatch sensor project.", 0.84),
    _n("person", "Rafael Ostrander", "Hiring manager for the Veltrana Systems graduate programme.", 0.74),
    _n("person", "Hollis Wendt", "Research coordinator at Brindlewood College.", 0.7),
    _n("person", "Priya Kalvane", "Course mate; pairs on statistics coursework and the thesis survey.", 0.8),
    _n("person", "Dmitri Solvang", "Volunteer lead at the Harrowgate Data Co-op.", 0.68),
    _n("person", "Edie Marchetti", "Grant officer at the Cinderpine Foundation.", 0.66),
    _n("person", "Felix Arnauld", "Second reader for the thesis; specialist in signal processing.", 0.78),
]

ORGS = [
    _n("organization", "Orvane University", "Fictional university where Maya studies; offers an MSc in Data Science.", 0.95),
    _n("organization", "Brindlewood College", "Fictional college with a public-health data research group.", 0.83),
    _n("organization", "Quillfeather Labs", "Fictional applied ML lab known for small, careful models.", 0.88),
    _n("organization", "Veltrana Systems", "Fictional logistics software company with a graduate programme.", 0.86),
    _n("organization", "Kestrelwick Health", "Fictional clinic network hiring junior data analysts.", 0.76),
    _n("organization", "Tidewell Robotics", "Fictional robotics startup offering a summer fellowship.", 0.79),
    _n("organization", "Harrowgate Data Co-op", "Fictional volunteer group that cleans open civic data.", 0.72),
    _n("organization", "Cinderpine Foundation", "Fictional foundation that funds student open-source work.", 0.75),
    _n("organization", "Nimbrel Studio", "Fictional two-person design studio taking frontend contractors.", 0.7),
    _n("organization", "Ashgrove Civic Lab", "Fictional city innovation lab with a one-year fellowship.", 0.73),
]

PROJECTS = [
    _n("project", "Tern Atlas", "Offline-first hiking map; caches map tiles per trail and syncs routes later.", 0.94),
    _n("project", "Mossboard", "Small kanban board for study groups with shared columns.", 0.9),
    _n("project", "Driftwatch", "Dashboard for river sensor readings collected by the robotics club.", 0.88),
    _n("project", "Pocket Herbarium", "Phone app that keeps photos and notes of pressed plants.", 0.8),
    _n("project", "Quiet Hours", "Focus timer that silences notifications during study blocks.", 0.85),
    _n("project", "Canopy Budget", "Shared budgeting sheet for student houses.", 0.78),
    _n("project", "Lanternfish", "Local full-text search over personal notes.", 0.87),
    _n("project", "Ferrylog", "Travel journal that groups photos and notes by day.", 0.76),
    _n("project", "Wren Scheduler", "Calendar helper that suggests study slots around lectures.", 0.81),
    _n("project", "Glasswing Thesis", "Final-year thesis on early warnings from noisy river sensor data.", 0.96),
    _n("project", "Orchard Metrics", "Charts for the co-op's volunteer hours and cleaned datasets.", 0.72),
    _n("project", "Saltmarsh Helper", "Chat helper that answers questions about course handbooks.", 0.69),
]

SKILLS = [
    _n("skill", "Statistics", "Comfortable with regression, sampling and hypothesis testing.", 0.9),
    _n("skill", "Technical writing", "Writes design docs and release notes others can follow.", 0.84),
    _n("skill", "Distributed systems", "Has built sync between devices with conflict handling.", 0.77),
    _n("skill", "Data visualization", "Builds clear charts and dashboards.", 0.88),
    _n("skill", "Mobile development", "Two shipped phone apps for small user groups.", 0.8),
    _n("skill", "Relational databases", "Schema design, indexes and migrations.", 0.86),
    _n("skill", "Machine learning", "Classical models and small neural networks for tabular and time-series data.", 0.83),
    _n("skill", "Accessibility", "Checks contrast, focus order and screen-reader labels.", 0.75),
    _n("skill", "Public speaking", "Gave two club talks and one department seminar.", 0.65),
    _n("skill", "Interface design", "Sketches flows and builds clickable prototypes.", 0.79),
    _n("skill", "Software testing", "Writes unit and property tests; likes testing before fixing.", 0.82),
    _n("skill", "Signal processing", "Filtering and anomaly detection on sensor streams.", 0.71),
]

APPLICATIONS = [
    _n("application", "Quillfeather Labs ML internship", "Applied on 4 August; waiting to hear back.", 0.86),
    _n("application", "Veltrana Systems graduate engineer", "Applied on 11 August through the graduate programme.", 0.9),
    _n("application", "Orvane University MSc Data Science", "Preparing documents; deadline 15 January.", 0.92),
    _n("application", "Brindlewood College research assistant", "Part-time role; applied on 20 August.", 0.74),
    _n("application", "Kestrelwick Health data analyst", "Considering whether to apply; closes in November.", 0.62),
    _n("application", "Tidewell Robotics summer fellowship", "Applied on 2 September with the Driftwatch write-up.", 0.79),
    _n("application", "Cinderpine Foundation grant", "Small grant to keep Lanternfish maintained; draft in progress.", 0.77),
    _n("application", "Nimbrel Studio frontend contract", "Short contract for a booking page; applied on 9 September.", 0.7),
    _n("application", "Harrowgate Data Co-op volunteer lead", "Offered to lead the data cleaning nights.", 0.68),
    _n("application", "Ashgrove Civic Lab fellowship", "One-year fellowship; applications open in December.", 0.64),
]

DECISIONS = [
    _n("decision", "Tern Atlas map storage", "Store map tiles as files on the phone, one folder per trail.", 0.85),
    _n("decision", "Mossboard sync model", "Sync the whole board on every change.", 0.8),
    _n("decision", "Thesis topic", "Early warnings for river flooding from noisy sensor data.", 0.93),
    _n("decision", "Driftwatch hosting", "Run the dashboard on a spare laptop in the club room.", 0.77),
    _n("decision", "Canopy Budget pricing", "Keep Canopy Budget free for everyone.", 0.83),
    _n("decision", "Lanternfish ranking", "Rank results by word overlap with the query.", 0.79),
    _n("decision", "Quiet Hours platform", "Build Quiet Hours for desktop first.", 0.81),
    _n("decision", "Ferrylog photo storage", "Keep full-size photos in the journal folder.", 0.74),
    _n("decision", "Study schedule", "Study in two long blocks on weekends.", 0.76),
    _n("decision", "Wren Scheduler release plan", "Release to the whole course at once.", 0.72),
    _n("decision", "Orchard Metrics charting approach", "Hand-made charts in a spreadsheet.", 0.69),
    _n("decision", "Masters or job first", "Apply for both and decide in spring.", 0.88),
]

PREFERENCES = [
    _n("preference", "Work style", "Prefers fully remote roles.", 0.8),
    _n("preference", "Meeting times", "Prefers meetings after 2 pm.", 0.74),
    _n("preference", "Code review style", "Likes small pull requests with a short summary.", 0.86),
    _n("preference", "Note-taking", "Keeps notes in one long file per course.", 0.7),
    _n("preference", "Learning style", "Learns best by building a small project first.", 0.84),
    _n("preference", "Team size", "Prefers teams of three to six people.", 0.78),
    _n("preference", "Interview format", "Prefers take-home tasks over live coding.", 0.72),
    _n("preference", "Feedback style", "Wants direct written feedback within a week.", 0.81),
]

DOCUMENTS = [
    _n("document", "Statement of purpose", "Draft 1 for the Orvane MSc; focuses on sensor data.", 0.85),
    _n("document", "Thesis proposal", "Approved proposal for Glasswing Thesis with a three-phase plan.", 0.92),
    _n("document", "Tern Atlas design doc", "Describes tile caching, route sync and conflict rules.", 0.88),
    _n("document", "Mossboard roadmap", "Plans shared columns, due dates and a dark theme.", 0.76),
    _n("document", "CV draft", "One page; leads with Tern Atlas and the thesis.", 0.9),
    _n("document", "Veltrana cover letter", "Links logistics routing to the Tern Atlas sync work.", 0.83),
    _n("document", "Driftwatch postmortem", "Explains the week of missing readings after a power cut.", 0.8),
    _n("document", "Grant budget sheet", "Costs for hosting, a domain and two design hours.", 0.71),
    _n("document", "Reading list", "Twelve papers on anomaly detection and early warning systems.", 0.77),
    _n("document", "Portfolio page copy", "Short descriptions of five projects with screenshots.", 0.74),
]

TASKS = [
    _n("task", "Ask Dr. Oren Halvik for a reference", "Request a reference letter by December.", 0.88),
    _n("task", "Finish thesis literature review", "Summarise the twelve papers on the reading list.", 0.9),
    _n("task", "Prepare Veltrana interview", "Practise a system design question about route planning.", 0.84),
    _n("task", "Fix Tern Atlas tile cache", "Tiles for long trails are cut off at the cache limit.", 0.8),
    _n("task", "Write Mossboard release notes", "Notes for the shared-columns release.", 0.75),
    _n("task", "Submit Cinderpine grant", "Send the form and budget sheet before the 30 October deadline.", 0.82),
    _n("task", "Book usability sessions", "Five sessions with classmates for Quiet Hours.", 0.7),
    _n("task", "Update portfolio", "Add Driftwatch and the thesis to the portfolio page.", 0.73),
    _n("task", "Review Driftwatch alerts", "Tune the alert threshold so night readings stop paging the club.", 0.76),
    _n("task", "Plan Ferrylog beta", "Pick ten testers and a two-week beta window.", 0.67),
    _n("task", "Draft seminar talk", "Fifteen-minute talk on the thesis for the department seminar.", 0.78),
    _n("task", "Clean co-op volunteer data", "Merge duplicate volunteer rows before the next report.", 0.69),
]

FACTS = [
    _n("fact", "Tern Atlas trail count", "Tern Atlas covers 140 trails in the beta.", 0.82),
    _n("fact", "Driftwatch sensor count", "Driftwatch reads 18 river sensors every five minutes.", 0.85),
    _n("fact", "Mossboard test coverage", "Mossboard has tests for 82% of its code.", 0.7),
    _n("fact", "Canopy Budget beta size", "Eleven student houses use the Canopy Budget beta.", 0.66),
    _n("fact", "Thesis dataset size", "The thesis dataset holds two years of readings from 18 sensors.", 0.89),
]

# -- links (relations from room.memory.store.RELATIONS only) ------------------------------------------------------

E_PEOPLE = (
    [_e(ME, "part_of", "Orvane University"), _e(ME, "part_of", "Harrowgate Data Co-op")]
    + [_e(p, "part_of", o) for p, o in (
        ("Dr. Oren Halvik", "Orvane University"), ("Lise Varnholt", "Quillfeather Labs"),
        ("Corin Vasquel", "Orvane University"), ("Anika Threlfall", "Nimbrel Studio"),
        ("Juno Pemberly", "Orvane University"), ("Rafael Ostrander", "Veltrana Systems"),
        ("Hollis Wendt", "Brindlewood College"), ("Priya Kalvane", "Orvane University"),
        ("Dmitri Solvang", "Harrowgate Data Co-op"), ("Edie Marchetti", "Cinderpine Foundation"),
        ("Felix Arnauld", "Orvane University"))]
    + [_e(ME, "relates_to", p) for p in ("Dr. Oren Halvik", "Lise Varnholt", "Priya Kalvane", "Juno Pemberly")]
)

E_PROJECTS = (
    [_e(ME, "works_on", p["name"]) for p in PROJECTS]
    + [_e(p, "works_on", proj) for p, proj in (
        ("Corin Vasquel", "Tern Atlas"), ("Anika Threlfall", "Mossboard"), ("Anika Threlfall", "Quiet Hours"),
        ("Juno Pemberly", "Driftwatch"), ("Priya Kalvane", "Glasswing Thesis"), ("Dr. Oren Halvik", "Glasswing Thesis"),
        ("Felix Arnauld", "Glasswing Thesis"), ("Dmitri Solvang", "Orchard Metrics"), ("Priya Kalvane", "Canopy Budget"))]
    + [_e(proj, "requires", s) for proj, s in (
        ("Tern Atlas", "Mobile development"), ("Tern Atlas", "Distributed systems"), ("Mossboard", "Interface design"),
        ("Mossboard", "Distributed systems"), ("Driftwatch", "Data visualization"), ("Driftwatch", "Signal processing"),
        ("Pocket Herbarium", "Mobile development"), ("Quiet Hours", "Interface design"), ("Quiet Hours", "Accessibility"),
        ("Canopy Budget", "Relational databases"), ("Lanternfish", "Relational databases"), ("Lanternfish", "Software testing"),
        ("Ferrylog", "Mobile development"), ("Wren Scheduler", "Statistics"), ("Glasswing Thesis", "Machine learning"),
        ("Glasswing Thesis", "Statistics"), ("Glasswing Thesis", "Signal processing"), ("Orchard Metrics", "Data visualization"),
        ("Saltmarsh Helper", "Machine learning"), ("Saltmarsh Helper", "Technical writing"))]
    + [_e(a, "relates_to", b) for a, b in (
        ("Driftwatch", "Glasswing Thesis"), ("Lanternfish", "Saltmarsh Helper"), ("Wren Scheduler", "Quiet Hours"),
        ("Ferrylog", "Pocket Herbarium"), ("Orchard Metrics", "Harrowgate Data Co-op"), ("Tern Atlas", "Ferrylog"))]
)

E_APPLICATIONS = (
    [_e(app, "applied_to", org) for app, org in (
        ("Quillfeather Labs ML internship", "Quillfeather Labs"), ("Veltrana Systems graduate engineer", "Veltrana Systems"),
        ("Orvane University MSc Data Science", "Orvane University"),
        ("Brindlewood College research assistant", "Brindlewood College"),
        ("Kestrelwick Health data analyst", "Kestrelwick Health"),
        ("Tidewell Robotics summer fellowship", "Tidewell Robotics"),
        ("Cinderpine Foundation grant", "Cinderpine Foundation"), ("Nimbrel Studio frontend contract", "Nimbrel Studio"),
        ("Harrowgate Data Co-op volunteer lead", "Harrowgate Data Co-op"),
        ("Ashgrove Civic Lab fellowship", "Ashgrove Civic Lab"))]
    + [_e(ME, "applied_to", app["name"]) for app in APPLICATIONS]
    + [_e(app, "requires", s) for app, s in (
        ("Quillfeather Labs ML internship", "Machine learning"), ("Veltrana Systems graduate engineer", "Distributed systems"),
        ("Kestrelwick Health data analyst", "Statistics"), ("Tidewell Robotics summer fellowship", "Signal processing"),
        ("Nimbrel Studio frontend contract", "Interface design"), ("Brindlewood College research assistant", "Statistics"))]
    + [_e("Tidewell Robotics summer fellowship", "mentions", "Driftwatch"),
       _e("Cinderpine Foundation grant", "mentions", "Lanternfish"),
       _e("Rafael Ostrander", "relates_to", "Veltrana Systems graduate engineer")]
)

E_DECISIONS = [_e(d, "decided_in", proj) for d, proj in (
    ("Tern Atlas map storage", "Tern Atlas"), ("Mossboard sync model", "Mossboard"),
    ("Thesis topic", "Glasswing Thesis"), ("Driftwatch hosting", "Driftwatch"), ("Canopy Budget pricing", "Canopy Budget"),
    ("Lanternfish ranking", "Lanternfish"), ("Quiet Hours platform", "Quiet Hours"),
    ("Ferrylog photo storage", "Ferrylog"), ("Wren Scheduler release plan", "Wren Scheduler"),
    ("Orchard Metrics charting approach", "Orchard Metrics"))] + [
    _e("Study schedule", "relates_to", ME), _e("Masters or job first", "relates_to", ME),
    _e("Masters or job first", "mentions", "Orvane University MSc Data Science"),
    _e("Masters or job first", "mentions", "Veltrana Systems graduate engineer"),
    _e("Thesis topic", "mentions", "Dr. Oren Halvik")]

E_PREFERENCES = [_e(p["name"], "relates_to", ME) for p in PREFERENCES]

E_DOCUMENTS = [_e(d, rel, t) for d, rel, t in (
    ("Statement of purpose", "relates_to", "Orvane University MSc Data Science"),
    ("Thesis proposal", "part_of", "Glasswing Thesis"), ("Tern Atlas design doc", "part_of", "Tern Atlas"),
    ("Mossboard roadmap", "part_of", "Mossboard"), ("CV draft", "relates_to", "Veltrana Systems graduate engineer"),
    ("CV draft", "relates_to", "Quillfeather Labs ML internship"),
    ("Veltrana cover letter", "relates_to", "Veltrana Systems graduate engineer"),
    ("Driftwatch postmortem", "part_of", "Driftwatch"), ("Grant budget sheet", "part_of", "Cinderpine Foundation grant"),
    ("Reading list", "part_of", "Glasswing Thesis"), ("Portfolio page copy", "mentions", "Tern Atlas"),
    ("Portfolio page copy", "mentions", "Driftwatch"), ("Thesis proposal", "mentions", "Felix Arnauld"))]

E_TASKS = [_e(t, rel, x) for t, rel, x in (
    ("Ask Dr. Oren Halvik for a reference", "mentions", "Dr. Oren Halvik"),
    ("Ask Dr. Oren Halvik for a reference", "relates_to", "Orvane University MSc Data Science"),
    ("Finish thesis literature review", "part_of", "Glasswing Thesis"),
    ("Finish thesis literature review", "mentions", "Reading list"),
    ("Prepare Veltrana interview", "relates_to", "Veltrana Systems graduate engineer"),
    ("Prepare Veltrana interview", "mentions", "Rafael Ostrander"),
    ("Fix Tern Atlas tile cache", "part_of", "Tern Atlas"), ("Write Mossboard release notes", "part_of", "Mossboard"),
    ("Submit Cinderpine grant", "relates_to", "Cinderpine Foundation grant"),
    ("Submit Cinderpine grant", "mentions", "Edie Marchetti"), ("Book usability sessions", "part_of", "Quiet Hours"),
    ("Update portfolio", "mentions", "Portfolio page copy"), ("Review Driftwatch alerts", "part_of", "Driftwatch"),
    ("Plan Ferrylog beta", "part_of", "Ferrylog"), ("Draft seminar talk", "relates_to", "Glasswing Thesis"),
    ("Draft seminar talk", "requires", "Public speaking"), ("Clean co-op volunteer data", "part_of", "Orchard Metrics"),
    ("Clean co-op volunteer data", "mentions", "Dmitri Solvang"),
    ("Interview format", "mentions", "Prepare Veltrana interview"))] + [_e(ME, "mentions", t["name"]) for t in TASKS[:6]]

E_FACTS = [_e(f, "relates_to", x) for f, x in (
    ("Tern Atlas trail count", "Tern Atlas"), ("Driftwatch sensor count", "Driftwatch"),
    ("Mossboard test coverage", "Mossboard"), ("Canopy Budget beta size", "Canopy Budget"),
    ("Thesis dataset size", "Glasswing Thesis"))] + [_e("Thesis dataset size", "mentions", "Driftwatch sensor count")]

# -- later sessions: new content for the same decisions, preferences, applications and tasks -------------------

CHANGES_SEPTEMBER = [
    _n("decision", "Tern Atlas map storage", "Moved map tiles into one small database per region after file counts grew.", 0.9),
    _n("decision", "Mossboard sync model", "Sync only the changed cards, with a change log per board.", 0.87),
    _n("preference", "Work style", "Now prefers hybrid roles with two office days for mentoring.", 0.85),
    _n("application", "Quillfeather Labs ML internship", "Interview scheduled for 24 September.", 0.9),
    _n("decision", "Study schedule", "Study in short weekday blocks; weekends stay free.", 0.82),
]

CHANGES_OCTOBER = [
    _n("decision", "Driftwatch hosting", "Moved the dashboard to the club's small home server after the power cut.", 0.84),
    _n("decision", "Lanternfish ranking", "Rank results with term weighting plus a boost for recent notes.", 0.86),
    _n("decision", "Quiet Hours platform", "Switched Quiet Hours to phone first after usability sessions.", 0.8),
    _n("preference", "Meeting times", "Prefers meetings before noon on Tuesdays and Thursdays.", 0.79),
    _n("preference", "Note-taking", "Moved to one note per topic, searched with Lanternfish.", 0.77),
    _n("application", "Veltrana Systems graduate engineer", "Second-round interview booked for 3 November.", 0.92),
    _n("application", "Orvane University MSc Data Science", "Statement of purpose draft 2 sent to Dr. Oren Halvik.", 0.93),
    _n("decision", "Masters or job first", "Leaning towards the job first and the masters part-time.", 0.81),
    _n("task", "Submit Cinderpine grant", "Submitted on 28 October; decision expected in January.", 0.88),
]

SESSIONS: list[tuple[str, str, str, dict]] = [
    ("room-2026-08-03-a1", "claude", "2026-08-03T09:12:00Z", {"nodes": PEOPLE + ORGS, "edges": E_PEOPLE}),
    ("room-2026-08-10-b2", "gemini", "2026-08-10T18:40:00Z", {"nodes": PROJECTS + SKILLS, "edges": E_PROJECTS}),
    ("room-2026-08-18-c3", "hermes", "2026-08-18T20:05:00Z", {"nodes": APPLICATIONS, "edges": E_APPLICATIONS}),
    ("room-2026-08-27-d4", "claude", "2026-08-27T11:30:00Z", {"nodes": DECISIONS, "edges": E_DECISIONS}),
    ("room-2026-09-04-e5", "gemini", "2026-09-04T16:15:00Z", {"nodes": PREFERENCES, "edges": E_PREFERENCES}),
    ("room-2026-09-12-f6", "hermes", "2026-09-12T10:45:00Z", {"nodes": DOCUMENTS, "edges": E_DOCUMENTS}),
    ("room-2026-09-21-g7", "claude", "2026-09-21T14:20:00Z", {"nodes": TASKS, "edges": E_TASKS}),
    ("room-2026-09-29-h8", "gemini", "2026-09-29T19:55:00Z", {"nodes": FACTS, "edges": E_FACTS}),
    ("room-2026-10-06-i9", "hermes", "2026-10-06T08:50:00Z", {"nodes": CHANGES_SEPTEMBER, "edges": [
        _e("Tern Atlas map storage", "mentions", "Relational databases"),
        _e("Mossboard sync model", "requires", "Distributed systems")]}),
    ("room-2026-10-14-j10", "claude", "2026-10-14T13:05:00Z", {"nodes": CHANGES_OCTOBER[:4], "edges": [
        _e("Driftwatch hosting", "mentions", "Driftwatch postmortem"),
        _e("Quiet Hours platform", "mentions", "Book usability sessions")]}),
    ("room-2026-10-22-k11", "gemini", "2026-10-22T17:35:00Z", {"nodes": CHANGES_OCTOBER[4:7], "edges": [
        _e("Note-taking", "mentions", "Lanternfish"),
        _e("Statement of purpose", "mentions", "Dr. Oren Halvik")]}),
    ("room-2026-10-29-l12", "hermes", "2026-10-29T09:10:00Z", {"nodes": CHANGES_OCTOBER[7:], "edges": [
        _e("Masters or job first", "mentions", "Veltrana Systems graduate engineer")]}),
]


_TS = "%Y-%m-%dT%H:%M:%SZ"
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}}) ({'|'.join(_MONTHS)})\b")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _shift_text(text: str, offset: timedelta) -> str:
    """Move "4 August"-style dates by ``offset`` (the story runs from August 2026 to January 2027)."""
    def move(m: re.Match) -> str:
        month = _MONTHS.index(m.group(2)) + 1
        when = datetime(2027 if month == 1 else 2026, month, int(m.group(1))) + offset
        return f"{when.day} {_MONTHS[when.month - 1]}"
    return _DAY_MONTH.sub(move, text)


def date_offset(now: datetime | None = None) -> timedelta:
    """The whole-second offset that puts the newest session two days before ``now``."""
    now = now or datetime.now(timezone.utc)
    newest = max(_parse(ts) for _, _, ts, _ in SESSIONS)
    return timedelta(seconds=round(((now - timedelta(days=2)) - newest).total_seconds()))


def load_rich_demo(home: Path | None = None, relative_dates: bool = False, now: datetime | None = None) -> MemoryStore:
    """Create (or recreate) the rich demo database in ``home`` (default: a new temp dir).

    ``relative_dates`` moves every timestamp (and every date written in a summary) by one constant offset, so
    the newest session was two days before ``now`` (default: the current time) and the span stays the same.
    Session ids are unchanged. Without it, the fixed Aug-Oct 2026 timestamps are used exactly."""
    home = Path(home) if home else Path(tempfile.mkdtemp(prefix="agent-room-rich-demo-"))
    db = home / "memory.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    store = MemoryStore(db)
    store.set_meta("demo", "1")
    offset = date_offset(now) if relative_dates else None
    for session_id, agent, ts, data in SESSIONS:
        if offset is not None:
            ts = (_parse(ts) + offset).strftime(_TS)
            data = copy.deepcopy(data)
            for node in data["nodes"]:
                node["summary"] = _shift_text(node["summary"], offset)
        store.apply_extraction(data, Provenance(session_id, agent, "session"), ts=ts)
    return store
