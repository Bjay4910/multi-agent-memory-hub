import pytest

from room.agents import AGENT_NAMES, default_agents
from room.cli import build_parser, main, resolve_agents
from room.presence import write_presence


def parse(*argv):
    return build_parser().parse_args(list(argv))


def agents_for(*argv):
    return resolve_agents(parse("ask", "q", *argv))


def test_ask_defaults():
    args = parse("ask", "what is a graph database?")
    assert args.command == "ask"
    assert args.question == "what is a graph database?"
    assert args.agents is None and args.with_agents is None
    assert args.rounds == 2
    assert args.synth == "claude"


def test_default_agents_are_claude_and_gemini(monkeypatch):
    monkeypatch.delenv("ROOM_DEFAULT_AGENTS", raising=False)
    assert agents_for() == ["claude", "gemini"]


def test_muse_and_hermes_are_opt_in(monkeypatch):
    monkeypatch.delenv("ROOM_DEFAULT_AGENTS", raising=False)
    assert "muse" not in agents_for() and "hermes" not in agents_for()
    assert agents_for("--with", "muse") == ["claude", "gemini", "muse"]
    assert agents_for("--with", "muse", "--with", "hermes") == ["claude", "gemini", "muse", "hermes"]
    assert agents_for("--with", "hermes,muse,claude") == ["claude", "gemini", "hermes", "muse"]


def test_agents_replaces_defaults_and_with_appends():
    assert agents_for("--agents", "hermes") == ["hermes"]
    assert agents_for("--agents", "hermes", "--with", "muse") == ["hermes", "muse"]
    assert agents_for("--agents", "gemini-api") == ["gemini-api"]


def test_defaults_configurable_by_env(monkeypatch):
    monkeypatch.setenv("ROOM_DEFAULT_AGENTS", "Hermes, claude")
    assert agents_for() == ["hermes", "claude"]
    assert agents_for("--with", "muse") == ["hermes", "claude", "muse"]


def test_invalid_default_env_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("ROOM_DEFAULT_AGENTS", "claude,gpt")
    with pytest.raises(ValueError):
        default_agents()
    with pytest.raises(SystemExit) as exc:
        main(["ask", "q"])
    assert exc.value.code == 2
    assert "ROOM_DEFAULT_AGENTS" in capsys.readouterr().err


def test_ask_options():
    args = parse("ask", "q", "--agents", "Hermes, muse,hermes", "--rounds", "1", "--synth", "gemini")
    assert args.agents == ["hermes", "muse"]
    assert args.rounds == 1
    assert args.synth == "gemini"


def test_gemini_api_is_known_but_not_default(monkeypatch):
    monkeypatch.delenv("ROOM_DEFAULT_AGENTS", raising=False)
    assert "gemini-api" in AGENT_NAMES
    assert "gemini-api" not in agents_for()
    assert parse("ask", "q", "--synth", "gemini-api").synth == "gemini-api"


@pytest.mark.parametrize(
    "argv",
    [
        ["ask", "q", "--agents", "hermes,gpt"],
        ["ask", "q", "--agents", ","],
        ["ask", "q", "--with", "gpt"],
        ["ask", "q", "--with"],
        ["ask", "q", "--rounds", "0"],
        ["ask", "q", "--rounds", "two"],
        ["ask", "q", "--synth", "nobody"],
        ["ask"],
        [],
    ],
)
def test_invalid_arguments_exit(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        parse(*argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["agents", "status"])
def test_simple_commands_parse(command):
    assert parse(command).command == command


def test_status_with_no_presence(capsys):
    assert main(["status"]) == 0
    assert "No presence files" in capsys.readouterr().out


def test_status_lists_agents(capsys):
    write_presence("hermes", "working", "session s: round 1/2")
    write_presence("muse", "idle")
    assert main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:2] == ["hermes", "working"] and "round 1/2" in lines[0]
    assert lines[1].split()[:2] == ["muse", "idle"]
