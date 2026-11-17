import pytest

from room.agents import AGENT_NAMES
from room.cli import build_parser, main
from room.presence import write_presence


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_ask_defaults():
    args = parse("ask", "what is a graph database?")
    assert args.command == "ask"
    assert args.question == "what is a graph database?"
    assert args.agents == list(AGENT_NAMES)
    assert args.rounds == 2
    assert args.synth == "claude"


def test_ask_options():
    args = parse("ask", "q", "--agents", "Hermes, muse,hermes", "--rounds", "1", "--synth", "gemini")
    assert args.agents == ["hermes", "muse"]
    assert args.rounds == 1
    assert args.synth == "gemini"


@pytest.mark.parametrize(
    "argv",
    [
        ["ask", "q", "--agents", "hermes,gpt"],
        ["ask", "q", "--agents", ","],
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
    out = capsys.readouterr().out
    assert "hermes  working" in out and "round 1/2" in out
    assert "muse    idle" in out
