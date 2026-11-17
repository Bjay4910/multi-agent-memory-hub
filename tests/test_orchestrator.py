import asyncio
import json

from conftest import FakeAgent
from room.orchestrator import Room
from room.transcript import ROLE_AGENT, ROLE_FINAL, ROLE_NOTE, ROLE_USER, load_session


def run(room, task="What is 2+2?"):
    return asyncio.run(room.run(task, session_id="test-session"))


def agent_turns(result):
    return [(m.round, m.agent) for m in result.transcript if m.role == ROLE_AGENT]


def test_agents_answer_in_order_for_each_round():
    calls = []
    a, b, c = (FakeAgent(n, calls=calls) for n in ("a", "b", "c"))
    result = run(Room([a, b, c], synthesizer=a, rounds=2))
    assert agent_turns(result) == [(1, "a"), (1, "b"), (1, "c"), (2, "a"), (2, "b"), (2, "c")]
    assert result.final is not None and result.final.agent == "a"
    assert result.final.round == 3 and result.final.role == ROLE_FINAL


def test_each_agent_sees_full_transcript_so_far():
    calls = []
    a, b = FakeAgent("a", calls=calls), FakeAgent("b", calls=calls)
    run(Room([a, b], synthesizer=a, rounds=2))
    # b's first call sees the task and a's round-1 reply
    _, _, seen = calls[1]
    assert seen == [("user", ROLE_USER, "What is 2+2?"), ("a", ROLE_AGENT, "a reply 1")]
    # a's round-2 call sees both round-1 replies
    _, _, seen = calls[2]
    assert [s[0] for s in seen] == ["user", "a", "b"]


def test_single_round():
    a = FakeAgent("a")
    result = run(Room([a], synthesizer=a, rounds=1))
    assert agent_turns(result) == [(1, "a")]
    assert result.final.round == 2


def test_unreachable_agent_is_skipped_with_note():
    a, down = FakeAgent("a"), FakeAgent("down", healthy=False)
    result = run(Room([a, down], synthesizer=a, rounds=2))
    assert down.calls == []
    assert agent_turns(result) == [(1, "a"), (2, "a")]
    notes = [m.content for m in result.transcript if m.role == ROLE_NOTE]
    assert any("down unreachable" in n for n in notes)


def test_failed_reply_is_noted_and_room_continues():
    a, flaky = FakeAgent("a"), FakeAgent("flaky", fail_rounds={1})
    result = run(Room([flaky, a], synthesizer=a, rounds=2))
    assert agent_turns(result) == [(1, "a"), (2, "flaky"), (2, "a")]
    notes = [m for m in result.transcript if m.role == ROLE_NOTE]
    assert notes[0].round == 1 and "flaky skipped this round" in notes[0].content


def test_synthesis_falls_back_when_synthesizer_fails():
    a, synth = FakeAgent("a"), FakeAgent("synth", fail_all=True)
    result = run(Room([a], synthesizer=synth, rounds=1))
    assert result.final.agent == "a"
    assert any("synthesis by synth failed" in m.content for m in result.transcript if m.role == ROLE_NOTE)


def test_unreachable_synthesizer_falls_back():
    a, synth = FakeAgent("a"), FakeAgent("synth", healthy=False)
    result = run(Room([a], synthesizer=synth, rounds=1))
    assert synth.calls == []
    assert result.final.agent == "a"


def test_no_final_when_everything_fails():
    a = FakeAgent("a", fail_all=True)
    result = run(Room([a], synthesizer=a, rounds=1))
    assert result.final is None
    assert "no final answer" in result.transcript[-1].content


def test_synthesizer_receives_synthesis_task():
    calls = []
    a = FakeAgent("a", calls=calls)
    run(Room([a], synthesizer=a, rounds=1), task="Explain X")
    _, synth_task, _ = calls[-1]
    assert "final answer" in synth_task and "Explain X" in synth_task


def test_transcript_persisted_as_jsonl(isolated_home):
    a, b = FakeAgent("a"), FakeAgent("b", healthy=False)
    result = run(Room([a, b], synthesizer=a, rounds=1))
    assert result.log_path == isolated_home / "sessions" / "test-session.jsonl"
    lines = result.log_path.read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    assert all(list(r.keys()) == ["ts", "session_id", "agent", "round", "role", "content"] for r in rows)
    assert all(r["session_id"] == "test-session" for r in rows)
    assert [r["role"] for r in rows] == [ROLE_USER, ROLE_NOTE, ROLE_AGENT, ROLE_FINAL]
    assert rows[0]["content"] == "What is 2+2?"
    assert [(m.agent, m.content) for m in load_session(result.log_path)] == [(m.agent, m.content) for m in result.transcript]


def test_on_message_callback_streams_every_message():
    seen = []
    a = FakeAgent("a")
    result = asyncio.run(Room([a], synthesizer=a, rounds=1, on_message=seen.append).run("q"))
    assert seen == result.transcript


def test_presence_is_idle_after_run(isolated_home):
    from room.presence import read_presence

    a = FakeAgent("a")
    run(Room([a], synthesizer=a, rounds=1))
    entries = read_presence()
    assert [(e.agent, e.status) for e in entries] == [("a", "idle")]
