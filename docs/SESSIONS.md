# Session log format (v1)

Every `room ask` run is saved as one JSON Lines file:

```
~/.agent-room/sessions/<session_id>.jsonl
```

(`$AGENT_ROOM_HOME/sessions/` if `AGENT_ROOM_HOME` is set.)

`session_id` looks like `20261117T114200Z-3fa9c1`: UTC start time plus 6 random
hex characters. Messages are appended as they happen, so a crashed run still
leaves a valid, partial log.

## Line format

Each line is one JSON object with exactly these fields, in this order:

| Field        | Type    | Meaning |
|--------------|---------|---------|
| `ts`         | string  | ISO 8601 UTC time the message was recorded, e.g. `"2026-11-17T11:42:09Z"`. |
| `session_id` | string  | Same as the file name. |
| `agent`      | string  | Who produced it: an agent name, `"user"` for the task, `"room"` for notes. |
| `round`      | integer | `0` for the task and pre-run notes, `1..N` for discussion rounds, `N+1` for synthesis. |
| `role`       | string  | One of `user`, `agent`, `note`, `final` (below). |
| `content`    | string  | The text. |

Roles:

- `user`: the original task. Always the first line, `round` 0, `agent` `"user"`.
- `agent`: one agent's reply in a discussion round.
- `note`: a room event, such as an agent being skipped because it is
  unreachable or failed. `agent` is `"room"`. Notes never contain secrets.
- `final`: the synthesized answer. `agent` is whoever wrote it (the configured
  synthesizer, or a fallback agent if that failed). At most one per session;
  missing if every synthesizer failed (a `note` says so).

Example:

```jsonl
{"ts": "2026-11-17T11:42:00Z", "session_id": "20261117T114200Z-3fa9c1", "agent": "user", "round": 0, "role": "user", "content": "In one sentence, what is a graph database?"}
{"ts": "2026-11-17T11:42:01Z", "session_id": "20261117T114200Z-3fa9c1", "agent": "room", "round": 0, "role": "note", "content": "muse unreachable, skipped: muse: API key not configured"}
{"ts": "2026-11-17T11:42:05Z", "session_id": "20261117T114200Z-3fa9c1", "agent": "hermes", "round": 1, "role": "agent", "content": "A graph database stores data as nodes and edges..."}
{"ts": "2026-11-17T11:42:12Z", "session_id": "20261117T114200Z-3fa9c1", "agent": "claude", "round": 2, "role": "final", "content": "A graph database is..."}
```

This format is the input for the planned graph-memory layer, so it is kept
deliberately simple. New fields may be added later; existing fields will keep
their meaning.
