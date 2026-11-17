"""Command line entry point.

``room agents | ask | status | remember | memory ... | brief | graph``
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Sequence

from . import __version__, presence
from .agents import AGENT_NAMES, DEFAULT_AGENTS_ENV, build_agent, default_agents, parse_names
from .orchestrator import DEFAULT_ROUNDS, Room
from .transcript import ROLE_AGENT, ROLE_FINAL, ROLE_NOTE, Message, SessionLog, new_session_id

# Indirection so tests can swap in fake agents.
agent_factory = build_agent


def parse_agent_list(value: str) -> list[str]:
    try:
        return parse_names(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def resolve_agents(args: argparse.Namespace) -> list[str]:
    """--agents (or the configured defaults), plus every --with agent appended in order."""
    names = list(args.agents) if args.agents else default_agents()
    for extra in args.with_agents or []:
        names.extend(extra)
    return list(dict.fromkeys(names))


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    from .memory.brief import PURPOSES

    parser = argparse.ArgumentParser(prog="room", description="Local multi-agent room with graph memory.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("agents", help="check which agents are reachable")

    ask = sub.add_parser("ask", help="ask the room a question")
    ask.add_argument("question", help="the task or question for the room")
    ask.add_argument("--agents", type=parse_agent_list, default=None,
                     help="comma-separated agents in speaking order, replacing the defaults "
                          f"(default: claude,gemini, or ${DEFAULT_AGENTS_ENV}; available: {','.join(AGENT_NAMES)})")
    ask.add_argument("--with", dest="with_agents", type=parse_agent_list, action="append", metavar="AGENTS",
                     help="add opt-in agents to the list, e.g. --with muse or --with muse,hermes (repeatable)")
    ask.add_argument("--rounds", type=positive_int, default=DEFAULT_ROUNDS,
                     help=f"discussion rounds before synthesis (default: {DEFAULT_ROUNDS})")
    ask.add_argument("--synth", choices=AGENT_NAMES, default="claude",
                     help="agent that writes the final answer (default: claude)")
    ask.add_argument("--show-memory", action="store_true", help="print the memory block injected into prompts")
    ask.add_argument("--no-memory", action="store_true", help="skip memory entirely: no recall and no extraction")

    sub.add_parser("status", help="show current presence of all agents")

    rem = sub.add_parser("remember", help="store a manual note in memory and extract it")
    rem.add_argument("text", nargs="?", default="", help="the note (optional with --file)")
    rem.add_argument("--tag", action="append", default=[], help="tag for the note (repeatable)")
    rem.add_argument("--file", type=Path, help="read the note from a text or markdown file")

    mem = sub.add_parser("memory", help="inspect and manage graph memory")
    msub = mem.add_subparsers(dest="memory_command", required=True)
    s = msub.add_parser("search", help="full-text search over memory")
    s.add_argument("query")
    s.add_argument("-k", type=positive_int, default=10, help="max results (default 10)")
    s.add_argument("--all", action="store_true", help="include superseded nodes")
    sh = msub.add_parser("show", help="show one node by id or name, with links and sources")
    sh.add_argument("node", help="node id or name")
    msub.add_parser("stats", help="counts of nodes, edges, sessions and queued work")
    msub.add_parser("process", help="retry extraction for queued sessions")
    fg = msub.add_parser("forget", help="delete a node and its edges")
    fg.add_argument("node_id", type=int)
    pg = msub.add_parser("purge", help="delete ALL memory (asks for confirmation)")
    pg.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    dm = msub.add_parser("demo", help="load a made-up sample dataset into a separate demo home")
    dm.add_argument("--home", type=Path, help="directory for the demo (default: a new temp dir)")
    ev = msub.add_parser("eval", help="run the offline recall evaluation")
    ev.add_argument("--dir", type=Path, help="eval fixture directory (default: tests/eval)")
    ev.add_argument("--verbose", action="store_true", help="list questions with missed gold nodes")
    ex = msub.add_parser("export", help="write memory as markdown notes into a private vault folder")
    ex.add_argument("--vault", type=Path, help="vault folder (default: ~/.agent-room/vault)")
    ex.add_argument("--include-superseded", action="store_true", help="also export superseded (outdated) nodes, marked as such")
    ex.add_argument("--i-know-this-is-a-repo", dest="allow_repo", action="store_true",
                    help="allow a vault inside a git work tree (personal data could get committed)")

    br = sub.add_parser("brief", help="write a markdown context brief to paste into any chat app")
    br.add_argument("--for", dest="purpose", choices=PURPOSES, default="applications")
    br.add_argument("--out", type=Path, help="output file (default: ~/.agent-room/briefs/<purpose>-<date>.md)")

    gr = sub.add_parser("graph", help="export the memory graph as Mermaid (.md) and HTML")
    gr.add_argument("--out", type=Path, help="output path without extension (default: ~/.agent-room/exports/graph)")
    return parser


# -- agents / status ---------------------------------------------------------------

async def cmd_agents() -> int:
    defaults = set(default_agents())
    agents = [agent_factory(n) for n in AGENT_NAMES]
    results = await asyncio.gather(*(a.healthcheck() for a in agents))
    for agent, (ok, detail) in zip(agents, results):
        tag = "default" if agent.name in defaults else "opt-in "
        print(f"{'OK  ' if ok else 'DOWN'}  {agent.name:<10} {tag}  {detail}")
    return 0 if any(ok for ok, _ in results) else 1


def cmd_status() -> int:
    entries = presence.read_presence()
    if not entries:
        print("No presence files yet.")
        return 0
    for e in entries:
        status = e.effective_status()
        stale = " (stale)" if e.is_stale() else ""
        task = f"  {e.task}" if status == presence.STATUS_WORKING else ""
        print(f"{e.agent:<10} {status:<7}{stale}  updated {e.age_seconds():.0f}s ago  pid {e.pid}{task}")
    return 0


# -- ask -----------------------------------------------------------------------------

def print_message(msg: Message) -> None:
    if msg.role == ROLE_AGENT:
        print(f"\n--- [round {msg.round}] {msg.agent} ---\n{msg.content}", flush=True)
    elif msg.role == ROLE_NOTE:
        print(f"\n(note) {msg.content}", flush=True)
    elif msg.role == ROLE_FINAL:
        print(f"\n=== FINAL ANSWER (by {msg.agent}) ===\n{msg.content}", flush=True)


def recall_block(task: str) -> str:
    """The memory block for ``task``; empty if there is no memory yet. Never raises."""
    from .memory.paths import memory_db_path
    from .memory.recall import recall
    from .memory.store import MemoryStore

    try:
        if not memory_db_path().exists():
            return ""
        with MemoryStore() as store:
            return recall(store, task).block
    except Exception as exc:  # noqa: BLE001 - memory problems must not stop the room
        print(f"(memory) recall skipped: {exc}", file=sys.stderr)
        return ""


def run_extraction(session_id: str, messages: list[Message]) -> None:
    """Extract a finished session into memory. Never raises; Ctrl-C queues the session."""
    from .memory.extractor import configured_extractors, enqueue, extract_session, session_source

    names, _ = configured_extractors()
    print(f"\n(memory) extracting with {names[0] if names else 'an invalid extractor setting'}... "
          "(Ctrl-C skips; the session is queued)", flush=True)
    try:
        result = asyncio.run(extract_session(session_id, messages, agent_factory))
        print(f"(memory) {result.describe()}", flush=True)
    except KeyboardInterrupt:
        try:
            enqueue(session_id, session_source(messages), "interrupted by user")
            print("(memory) skipped; session queued for `room memory process`", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"(memory) skipped and could not queue: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"(memory) extraction failed: {exc}", file=sys.stderr)


def cmd_ask(args: argparse.Namespace) -> int:
    names = resolve_agents(args)
    agents = [agent_factory(n) for n in names]
    by_name = {a.name: a for a in agents}
    synthesizer = by_name.get(args.synth) or agent_factory(args.synth)

    block = "" if args.no_memory else recall_block(args.question)
    if args.show_memory:
        if args.no_memory:
            print("(memory) disabled with --no-memory")
        else:
            print(f"--- injected memory ---\n{block or '(no relevant memory found)'}\n--- end memory ---")

    room = Room(agents, synthesizer, rounds=args.rounds, on_message=print_message, memory_block=block)
    print(f"Asking {', '.join(names)} ({args.rounds} round(s), synthesis by {args.synth})...", flush=True)
    result = asyncio.run(room.run(args.question))
    print(f"\nSession {result.session_id} saved to {result.log_path}")
    if not args.no_memory:
        run_extraction(result.session_id, result.transcript)
    return 0 if result.final else 1


# -- remember ---------------------------------------------------------------------------

def cmd_remember(args: argparse.Namespace) -> int:
    from .memory.paths import ensure_outside_repo
    from .memory.redact import redact_secrets

    parts = [args.text.strip()] if args.text.strip() else []
    if args.file:
        try:
            parts.append(args.file.expanduser().read_text(encoding="utf-8").strip())
        except OSError as exc:
            print(f"room remember: cannot read {args.file}: {exc.strerror}", file=sys.stderr)
            return 2
    body = "\n\n".join(p for p in parts if p)
    if not body:
        print("room remember: give the note as text or with --file", file=sys.stderr)
        return 2
    if args.tag:
        body = f"Tags: {', '.join(args.tag)}\n\n{body}"
    content = redact_secrets(body)
    session_id = f"manual-{new_session_id()}"
    log = SessionLog(session_id)
    ensure_outside_repo(log.path)
    message = Message(session_id, "manual", 0, "user", content)
    log.append(message)
    print(f"Saved note {session_id} ({len(content)} characters) to {log.path}")
    run_extraction(session_id, [message])
    return 0


# -- memory ----------------------------------------------------------------------------

def _summary(text: str, limit: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def cmd_memory(args: argparse.Namespace) -> int:
    from .memory import extractor
    from .memory.paths import memory_db_path, queue_dir
    from .memory.store import MemoryStore

    cmd = args.memory_command
    if cmd == "demo":
        from .memory.demo import load_demo

        with load_demo(args.home) as demo:
            s = demo.stats()
            home = demo.path.parent
        print(f"Loaded made-up demo memory ({s['nodes_active']} active nodes, {s['nodes_superseded']} superseded, "
              f"{s['edges']} edges) into {home}")
        print("Your real memory was not touched. To use the demo in this shell:")
        print(f"  export AGENT_ROOM_HOME={home}")
        print("  room memory stats; room graph; room brief --for applications")
        print("When done: unset AGENT_ROOM_HOME")
        return 0
    if cmd == "eval":
        from .memory.evaluation import default_eval_dir, evaluate

        directory = args.dir or default_eval_dir()
        if not (directory / "questions.json").exists():
            print(f"room memory eval: no fixtures in {directory} (use --dir)", file=sys.stderr)
            return 2
        report = evaluate(directory)
        print(report.format(verbose=args.verbose))
        return 0 if report.stale_violations == 0 else 1
    if cmd == "export":
        from .memory.vault import VaultSafetyError, export_vault

        try:
            res = export_vault(args.vault, args.include_superseded, args.allow_repo)
        except VaultSafetyError as exc:
            print(f"room memory export: {exc}", file=sys.stderr)
            return 2
        print(f"Vault: {res.vault} (private: personal data, keep it local)")
        print(f"  {res.written} files: {len(res.created)} new, {len(res.updated)} updated, "
              f"{len(res.unchanged)} unchanged; {len(res.removed)} removed")
        for rel, reason in res.refused:
            print(f"  refused {rel}: {reason}", file=sys.stderr)
        return 1 if res.refused else 0
    if cmd == "process":
        queued = extractor.queued_sessions()
        if not queued:
            print("Nothing queued.")
            return 0
        print(f"Retrying {len(queued)} queued session(s)...")
        results = asyncio.run(extractor.process_queue(agent_factory, lambda r: print(f"  {r.session_id}: {r.describe()}")))
        return 0 if all(r.status != "queued" for r in results) else 1
    if cmd == "purge":
        db = memory_db_path()
        targets = [Path(f"{db}{sfx}") for sfx in ("", "-wal", "-shm")]
        dirs = [queue_dir(), db.parent / "briefs", db.parent / "exports"]
        if not any(p.exists() for p in targets + dirs):
            print("No memory to purge.")
            return 0
        if not args.yes:
            prompt = (f"This permanently deletes {db}, the retry queue, briefs and graph exports.\n"
                      "Session logs are kept. Type 'purge' to confirm: ")
            try:
                answer = input(prompt)
            except EOFError:
                answer = ""
            if answer.strip() != "purge":
                print("Aborted; nothing was deleted.")
                return 1
        for p in targets:
            p.unlink(missing_ok=True)
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
        print("Memory purged.")
        return 0

    if not memory_db_path().exists():
        print("No memory yet. Use `room ask` or `room remember` to add some.")
        return 0 if cmd in ("stats", "search") else 1
    with MemoryStore() as store:
        if cmd == "stats":
            s = store.stats()
            size_kb = sum(Path(f"{store.path}{x}").stat().st_size for x in ("", "-wal") if Path(f"{store.path}{x}").exists()) / 1024
            print(f"Memory: {store.path} ({size_kb:.0f} KB, schema v{s['schema_version']}{', DEMO data' if store.is_demo else ''})")
            print(f"  nodes:    {s['nodes_active']} active, {s['nodes_superseded']} superseded")
            print(f"  edges:    {s['edges']}")
            print(f"  sources:  {s['sessions']} ({s['manual_notes']} manual notes)")
            print(f"  queued:   {len(extractor.queued_sessions())} session(s) waiting for `room memory process`")
            if s["by_type"]:
                print("  by type:  " + ", ".join(f"{t} {c}" for t, c in s["by_type"].items()))
            if s["by_relation"]:
                print("  by link:  " + ", ".join(f"{r} {c}" for r, c in s["by_relation"].items()))
            return 0
        if cmd == "search":
            results = store.search(args.query, args.k, include_superseded=args.all)
            if not results:
                print("No matches.")
                return 0
            for node, score in results:
                flag = "" if node.status == "active" else " (superseded)"
                print(f"#{node.id:<4} {node.type:<12} {node.name}{flag}\n       {_summary(node.summary)}")
            return 0
        if cmd == "show":
            nodes = [store.get_node(int(args.node))] if args.node.isdigit() else store.find_nodes(args.node)
            nodes = [n for n in nodes if n]
            if not nodes:
                print(f"No node matches {args.node!r}.")
                return 1
            for node in nodes:
                print(f"#{node.id} {node.name}  [{node.type}, {node.status}, confidence {node.confidence:.2f}]")
                if node.summary:
                    print(f"  {node.summary}")
                if node.aliases:
                    print(f"  aliases: {node.aliases.replace('|', ', ')}")
                print(f"  created {node.created_at}, updated {node.updated_at}")
                for edge, other in store.edges_of(node.id):
                    arrow = f"{edge.relation} -> {other.name}" if edge.src == node.id else f"<- {edge.relation} {other.name}"
                    note = "" if other.status == "active" else ", superseded"
                    print(f"  link: {arrow} (#{other.id}{note})")
                for p in store.provenance(node.id):
                    print(f"  source: {p['source']} {p['session_id']} via {p['agent']} at {p['ts']}")
            return 0
        if cmd == "forget":
            node = store.get_node(args.node_id)
            if not node or not store.forget(args.node_id):
                print(f"No node #{args.node_id}.")
                return 1
            print(f"Forgot #{node.id} {node.name} and its links.")
            return 0
    return 2


# -- brief / graph ---------------------------------------------------------------------

def cmd_brief(args: argparse.Namespace) -> int:
    from .memory.brief import write_brief
    from .memory.store import MemoryStore

    with MemoryStore() as store:
        path = write_brief(store, args.purpose, args.out)
    print(path)
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    from .memory.export import write_graph
    from .memory.store import MemoryStore

    with MemoryStore() as store:
        md, html = write_graph(store, args.out)
    print(f"Mermaid: {md}\nHTML:    {html}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from .memory.paths import RepoWriteError

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in ("agents", "ask"):
            default_agents()  # fail early with a clear message if ROOM_DEFAULT_AGENTS is invalid
    except ValueError as exc:
        parser.error(str(exc))
    handlers = {
        "agents": lambda: asyncio.run(cmd_agents()),
        "ask": lambda: cmd_ask(args),
        "status": cmd_status,
        "remember": lambda: cmd_remember(args),
        "memory": lambda: cmd_memory(args),
        "brief": lambda: cmd_brief(args),
        "graph": lambda: cmd_graph(args),
    }
    try:
        return handlers[args.command]()
    except RepoWriteError as exc:
        print(f"room: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
