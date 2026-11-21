"""Command line entry point.

``room agents | ask | status | remember | memory ... | working ... | capture ... | brief | graph | mcp``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
    rem.add_argument("--clipboard", action="store_true", help="read the note from the clipboard (macOS pbpaste)")
    rem.add_argument("--yes", action="store_true", help="with --clipboard: save without the y/N prompt")

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

    sub.add_parser("mcp", help="MCP server over stdio (read-only search/show tools, append-only memory_remember)")

    wk = sub.add_parser("working", help="the working layer: raw, redacted session notes that expire after 14 days")
    wsub = wk.add_subparsers(dest="working_command", required=True)
    wsub.add_parser("list", help="list unexpired working notes")
    wsh = wsub.add_parser("show", help="show one working note")
    wsh.add_argument("note_id")
    wse = wsub.add_parser("search", help="working notes containing all the given words")
    wse.add_argument("words", nargs="+")
    wse.add_argument("-k", type=positive_int, default=10, help="max results (default 10)")
    wpu = wsub.add_parser("purge", help="delete expired working notes")
    wpu.add_argument("--all", dest="everything", action="store_true", help="delete every working note")
    wpr = wsub.add_parser("promote", help="send one working note through extraction into long-term memory")
    wpr.add_argument("note_id")

    cap = sub.add_parser("capture", help="opt-in capture of Claude Code and Antigravity sessions into the working layer")
    csub = cap.add_subparsers(dest="capture_command", required=True)
    ccl = csub.add_parser("claude", help="SessionEnd hook entry: reads the hook input on stdin; always exits 0")
    ccl.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    cag = csub.add_parser("antigravity", help="agy Stop hook entry: reads the hook payload on stdin; prints {} and exits 0")
    cag.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ih = csub.add_parser("install-hook", help="print, merge in, or remove the capture hook (Claude Code or Antigravity)")
    ih.add_argument("--agent", choices=("claude", "antigravity"), default="claude",
                    help="claude: SessionEnd hook in ~/.claude/settings.json (default); "
                         "antigravity: 'agent-room' Stop group in ~/.gemini/config/hooks.json")
    ih.add_argument("--hooks-file", type=Path, help="Antigravity hooks file (default: ~/.gemini/config/hooks.json)")
    act = ih.add_mutually_exclusive_group(required=True)
    act.add_argument("--print", dest="hook_action", action="store_const", const="print", help="print the JSON to add")
    act.add_argument("--apply", dest="hook_action", action="store_const", const="install",
                     help="merge the hook into settings.json (keeps everything else; makes a backup)")
    act.add_argument("--uninstall", dest="hook_action", action="store_const", const="uninstall",
                     help="remove only this hook from settings.json (makes a backup)")
    ih.add_argument("--settings", type=Path, help="settings file (default: ~/.claude/settings.json)")
    csub.add_parser("enable", help="turn capture on")
    csub.add_parser("disable", help="turn capture off")
    csub.add_parser("status", help="show whether capture is on and the hook is installed")

    gr = sub.add_parser("graph", help="export the memory graph as Mermaid (.md) and HTML")
    gr.add_argument("--out", type=Path, help="output path without extension (default: ~/.agent-room/exports/graph)")

    da = sub.add_parser("dashboard-api", help="read-only JSON API over memory for the dashboard (127.0.0.1 only)")
    da.add_argument("--port", type=int, default=8766, help="port on 127.0.0.1 (default: 8766)")
    da.add_argument("--demo", action="store_true", help="serve the made-up demo dataset from a temp home instead")
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


def run_extraction(session_id: str, messages: list[Message]):
    """Extract a finished session into memory. Never raises; Ctrl-C queues the session.

    Returns the ExtractionResult, or None if it was interrupted or failed outright.
    """
    from .memory.extractor import configured_extractors, enqueue, extract_session, session_source

    names, _ = configured_extractors()
    print(f"\n(memory) extracting with {names[0] if names else 'an invalid extractor setting'}... "
          "(Ctrl-C skips; the session is queued)", flush=True)
    try:
        result = asyncio.run(extract_session(session_id, messages, agent_factory))
        print(f"(memory) {result.describe()}", flush=True)
        return result
    except KeyboardInterrupt:
        try:
            enqueue(session_id, session_source(messages), "interrupted by user")
            print("(memory) skipped; session queued for `room memory process`", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"(memory) skipped and could not queue: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"(memory) extraction failed: {exc}", file=sys.stderr)
    return None


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

def _clipboard_note(assume_yes: bool) -> str | None:
    """Clipboard text, redacted, after a preview and a y/N confirmation. None means do not save."""
    from .clipboard import MAX_CLIPBOARD_CHARS, ClipboardError, read_clipboard
    from .memory.redact import redact_secrets

    try:
        raw = read_clipboard()
    except ClipboardError as exc:
        print(f"room remember: {exc}", file=sys.stderr)
        return None
    if not raw.strip():
        print("room remember: the clipboard is empty", file=sys.stderr)
        return None
    if len(raw) > MAX_CLIPBOARD_CHARS:
        print(f"room remember: the clipboard holds {len(raw):,} characters; the limit is {MAX_CLIPBOARD_CHARS:,}. "
              "Save the relevant part to a file and use --file.", file=sys.stderr)
        return None
    text = redact_secrets(raw.strip())  # redact before anything is shown or stored
    preview = text[:300] + ("..." if len(text) > 300 else "")
    print(f"Clipboard: {len(raw):,} characters. Preview (secrets redacted):\n---\n{preview}\n---")
    if not assume_yes:
        try:
            answer = input("Save this to memory? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Not saved.")
            return None
    return text


def cmd_remember(args: argparse.Namespace) -> int:
    from .memory.paths import ensure_outside_repo
    from .memory.redact import redact_secrets

    if args.clipboard and (args.text.strip() or args.file):
        print("room remember: use --clipboard on its own, without text or --file", file=sys.stderr)
        return 2
    if args.clipboard:
        clip = _clipboard_note(args.yes)
        if clip is None:
            return 1
        parts = [clip]
    else:
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
    kind = "clipboard" if args.clipboard else "manual"
    session_id = f"{kind}-{new_session_id()}"
    log = SessionLog(session_id)
    ensure_outside_repo(log.path)
    message = Message(session_id, kind, 0, "user", content)
    log.append(message)
    print(f"Saved note {session_id} ({len(content)} characters) to {log.path}")
    run_extraction(session_id, [message])
    return 0


# -- memory ----------------------------------------------------------------------------

def cmd_memory(args: argparse.Namespace) -> int:
    from .memory import extractor
    from .memory.format import format_node, format_search
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
        from .memory.vault import VaultSafetyError, clear_vault, default_vault, has_managed_vault

        db = memory_db_path()
        targets = [Path(f"{db}{sfx}") for sfx in ("", "-wal", "-shm")]
        dirs = [queue_dir(), db.parent / "briefs", db.parent / "exports"]
        vault = default_vault()
        has_vault = has_managed_vault(vault)
        if not any(p.exists() for p in targets + dirs) and not has_vault:
            print("No memory to purge.")
            return 0
        if not args.yes:
            vault_line = (f"The exported vault copy in {vault} is cleared too (only files room wrote; "
                          "your own files there are kept).\n") if has_vault else ""
            prompt = (f"This permanently deletes {db}, the retry queue, briefs and graph exports.\n"
                      f"{vault_line}"
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
        if has_vault:
            try:
                cleared = clear_vault(vault)
                kept = "" if cleared.vault_deleted else " (folder kept: it holds files you added)"
                print(f"Vault: removed {len(cleared.removed)} file(s) from {vault}{kept}.")
            except (VaultSafetyError, OSError) as exc:
                print(f"Vault not cleared: {exc}", file=sys.stderr)
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
            print(format_search(store.search(args.query, args.k, include_superseded=args.all)))
            return 0
        if cmd == "show":
            nodes = [store.get_node(int(args.node))] if args.node.isdigit() else store.find_nodes(args.node)
            nodes = [n for n in nodes if n]
            if not nodes:
                print(f"No node matches {args.node!r}.")
                return 1
            for node in nodes:
                print(format_node(store, node))
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


# -- working / capture --------------------------------------------------------------------

def cmd_working(args: argparse.Namespace) -> int:
    from .memory import working
    from .memory.paths import ensure_outside_repo

    cmd = args.working_command
    try:
        if cmd == "list":
            notes = working.list_notes()
            print(working.format_list(notes) if notes else "No working notes.")
            return 0
        if cmd == "search":
            results = working.search(" ".join(args.words), args.k)
            print(working.format_search(results) if results else "No working notes match.")
            return 0
        if cmd == "purge":
            removed = working.purge(everything=args.everything)
            print(f"Removed {removed} working note(s){'' if args.everything else ' (expired)'}.")
            return 0
        try:
            note = working.get_note(args.note_id)
        except ValueError:
            print(f"room working: {args.note_id!r} is not a working note id", file=sys.stderr)
            return 2
        if note is None:
            print(f"room working: no unexpired working note {args.note_id}", file=sys.stderr)
            return 1
        if cmd == "show":
            print(working.format_note(note))
            return 0
        # promote: the only path from working into long-term memory
        if note.get("promoted_at"):
            print(f"room working: {note['id']} was already promoted on {note['promoted_at'][:10]}", file=sys.stderr)
            return 1
        from .transcript import load_session

        session_id, messages = working.promotion_messages(note)
        log = SessionLog(session_id)
        ensure_outside_repo(log.path)
        if log.path.exists():  # an earlier promote whose extraction did not finish: retry it as is
            messages = load_session(log.path)
        else:
            for message in messages:
                log.append(message)
        print(f"Promoting {session_id} ({len(messages)} turns) through extraction...")
        result = run_extraction(session_id, messages)
        if result is not None and result.status == "stored":
            working.mark_promoted(note["id"])
            return 0
        working.mark_promote_queued(note["id"])
        print(f"room working: extraction did not finish, so {note['id']} is not marked promoted. "
              "It is queued for `room memory process`; run `room working promote` again to retry.", file=sys.stderr)
        return 1
    except working.WorkingSafetyError as exc:
        print(f"room working: {exc}", file=sys.stderr)
        return 2


def cmd_capture(args: argparse.Namespace) -> int:
    from . import capture

    cmd = args.capture_command
    if cmd == "claude":
        try:
            raw = sys.stdin.read(capture.MAX_HOOK_INPUT + 1)
        except Exception:  # noqa: BLE001 - the hook path always exits 0
            return 0
        return capture.run_worker(raw) if args.worker else capture.run_hook(raw)
    if cmd == "antigravity":
        from . import capture_agy

        try:
            raw = sys.stdin.read(capture.MAX_HOOK_INPUT + 1)
        except Exception:  # noqa: BLE001 - the hook path always exits 0
            raw = ""
        if args.worker:
            return capture_agy.run_worker(raw)
        capture_agy.run_hook(raw)
        try:
            print("{}", flush=True)  # agy reads a JSON object from a hook's stdout; {} lets the agent stop normally
        except Exception:  # noqa: BLE001 - e.g. agy closed the pipe; the hook still exits 0
            try:  # point stdout at /dev/null so the flush at interpreter exit cannot fail either
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                os.close(devnull)
            except Exception:  # noqa: BLE001
                pass
        return 0
    if cmd == "install-hook":
        from . import capture_agy

        agy = args.agent == "antigravity"
        if args.hook_action == "print":
            print(json.dumps(capture_agy.hook_config() if agy else capture.hook_config(), indent=2))
            return 0
        try:
            if agy:
                print(capture_agy.change_hooks(args.hooks_file or capture_agy.default_hooks_path(), args.hook_action))
            else:
                print(capture.change_settings(args.settings or capture.default_settings_path(), args.hook_action))
        except (capture.SettingsError, OSError, ValueError) as exc:
            print(f"room capture: {exc}; the file was not changed.", file=sys.stderr)
            return 2
        return 0
    if cmd in ("enable", "disable"):
        capture.set_enabled(cmd == "enable")
        print(f"Claude Code and Antigravity capture: {'on' if cmd == 'enable' else 'off'}")
        return 0
    from .memory import working

    print(f"Claude Code and Antigravity capture: {'on' if capture.is_enabled() else 'off'}")
    settings = capture.default_settings_path()
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
        groups = (data.get("hooks") or {}).get(capture.HOOK_EVENT) or []
        installed = any(capture.is_our_hook(h) for g in groups if isinstance(g, dict) for h in g.get("hooks", []))
        print(f"SessionEnd hook in {settings}: {'installed' if installed else 'not installed'}")
    except (OSError, ValueError, AttributeError):
        print(f"SessionEnd hook in {settings}: could not read the file")
    from . import capture_agy

    hooks = capture_agy.default_hooks_path()
    try:
        state = capture_agy.install_state(hooks)
    except (OSError, ValueError):
        state = "could not read the file"
    print(f"Antigravity Stop hook ('{capture_agy.HOOK_GROUP}' group) in {hooks}: {state}")
    print(f"Working notes: {len(working.list_notes())} unexpired")
    return 0


def cmd_dashboard_api(args: argparse.Namespace) -> int:
    from .web import api

    try:
        return api.serve(args.port, args.demo)
    except api.PortInUse as exc:
        print(f"room dashboard-api: {exc}", file=sys.stderr)
        return 1


def cmd_mcp() -> int:
    from .mcp_server import serve

    return serve()


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
        "mcp": cmd_mcp,
        "working": lambda: cmd_working(args),
        "capture": lambda: cmd_capture(args),
        "dashboard-api": lambda: cmd_dashboard_api(args),
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
