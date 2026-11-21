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

There are two layers. This document is mostly about the **long-term** layer
(`memory.db`): curated facts that are kept. The **working** layer
(`~/.agent-room/working/`) holds raw, redacted session transcripts that expire;
see [Working layer](#working-layer) below. The only way from working to
long-term is `room working promote <id>`.

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
| `provenance` | `id`, `node_id` **or** `edge_id`, `session_id`, `agent` (the extractor), `source` (`session` / `manual` / `clipboard` / `mcp-desktop` / `claude-code` / `antigravity`), `ts` |
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
  information is **merged**: new sentences are appended to the summary (a
  sentence sharing at least 80% of its words with one already there is skipped),
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
   Before that, a pre-filter (`room/memory/quality.py`) drops messages that are
   only chatter (greetings, thanks, "reply with just the word ...") or an
   impersonal general question ("what is a hash map?"), and removes code blocks,
   tracebacks, shell prompts and symbol-heavy lines. The exact rules:
   - A leading `> ` is a quote mark and is stripped, so "> I prefer tabs over
     spaces" is kept.
   - A line ending in a space and a backslash, and the line after it, are a
     shell command split over lines and are dropped (`docker run \`,
     `> --rm -it \`). A backslash with no space before it, as in
     `C:\Users\alex\`, does not continue anything.
   - A bare level line (`Error:`, `Warning:`, `Fatal:`, `Critical:`, `Info:`,
     `Debug:`, `Trace:`, `Notice:`) is kept only if the text after the colon
     opens with you speaking (see below): "Warning: I am moving Trail Tracker to
     Rust" is kept; "Warning: region us failed health check", "Error: variable
     i is not defined" and "INFO: Started server" are dropped.
     `RuntimeError: ...` is always dropped.
   - Also dropped: git status and git log output ("On branch main", "Changes
     not staged for commit", "modified: src/app.py", "commit 1a2b3c4",
     "Signed-off-by: ..."), Go panics ("panic: ...", "goroutine 1 [running]:"),
     source locations ("main.go:12 +0x1d", "main.c:12:5:") and bare call frames
     ("main.main()").
   - The indented block under a noise line belongs to it, blank lines
     included, until the next line that is neither indented nor blank. Under a
     stack trace (Python, JS/Java, Go panic) the whole block is dropped; a new
     `$ ` prompt ends the trace and starts a new output block. Under other
     tool output (`$ npm test`) a line is kept only if it opens with you
     speaking: "I", "I'm", "I've", "I'd", "I'll", "im" or "My" followed by a
     word, or "We"/"Our" when the line also names a multi-word Title Case name.
     So "  I switched Trail Tracker to Vitest" and "  We migrated Harbor Log to
     pytest last week." are kept, while "  Tests: 1 failed", "  we expected 3
     but got 4", "  I: Installing ..." and "  I [main] worker started" are
     dropped.
   - An impersonal definition question ("what is / what's / who is / define /
     explain / how does ... work") is dropped,
     whatever it names ("What is Apache Kafka?"). Any other impersonal question
     is dropped unless it names a multi-word Title Case name, which is probably
     your own project ("Is Postgres right for Trail Tracker?",
     "Should Trail Tracker use Redis?").

   If nothing is left, the model is not called and the result is "nothing to
   extract".
2. **Extractor.** `MEMORY_EXTRACTOR` is `hermes` (default), `claude` or `gemini`.
   The prompt asks for strict JSON in this shape:
   `{"nodes":[{type,name,summary,confidence,evidence}],"edges":[{src,relation,dst}]}`,
   where `evidence` is a short quote of your own words. The prompt lists what not
   to record (greetings, the question itself, general knowledge, guessed details,
   logs) and has one placeholder example. With Hermes, Ollama's structured outputs
   constrain decoding to the JSON Schema (`EXTRACTION_SCHEMA`, which requires
   `evidence`), so node types and relations can only be valid values, and the
   call uses temperature 0, so the same text gives the same result.
3. **Validation.** Every output is validated against the schema, whichever
   backend produced it: types, lengths, confidence in [0, 1], known relations,
   no extra keys.
   Then a post-filter drops a node when its evidence quote is not in your own
   words (exact, or at least 80% of its words), when it records chatter or your
   question ("the user asked about ...", "the user is deciding ..."), when its
   evidence appears only inside one of your questions and it is not a project or
   application (an open question is not a decision or task: from "What should I
   name my budgeting app?" the budgeting app is kept, "Name Budgeting App" is
   not; this also drops requests such as "Review PR for Ledgerly"). A clause
   before the last comma of a question counts as a statement only if it opens
   with a factual marker (since, because, as a / as an, as someone who, now
   that, given that, seeing that, considering that) and holds none of the
   words if, suppose, supposing, assume, assuming, imagine, hypothetically,
   say, would, could, might, were, in case, unless, whether, provided. So
   "Since I am joining Corvid Analytics as a data engineer next month, what
   should I read?" and "Now that I have joined Corvid Analytics, what should I
   read?" keep Corvid Analytics, while "If I joined Google next year, what
   should I learn?", "Suppose I am accepted at UCSB, ...", "Assuming I get the
   offer from Corvid Analytics, ...", "What if I switch to Go, would that
   help?", "Say I were a data engineer at Corvid, ...", "Imagine I prefer Vue,
   ...", "Hypothetically, I joined Stripe, ..." and "Would it help if I
   learned Rust, ...?" store nothing from the premise. The question part is
   never a statement. A question that holds one of those hypothetical words
   does not even keep the project or application it names: "If I build Trail
   Tracker, which stack should I use?" stores no Trail Tracker project, while
   "Which database should Harbor Log use?" still keeps Harbor Log. A node is also dropped when its confidence is below
   0.5, or
   when any secret pattern matches its name, summary or evidence, or when a
   credential value (below) appears in its name, summary or evidence (any
   node, "Me" too), or when its name or summary is other secret talk; for "Me"
   only the summary is emptied for those. Secret talk is exactly one of:
   - a credential value: a label, then up to 6 filler words, then a
     connector (`=`, `:`, `->`, `=>`, an em or en dash, `is`, `was`, `are`,
     `is now`, `set to`, `is set to`, `equals`, `is equal to`, `will be`, or
     是 / は; the value may start on the next line), then a value that runs to
     the end of the clause, a quote, a closing bracket, or `&` before another
     URL parameter ("API_KEY=abcd1234", "pwd: orange", "the password is
     swordfish", "my passphrase is blue sky mining", "The password for Harbor
     Log is now42abc", "the password (for prod) is ...", '{"password":
     "x"}', "?api_key=x&v=1"); URL userinfo (`postgres://user:pass@host`,
     masked whole); a 32+ character base64 token with mixed case and digits;
     padded base64 after a label; or `Authorization: ...` / `Bearer <value>`
     / `Basic <value>`. Matching runs on a normalized copy (NFKC, zero-width
     characters removed, Unicode spaces, Cyrillic а е о р с х as Latin); the
     original text is what gets masked. A filler word holds
     no `,` `;` `:` `=` `!` `?` and no `.` except inside a name. There is no
     status-word exception (stage 8): "The password is not-so-secret-42" and
     "the token is valid for one hour" are both masked; losing the second
     fact is the accepted price. The labels are client secret, secret key,
     api key, access key, private key, passphrase, passcode, password,
     passwd, pwd, secret, token, key, pass, pw, pin, otp, 2fa code,
     recovery code, session cookie, cookie, psk, secret code, seed phrase,
     auth header, auth, credentials, mot de passe, contraseña, passwort,
     密码, パスワード, jwt, totp, hmac and license key; in each, a space may also be `_` or `-` (api
     key, api_key, api-key), and a variable prefix is allowed (`DB_PASSWD`,
     `GH_PAT`; `pat` only with a prefix). A label directly followed by manager,
     limit, count, budget, window or policy (or their plurals) names a thing,
     not a credential ("token limit is 4096", "password policy is strict");
   - a credential variable name with one of these suffixes: `_API_KEY`,
     `_ACCESS_KEY`, `_SECRET_KEY`, `_PRIVATE_KEY`, `_CLIENT_SECRET`, `_SECRET`,
     `_TOKEN`, `_PASSWORD`, `_PASSWD`, `_PAT` ("User set ANTHROPIC_API_KEY");
     `PRIMARY_KEY` and `MAX_TOKENS` do not match;
   - a secret the user owns ("their API key", "the user's secret token");
   - a sentence saying the user holds one ("the user stores an API key and a
     password", "they rotated their token").

   Plain technical words are kept: "Trail Tracker uses JWT tokens", "an offline
   password manager", "a daily limit of 200,000 tokens", "renamed PRIMARY_KEY
   to USER_ID". A bare "key" with no connector and value ("rotated the key
   for Lumen Budget") is not matched; "the key for Lumen Budget is abc" is. Before the model, the pre-filter replaces every credential value
   with `[REDACTED]` (the rest of the line is kept), and the post-filter drops
   any node that still contains one of those withheld values, or any word of
   one that the model never saw, even from a model that copies it. The same name
   twice in one output is stored once, and a fact the graph already holds under
   another name (same type, every sentence sharing at least 80% of its words) is
   not added again. Links to a dropped node are dropped. A "Me" node is always a
   person, and a summary that only describes the conversation is left empty.
   Nodes with no `evidence` field at all (from backends without structured
   output) are kept as before; an empty or blank quote counts as ungrounded and
   the node is dropped.
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

### Measuring extraction

`python -m room.memory.extraction_eval --runs 3` runs the real extractor (local
Hermes) on 7 made-up, redacted transcripts in `tests/eval/extraction_cases.json`
(a project, a decision and preference, two applications, chatter only, an
assistant's guess, a fake secret with a traceback, and a fact already in the
graph), each in a temporary home. It reports recall of the expected facts,
precision (nodes that match an expected or allowed fact, are supported by the
user's words and hold no forbidden word), unsupported nodes, forbidden words,
secret leaks and duplicates. Round 1, measured with `hermes3:8b` on the round-1
code (commit 831091d and its follow-up), not on the current version:

| | recall | precision | F1 | unsupported nodes | forbidden | leaks |
|---|---|---|---|---|---|---|
| before (3 runs, mean) | 0.767 (0.600–0.900) | 0.495 | 0.598 | 31 of 61 | 2 | 0 |
| after (3 runs, all equal) | 0.900 | **0.733** | 0.808 | 12 of 45 | 0 | 0 |

What this shows, and what it does not:

- **Precision** is the only clear gain. A separate single run of the old version
  scored precision 0.571, still well below 0.733.
- **Recall** did not clearly change. The old version ranged from 0.600 to 0.900
  across runs (a separate single run scored 0.900, equal to "after"), so the
  difference is within run-to-run noise on this set. The new version also uses
  temperature 0, which removes some of that spread, so the F1 change mostly
  reflects precision.
- **Small set, tuned on itself.** There are only 7 cases, and the round-1
  filters were tuned while looking at these same 7 cases. Round 2 added a
  separate 13-case held-out set (below), written by the same author.
- **Scorer and fix landed together.** The scorer, the cases and the filters came
  in one commit, so the history cannot show that the scorer was fixed before the
  baseline was measured. Both rows were scored with the same scorer.
- The "supported" check counts a node as unsupported when most of its words are
  not in your text, so an honest paraphrase can count against it, and the
  duplicate count also flags a task named after its project. It also keeps a
  word's trailing period, so a word at the end of one of your sentences
  ("... Trail Tracker.") does not count as support. This quirk is left as is so
  that scores stay comparable across rounds.

#### Round 2: held-out set

`python -m room.memory.extraction_eval --runs 3 --cases tests/eval/heldout_cases.json`
scores a second set of 13 made-up, redacted transcripts, with totals per group:

- `ab` (5): one case for each issue fixed in round 2 (a `> ` quote, a
  `Warning:` line in prose, a question naming a project, an indented line after
  `$ npm test`, a restated question with an assistant's guess);
- `control` (3): two with no facts at all and one with a fake secret;
- `independent` (5): unrelated to those issues (a debugging chat, a planning
  chat, personal preferences, a code review, a new job).

**Held-out = written before the fixes, by the same author, so it is a sanity
check, not an independent benchmark.** The cases were written and scored on the
old code before any filter changed; the filters were not tuned on them
afterwards. One held-out case was inspected after the fix, only to explain a
drop (below). Caveat: `heldout_cases.json` and the round-2 code landed together
in commit 38bd874, so the git history alone cannot prove the cases came first;
the "before" scores were taken on the unchanged filter code in the same working
tree before any filter edit. All rows: `hermes3:8b`, temperature 0, 3 runs, all 3 equal.
"After review" is after the fixes from a review of round 2 (indentation block,
whole-word first person, shell continuations, definition questions, secret-talk
nodes); those were tested with new unit tests, not tuned on the held-out cases.

| set (recall / precision / F1) | before | after round 2 | after review | round 3 (stage 6, 3 runs, all 3 equal) |
|---|---|---|---|---|
| original 7 cases | 0.900 / 0.733 / 0.808 | 0.900 / 0.786 / 0.839 | 0.900 / 0.786 / 0.839 | 0.900 / 0.786 / **0.839** |
| held-out, all 13 | 0.647 / 0.650 / 0.649 | 0.882 / 0.652 / 0.750 | 0.882 / 0.682 / 0.769 | 0.882 / 0.652 / **0.750** |
| held-out `ab` (5) | 0.000 / 1.000 / 0.000 (1 node) | 1.000 / 0.800 / 0.889 | 1.000 / 0.800 / 0.889 | 1.000 / 0.800 / **0.889** |
| held-out `control` (3) | 1.000 / 0.333 / 0.500 | 1.000 / 0.333 / 0.500 | 1.000 / 0.500 / 0.667 | 1.000 / 0.333 / **0.500** |
| held-out `independent` (5) | 0.833 / 0.688 / 0.753 | 0.833 / 0.667 / 0.741 | 0.833 / 0.667 / 0.741 | 0.833 / 0.667 / **0.741** |

Stage 8 (stricter credential masking, 1 run each): original 0.900 / 0.800 /
0.847, up from 0.839. In `secret-and-tool-noise`, `API_TOKEN=<secret> in it`
is now masked to the end of its clause, the prompt changed, and the model
stored one more useful node ("Deploy Okapi Notes to Fly.io"); that case was
inspected only to explain the change. Held-out 0.882 / 0.652 / 0.750 and
regression 0.889 / 0.800 / 0.842 are unchanged. Secret leaks 0.

Stage 9 (shared mask rules, normalization, new shapes and labels; `--runs 1`
each): original 0.900 / 0.800 / 0.847, held-out 0.882 / 0.652 / 0.750,
regression 0.889 / 0.800 / 0.842, all unchanged from stage 8; 0 secret
leaks. The 0.847 original-set score is a single-run (`--runs 1`)
measurement, not a 3-run mean.

Round 3 narrowed the secret-talk rule so that plain technical words ("JWT
tokens", "password manager", "PRIMARY_KEY") are kept. As a result the held-out
control group dropped from 0.667 to 0.500: the node "Rotate LUMEN_KEY for
Lumen Budget" is stored again, because `LUMEN_KEY` is not on the credential
suffix list. Its value is not stored (0 secret leaks). This was found by
inspecting that held-out case after the change; it was accepted as a known
gap and nothing was tuned against it.

Secret leaks were 0 in every run. What this shows:

- The round-2 issues are fixed on their own cases (`ab` recall 0 to 1), and the
  original set did not get worse; its precision rose because a restated
  question is no longer stored.
- The `independent` group did not improve and is slightly lower: the only
  difference is that a task "Review PR for Ledgerly", read out of the question
  "Can you review this PR for Ledgerly ...?", is now dropped. The scorer had
  counted it as useful because it names Ledgerly; the new rule treats it as a
  request, not a fact. Recall there is unchanged.
- The review fixes changed only the controls: one forbidden node is gone (2
  stored nodes, 1 of them still junk). No group got worse.
- These sets are small (7 and 13 cases); one node moves a group's precision by
  several points.

#### Round 3: regression set (found by the reviewer, not held-out)

`python -m room.memory.extraction_eval --runs 3 --cases tests/eval/regression_cases.json`
scores 19 cases, each labelled "regression: found by reviewer, not held-out".
**This set is not held-out: its inputs come from the round-2 review, and the
filters were fixed while looking at them, so it only checks that those fixes
stay fixed.** 17 cases are the reviewer's inputs; the 2 `known-gap` cases were
found in the stage 5c probes and are expected to fail. No code was changed
after these cases were written.

| group (cases) | recall / precision / F1 | notes |
|---|---|---|
| `false-drop` (4): JWT tokens, PassVault, Muse token limit, PRIMARY_KEY rename | 1.000 / 1.000 / 1.000 | all kept |
| `premise` (2): Corvid Analytics, Rust over Go | 1.000 / 1.000 / 1.000 | premise kept, question not stored |
| `noise` (6): git log, git status, `us`/`i` log lines, apt `I:`, Go panic, INFO/DEBUG | no facts expected, 0 nodes | nothing reached the model |
| `speaker` (2): "We migrated Harbor Log", Windows path | 1.000 / 0.667 / 0.800 | both facts found; the Windows case also stores a "Me" node the scorer counts as unsupported |
| `hypothetical` (3): premise and project | no facts expected, 0 nodes | nothing stored |
| `known-gap` (2) | 0.000 / 0.000 / 0.000 | "Which database could Harbor Log use?" loses the project; "If I build Harbor Log I will write it in Go." stores a Harbor Log project |
| all 19 | 0.889 / 0.800 / 0.842 | 3 runs, all 3 equal; 0 forbidden outside `known-gap` |

Secret leaks were 0, but this set contains no fake secret, so that number says
nothing here; secrets are covered by the leak tests and the held-out control.

#### Known gaps

Every filter here is pattern-based and best-effort, not a guarantee. Known
gaps in the question, noise and secret rules:

- A question without "?" ("what should I name my app") is not treated as a
  question, so a task read out of it can still be stored.
- Other question shapes slip through: "Which is better, Vue or React Native?"
  names a multi-word Title Case name and reaches the model; an impersonal
  question about your project that starts like a definition ("What is Trail
  Tracker's next milestone?") is dropped.
- "We ..." at the start of an indented line after a shell prompt is dropped
  unless the line names a multi-word Title Case name ("We moved it to Go" is
  dropped).
- A prose line ending in a space and a backslash is dropped with the next line.
- Non-indented tool output after a `$ ` prompt is dropped only when it matches
  a known pattern (git, Go panics, log levels, source locations); other output
  lines reach the model.
- The secret-talk rule is pattern-based. It still drops some honest facts
  ("the user's JWT tokens expire hourly" reads as owning a token;
  "`CSRF_TOKEN` is set per request" names a credential variable).
- Masking a value to the end of its clause can over-mask: in "my password is
  hunter2 for Trail Tracker" the words "for Trail Tracker" are masked too. The
  model never sees them, and the post-filter withholds them, so a Trail Tracker
  node from that message is lost (measured by
  `test_over_masking_loss_is_counted_not_silent` and a strict xfail). The drop
  is counted in the extraction result's `dropped` total, not itemised. If the
  name appears anywhere else in the message, the node is kept.
- **Secret masking (stages 8 and 9), what it covers.** A label (see the
  list above), up to 6 filler words, a connector (`=`, `:`, `->`, `=>`, an
  em or en dash, is, was, are, is now, set to, equals, will be; the value may
  be on the next line) and a value; JSON and dict values; URL userinfo
  (`postgres://user:pass@host`) and URL query secrets (`?api_key=`); base64
  tokens of 32+ characters with mixed case and digits; padded base64 after a
  label; `Authorization`, `Bearer` and `Basic` tokens. Matching runs on a
  normalized copy (NFKC, zero-width characters removed, Unicode spaces,
  Cyrillic а е о р с х), and the original text is masked. The withheld
  values, the mask and the node filter share one rule set, and a queued
  error message is scrubbed of withheld values before it is written to
  `memory_queue`.
- **What the fuzz tests scan, exactly.** The stage-8 fuzz test
  (`test_extraction_stage8.py`) takes its labels from the label list, so it
  now runs 64,680 combinations (33 labels x 0-6 filler words x 7 connectors x
  5 value shapes x 8 message shapes). It checks the extraction prompt, the
  stored nodes and every file under each combination's own temporary home
  and database (`memory.db`, -wal, -shm). Its model always returns valid
  output, so it never writes `memory_queue`. The stage-9 fuzz test
  (`test_extraction_stage9.py`, 17,824 combinations: 13 connectors, 5 Unicode
  variants, JSON/dict/URL forms and 4 URL schemes) gives every third
  combination invalid output, so 4,750 combinations really write
  `memory_queue`, and every file under the temporary home is scanned. Both
  report 0 leaks. Neither creates a session log: extraction never writes
  one, and session logs belong to the working layer (gap 3 below). On the
  pre-stage-9 code the stage-9 matrix leaked in 5,261 of 7,336 combinations
  (13 labels there), including every queued one.
- **Still not handled after stage 9 (can store a secret).** All checked:
  - a value split across two lines ("my password is crim" / "son42": the
    second line is not masked);
  - a value before its label ("crimson42 is my password");
  - more than 6 filler words ("the password for a b c d e f g is x");
  - a comma, colon or sentence end inside the filler ("the password, for
    prod, is x");
  - a label directly followed by manager, limit, count, budget, window or
    policy ("the password policy is hunter2");
  - labels in other languages that are not listed ("la clave es pino42", "das
    Kennwort ist tanne42"), and connectors other than the ones above;
  - spaced-out letters ("p a s s w o r d: birch42");
  - lookalike letters outside the small Cyrillic map (Greek ο in "pοssword").
- **Recall cost (security over recall).** Any sentence with a label, a
  connector and a value is masked and its node dropped. The longer label list
  makes this more common: "the token is valid for one hour", "the key point
  of Trail Tracker is speed", "the pass is valid until June", "auth is
  handled by Auth0", "the cookie banner is ugly" all lose their value. `=` and
  the next line after `:` are masked to the end of the clause.
- A credential variable whose name is not on the suffix list, such as
  `LUMEN_KEY`, is stored as a name ("Rotate LUMEN_KEY for Lumen Budget"); its
  value is never stored. This is the price of keeping `PRIMARY_KEY`, and it is
  why the held-out control group dropped in round 3 (see the table).
- **Gap 3 (open, tracked separately for stage 10; pre-existing, not changed
  here):** the redaction layer (`room/memory/redact.py`, used by capture and the working
  layer) only catches known token shapes and `label=value` pairs with 6+
  characters, so a prefix-less password ("the password is swordfish") in a
  working-layer transcript is not redacted there, and `working_search` (also
  through the room MCP server) can return it. The masking above applies only
  on the way into the long-term graph.
- A fact before a question is kept only when a comma separates it from the
  question and it opens with a factual marker. These lose a real fact
  (checked): "I prefer Rust so which parser should I pick?" (no comma), "I
  joined Corvid Analytics last week, what should I read?" (no marker),
  "Since we were acquired by Stripe, what changes?" ("were" counts as
  hypothetical), and "Since I moved to Go if that matters, which linter?"
  ("if" anywhere in the clause). When unsure, nothing is kept.
- The hypothetical check is a word list, so it also catches plain questions
  that use "could" or "would": "Which database could Harbor Log use?" and
  "Would Harbor Log work on Fly.io?" no longer keep the Harbor Log project
  (checked; a missing fact, not a false one).
- A hypothetical written without "?" is not seen as a question: "If I build
  Harbor Log" can still store a Harbor Log project (checked).
- An impersonal question that names your project with a single word ("Is
  Postgres right for Ledgerly?") is dropped: without knowing your projects,
  "Ledgerly" looks like "Python". Only multi-word Title Case names pass.
- "Describe X?" and "Tell me about X?" are not recognised as questions at all,
  so they reach the model, which is told not to record general knowledge.

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

## Working layer

Raw and temporary, unlike the curated graph above.

- **Where:** one JSON file per captured session in `~/.agent-room/working/`
  (`$AGENT_ROOM_HOME/working/`). The folder is 0700 and files are 0600. Like the
  vault, it refuses to live in a git work tree, inside this repository or near
  the key folder.
- **What:** `id` (`claude-code-<UTC stamp>-<hex>` or `antigravity-...`), `source`, `project` (the
  project folder name), `captured_at`, `updated_at`, `expires_at`,
  `promoted_at`, `dropped_turns`, and `turns` (`role` `user` or `assistant`,
  `ts`, `text`). The Claude Code session id or Antigravity conversation id is
  stored only as a hash, used to find the note again when the session goes on.
- **Written by:** `room capture claude` (the Claude Code `SessionEnd` hook,
  opt-in) and `room capture antigravity` (the Antigravity CLI `Stop` hook,
  opt-in with the same switch; it runs after each turn and keeps only your typed
  request and the final reply of each turn; the room's own `agy` calls, any
  `agy -p` run, and the Antigravity desktop app and IDE are not captured). Text is redacted before the write; if a secret pattern still
  matches, nothing is written. That redaction (`room/memory/redact.py`) is a
  best-effort pattern filter: it misses a prefix-less password ("the
  password is swordfish"), which can then sit in a working note and be
  returned by `working_search` (gap 3 in [Known gaps](#known-gaps), open,
  tracked for stage 10). Sessions with fewer than 2 typed messages or
  under 400 characters are skipped. Each message is cut at 2,000 characters and
  a note keeps at most 8,000 characters, dropping the oldest turns first. A lock
  keeps two sessions that end at the same time from clashing; a resumed session
  adds only turns it has not stored before.
- **Expiry:** 14 days after the last update. Expired notes are never returned
  by `room working list/show/search` or the MCP tools, and
  `room working purge` deletes them (`--all` deletes every note).
- **Promotion:** `room working promote <id>` writes the note as a session log
  (`sessions/<id>.jsonl`; your turns as role `user`, the assistant's as role
  `agent`) and runs extraction. By default only your own words are extracted,
  and the nodes carry source `claude-code` or `antigravity`. The note is marked promoted only
  when extraction has stored it; if extraction was only queued (for example,
  Ollama was not running), it is marked `promote_queued_at` and
  `room working promote` can be run again, reusing the same session log. A
  promoted note cannot be promoted again.
- `room memory purge` does not touch the working layer; use
  `room working purge --all`.

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
