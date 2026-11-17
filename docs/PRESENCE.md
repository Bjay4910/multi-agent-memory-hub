# Presence contract (v1)

The room publishes which agent is working right now as small JSON files.
Other local programs, such as a power manager, can read them to decide whether
the machine is busy. This document is the contract; `src/room/presence.py`
implements the writer side and a reference reader (`read_presence`).

## Location

```
~/.agent-room/presence/<agent>.json
```

- `<agent>` is the agent name: `claude`, `gemini`, `hermes`, `muse` or `gemini-api`,
  or `memory` while graph-memory extraction runs after a session
  (new agents may be added; consumers should glob `*.json`, not hard-code names).
- If the environment variable `AGENT_ROOM_HOME` is set, the base directory is
  `$AGENT_ROOM_HOME` instead of `~/.agent-room`.
- The directory may not exist until the first run. Treat a missing directory
  as "no agent is working".

## File format

One JSON object, UTF-8, with exactly these fields:

| Field        | Type    | Meaning |
|--------------|---------|---------|
| `agent`      | string  | Agent name, same as the file name without `.json`. |
| `status`     | string  | `"working"` or `"idle"`. No other values. |
| `task`       | string  | Short label of what the agent is doing, at most 120 characters. Empty string when idle. |
| `pid`        | integer | PID of the `room` process that wrote the file (for the CLI agents `claude` and `gemini`, the `room` process, not the `claude`/`agy` child). |
| `updated_at` | string  | ISO 8601 UTC timestamp with `Z` suffix and second precision, e.g. `"2026-11-17T11:42:05Z"`. |

Example:

```json
{"agent": "gemini", "status": "working", "task": "session 20261117T114200Z-3fa9c1: round 1/2", "pid": 48121, "updated_at": "2026-11-17T11:42:05Z"}
```

### What `task` contains

`task` never contains prompt content, model output or secrets. It is a label
built only from the session id and the step, in one of these forms:

- `session <session_id>: round <n>/<total>`
- `session <session_id>: synthesis`
- `memory extraction: session <session_id>` (agent `memory`)

## Writer guarantees

1. **Atomic writes.** Each update writes a temporary file in the same
   directory (named `.<agent>.<random>.tmp`), flushes and fsyncs it, then
   renames it over `<agent>.json` with `os.replace`. A reader therefore sees
   either the previous complete file or the new complete file, never a partial
   one.
2. **Working before the call.** `status: "working"` is written immediately
   before the model call starts.
3. **Heartbeat.** While the call is running, the file is rewritten every
   **30 seconds** with a fresh `updated_at` (same `status` and `task`).
4. **Idle afterwards.** `status: "idle"` with `task: ""` is written when the
   call ends, whether it succeeded, failed or was cancelled (a `finally`
   block). Files are left in place after a run; they are not deleted.

## Reader rules

1. Glob `*.json` in the presence directory. Ignore files starting with `.`
   (temporary files) and any file that does not parse or lacks a required field.
2. An agent is **working** only if `status == "working"` **and**
   `now - updated_at <= 120 seconds`.
3. A `"working"` entry older than **120 seconds** is **stale** and must be
   treated as idle. This covers a `room` process that was killed (for example
   with `kill -9` or a power loss) before it could write `idle`. 120 s is four
   missed heartbeats.
4. `"idle"` entries are idle regardless of age.
5. Optionally, a reader may also check whether `pid` is still alive, but the
   timestamp rule above is the contract and is sufficient on its own.

Reference implementation: `room.presence.read_presence()` and
`PresenceEntry.effective_status()`. `room status` prints the effective status.

## Versioning

This is version 1 of the contract. Fields will not be removed or change meaning
without a new version; new optional fields may be added, so readers should
ignore unknown fields.
