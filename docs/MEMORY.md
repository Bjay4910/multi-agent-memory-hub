# Graph memory

The room keeps a small, local knowledge graph about you and your work. After a
session it extracts facts into the graph; before each agent call it recalls the
relevant part and adds it to the agent's prompt. Everything runs on your machine
and, by default, costs nothing: extraction uses Hermes through Ollama.

```
room ask ──► recall (SQLite FTS5) ──► memory block at the start of each called agent's prompt
   │
   └─ after the session ──► redact secrets ──► extractor (Hermes) ──► validate ──► memory.db
                                                    │ invalid / down
                                                    └─► repair prompt ─► fallback agent ─► queue
```

## Privacy model

- **Location.** All memory data lives in `~/.agent-room/` (or `$AGENT_ROOM_HOME`):
  `memory.db`, `memory_queue/`, `briefs/`, `exports/`. Every memory write checks
  its target path and refuses anything inside this repository. Those paths are
  also listed in `.gitignore`. The database and briefs are created with mode `0600`.
- **Secrets.** Before any text reaches an extractor, it is passed through
  `room.memory.redact.redact_secrets`. That function removes known key formats
  (`AIza…`, `sk-…`, `gh*_…`, `github_pat_…`, `LLM|…`, `xox*-…`, `AKIA…`, JWTs,
  `Bearer …`, PEM private keys), `api_key=…`/`password: …` style values, long
  high-entropy tokens, and the actual values of `GEMINI_API_KEY` and
  `MUSE_API_KEY`. Extracted names and summaries are redacted again before
  storage, and manual notes are redacted before they are saved.
- **What leaves the machine.** With the default extractor (Hermes), nothing
  leaves for memory purposes. Recalled memory is sent to the agents you call in
  that session, as part of their prompt: an opt-in agent such as Muse receives
  memory only in sessions where it is actually called. A cloud extractor is used
  only if you set `MEMORY_EXTRACTOR=claude` or `gemini`.
- **Control.** `room ask --no-memory` skips recall and extraction for a run.
  `room memory forget <id>` deletes a node and its links.
  `room memory purge` deletes the database, queue, briefs and exports after you
  type `purge`, and clears the files room wrote to the vault (see
  [Vault export](#vault-export)). Session logs are kept.

## Storage: `~/.agent-room/memory.db`

SQLite in WAL mode. Every write batch runs in one `BEGIN IMMEDIATE`
transaction, so a session's extraction is stored completely or not at all.
Migrations are versioned in `schema_version` (currently **2**) and applied in order.

| Table | Columns |
|-------|---------|
| `nodes` | `id`, `type`, `name`, `norm_name`, `summary`, `aliases` (`|`-separated), `status` (`active` / `superseded`), `confidence` (0–1), `created_at`, `updated_at` |
| `edges` | `id`, `src` → nodes, `dst` → nodes, `relation`, `created_at`; unique on (`src`, `dst`, `relation`) |
| `provenance` | `id`, `node_id` **or** `edge_id`, `session_id`, `agent` (the extractor), `source` (`session` / `manual` / `clipboard` / `mcp-desktop`), `ts` |
| `nodes_fts` | FTS5 over `name`, `summary`, `aliases` (Porter stemming), kept in sync by triggers |
| `meta` | key/value; `demo=1` marks the made-up demo database |
| `schema_version` | one row: the applied migration version |

Timestamps are ISO 8601 UTC with a `Z` suffix.

**Node types:** `person`, `organization`, `project`, `decision`, `fact`, `task`,
`document`, `preference`, `application`, `skill`.

**Relations:** `relates_to`, `part_of`, `decided_in`, `supersedes`, `mentions`,
`works_on`, `applied_to`, `requires`.

### Dedupe, merge and supersede

- A node's identity is (`type`, normalized name). Normalization lowercases the
  name, turns punctuation and underscores into spaces, and drops a leading "the".
  So "The Agent-Room" and "agent_room" are the same project.
- For **person, organization, project, document and skill** nodes, new
  information is **merged**: new sentences are appended to the summary,
  confidence becomes the maximum, and a differently spelled name becomes an alias.
- For **fact, decision, preference, application and task** nodes, a newer,
  different summary **supersedes** the old node. The old node is kept with
  `status = 'superseded'`, a new node is created, a `supersedes` edge links new
  to old, and the old node's other links are copied to the new one. Repeating
  the same information merges instead.
- An extractor can also say `A supersedes B` explicitly.
- Superseded nodes are history: `room memory show` and `room memory search --all`
  display them, but recall never returns them.

## Extraction

After each `room ask` (unless `--no-memory`) and after `room remember`:

1. **Source text.** By default this is only your own words: the task you typed,
   or the manual note. `MEMORY_EXTRACT_FROM=session` also includes the final
   answer. The default exists because a real run showed a feedback loop: an
   answer built from recalled memory, including a `[X]%` placeholder, was stored
   back as a "project result". The text is redacted and capped at 8,000 characters.
2. **Extractor.** `MEMORY_EXTRACTOR` is `hermes` (default), `claude` or `gemini`.
   The prompt asks for strict JSON in this shape:
   `{"nodes":[{type,name,summary,confidence}],"edges":[{src,relation,dst}]}`.
   With Hermes, Ollama's structured outputs constrain decoding to the JSON
   Schema (`EXTRACTION_SCHEMA`), so node types and relations can only be valid
   values.
3. **Validation.** Every output is validated against the schema, whichever
   backend produced it: types, lengths, confidence in [0, 1], known relations,
   no extra keys.
4. **Repair.** If the output is invalid, one repair prompt lists the errors.
5. **Fallback.** If it is still invalid, or the agent failed or timed out
   (`MEMORY_EXTRACT_TIMEOUT`, default 300 s), `MEMORY_EXTRACTOR_FALLBACK` is
   tried if set.
6. **Queue.** Otherwise the session is written to
   `~/.agent-room/memory_queue/<session_id>.json` with the attempt count and
   last error. `room memory process` retries it later.

While extracting, the presence file `~/.agent-room/presence/memory.json` says
`working` (see [PRESENCE.md](PRESENCE.md)). Extraction runs after the answer is
printed, and no failure in it changes the room's result. Ctrl-C during
extraction queues the session.

## Recall

Before the agents are called, the task text is turned into an FTS5 query (stop
words removed, terms OR-ed) and:

1. BM25 picks the top `MEMORY_TOP_K` (default 8) **active** nodes.
   Columns are weighted name 10, aliases 6, summary 2.
2. Their 1-hop neighbours are added (active nodes only; `supersedes` edges are
   not followed).
3. Each node is scored as `relevance × (0.5 + 0.5·confidence) × (0.6 + 0.4·recency)`.
   - `relevance` is BM25 normalized to the best hit and mapped to 0.3–1.
   - A neighbour gets half of its parent's relevance.
   - `recency` halves every 90 days.
4. Nodes are rendered best-first, then the links between them, until
   `MEMORY_TOKEN_BUDGET` (default 800 tokens, estimated as 4 characters per
   token) is used:

```
Relevant memory (may be outdated, verify):
- Power Manager (project, 2026-11-17): Keeps the Mac awake while agents work.
- Me (person, 2026-11-17): Applying for a CS masters and jobs.
Links:
- Me works_on Power Manager
```

The block goes at the very start of each called agent's prompt. It is not
written to the session log, so it is never extracted again. `--show-memory`
prints it.

## Evaluation

`room memory eval` runs 25 questions over a fixed, pre-extracted synthetic graph
(`tests/eval/`, a fictional person, no LLM calls). The graph is loaded through
the normal store logic, so dedupe and supersede are exercised too. It reports:

- recall@3, recall@8, precision@3 and MRR over the gold nodes
- a stale-fact check: five questions whose answer changed over time; the
  superseded text must not appear in the memory block, and the current text must

## Vault export

`room memory export [--vault PATH] [--include-superseded]` writes memory as
plain markdown notes that other tools (Obsidian, editors, grep, scripts) can
read. No network calls and no model calls are made.

> **The vault is personal data.** It contains the same facts as `memory.db`,
> in readable form. Keep it private and on this machine. Don't put it in a
> cloud-synced folder, a shared drive or a git repository.

Layout (default `~/.agent-room/vault`; set `MEMORY_VAULT` to use another
folder for both export and purge):

```
vault/
  index.md                          every note, grouped by type
  people/me-1.md
  projects/agent-room-2.md
  decisions/ facts/ tasks/ ...      one folder per node type
  .room-vault.json                  list of files the exporter manages
```

- **Filenames** are `<slug>-<node id>.md`: lowercase ASCII words, cut at a
  word boundary so the name stays under 32 characters. The same node always
  gets the same path; a renamed node gets a new path and the old note is removed.
- **Frontmatter:** `id`, `type`, `name`, `created`, `updated`, `status`
  (`active` / `superseded`), `confidence`, `aliases`, and `sources` (a list of
  `source:session_id`, where source is `session`, `manual`, `clipboard` or
  `mcp-desktop`).
- **Body:** the summary, then a `## Related` section with one line per edge:
  `- part_of [[projects/agent-room-2|Agent Room]]` for outgoing links and
  `- [[people/me-1|Me]] works_on this` for incoming ones. Links use the note
  path plus the display name, so they resolve in Obsidian whatever the folder.
- **Superseded nodes** are left out by default. With `--include-superseded`
  they are exported with `status: superseded`, a "SUPERSEDED: this is outdated
  history" banner, a `(superseded)` mark on links to them, and their own
  section in `index.md`.
- **Idempotent.** Re-running writes only changed notes, adds new ones, and
  deletes notes for nodes that were forgotten, purged or are no longer
  exported. Only files listed in `.room-vault.json` are ever changed or
  deleted. Your own notes in the folder are left alone, and an existing file
  that room didn't write is never overwritten.
- **`room memory forget` does not touch the vault.** The forgotten node's note
  stays there until you run `room memory export` again.
- **`room memory purge` clears the vault copy.** The confirmation prompt says
  so, and afterwards purge prints how many vault files it removed. It deletes
  only the files listed in `.room-vault.json`, plus `index.md` and the
  manifest itself, then removes type folders left empty. Files you added are
  kept, and so is the vault folder if anything of yours remains in it. It
  clears only the default vault (or `$MEMORY_VAULT`): a vault exported
  elsewhere with `--vault PATH` must be deleted by you, or cleared by running
  purge with `MEMORY_VAULT=PATH`. A folder without a manifest was not written
  by room and is left alone.

Safety checks:

- The vault folder is created (or reset) to mode `700` and notes are written
  with mode `600`.
- A vault inside any git work tree (any folder with a `.git` above it,
  including this repository) is refused unless you pass
  `--i-know-this-is-a-repo`. A path overlapping `~/.config/agent-keys` is
  always refused; that directory is never read.
- Deletions (by export or purge) follow no symlinks. A manifest entry that is
  absolute, contains `..`, or reaches a file through a linked folder or a linked
  file is skipped, so nothing outside the vault can be deleted. A vault path
  that is itself a symlink is not cleared by purge.
- Every note, including `index.md`, is passed through `redact_secrets`. The
  result is then checked with `find_secrets` (the same patterns, the configured
  key values and high-entropy tokens). A note that still matches is not
  written; the command names it and exits with status 1.

`room remember --clipboard [--tag TAG] [--yes]` is the matching way in. It
reads the clipboard with `pbpaste` (macOS) and refuses anything over 50,000
characters. It redacts the text, prints its size and a preview of the first
300 characters (after redaction), and asks `Save this to memory? [y/N]`;
`--yes` skips the question. The note is stored with source `clipboard` and
extracted like any other `room remember` note. If Hermes/Ollama is not
running, it is queued for `room memory process`.

The MCP tool `memory_remember` (see the README's "Claude desktop app" section)
is the third way in, for apps that can't run shell commands. It saves only
when the user explicitly asks. The note is redacted, refused if a secret
pattern still matches, and limited to 8,000 characters and 5 saves per minute
per server process. It is written as `sessions/mcp-<timestamp>-<hex>.jsonl`
(agent `mcp-desktop`, round 0, role `user`, content `Tags: <tag>` and the
note) and queued. The server then starts `room memory process` in the
background and does not wait; it never opens `memory.db` itself. Nodes
extracted from it carry the source `mcp-desktop`.

## Reading the database from another tool

Open it read-only. WAL mode lets you read while the room writes:

```python
import sqlite3
con = sqlite3.connect("file:" + str(Path.home() / ".agent-room/memory.db") + "?mode=ro", uri=True)
assert con.execute("SELECT version FROM schema_version").fetchone()[0] >= 2
active = con.execute("SELECT id, type, name, summary FROM nodes WHERE status = 'active'").fetchall()
hits = con.execute(
    "SELECT n.name, n.summary FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.rowid "
    "WHERE nodes_fts MATCH ? AND n.status = 'active' ORDER BY bm25(nodes_fts, 10.0, 2.0, 6.0) LIMIT 8",
    ('"graph" OR "memory"',),
).fetchall()
```

Rules for readers:
- Treat `superseded` nodes as history.
- Check `schema_version` before relying on columns.
- Never write to the database; use `room remember` or `room memory forget` instead.
