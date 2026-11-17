"""Command line entry point: ``room agents | ask | status``."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Sequence

from . import __version__, presence
from .agents import AGENT_NAMES, DEFAULT_AGENTS_ENV, build_agent, default_agents, parse_names
from .orchestrator import DEFAULT_ROUNDS, Room
from .transcript import ROLE_AGENT, ROLE_FINAL, ROLE_NOTE, Message


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
    parser = argparse.ArgumentParser(prog="room", description="Local multi-agent room.")
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

    sub.add_parser("status", help="show current presence of all agents")
    return parser


async def cmd_agents() -> int:
    defaults = set(default_agents())
    agents = [build_agent(n) for n in AGENT_NAMES]
    results = await asyncio.gather(*(a.healthcheck() for a in agents))
    for agent, (ok, detail) in zip(agents, results):
        tag = "default" if agent.name in defaults else "opt-in "
        print(f"{'OK  ' if ok else 'DOWN'}  {agent.name:<10} {tag}  {detail}")
    return 0 if any(ok for ok, _ in results) else 1


def print_message(msg: Message) -> None:
    if msg.role == ROLE_AGENT:
        print(f"\n--- [round {msg.round}] {msg.agent} ---\n{msg.content}", flush=True)
    elif msg.role == ROLE_NOTE:
        print(f"\n(note) {msg.content}", flush=True)
    elif msg.role == ROLE_FINAL:
        print(f"\n=== FINAL ANSWER (by {msg.agent}) ===\n{msg.content}", flush=True)


async def cmd_ask(args: argparse.Namespace) -> int:
    names = resolve_agents(args)
    agents = [build_agent(n) for n in names]
    by_name = {a.name: a for a in agents}
    synthesizer = by_name.get(args.synth) or build_agent(args.synth)
    room = Room(agents, synthesizer, rounds=args.rounds, on_message=print_message)
    print(f"Asking {', '.join(names)} ({args.rounds} round(s), synthesis by {args.synth})...", flush=True)
    result = await room.run(args.question)
    print(f"\nSession {result.session_id} saved to {result.log_path}")
    return 0 if result.final else 1


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in ("agents", "ask"):
            default_agents()  # fail early with a clear message if ROOM_DEFAULT_AGENTS is invalid
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if args.command == "agents":
            return asyncio.run(cmd_agents())
        if args.command == "ask":
            return asyncio.run(cmd_ask(args))
        return cmd_status()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
