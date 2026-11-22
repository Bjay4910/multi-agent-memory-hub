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
  `room.memory.redact.redact_secrets`. Its patterns target known key formats
  (`AIza…`, `sk-…`, `gh*_…`, `github_pat_…`, `LLM|…`, `xox*-…`, `AKIA…`, JWTs,
  `Bearer …`, PEM private keys), `api_key=…`/`password: …` style values, long
  high-entropy tokens, and the actual values of `GEMINI_API_KEY` and
  `MUSE_API_KEY`. Extracted names and summaries are redacted again before
  storage, and manual notes are redacted before they are saved. Those are
  redact.py's patterns, not verified forms; for the extraction filter,
  verified forms only are the exact examples in [Handled forms
  (tested)](#handled-forms-tested).
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
     connector, then a value that runs to the end of the clause, a quote, a
     closing bracket, or `&` before another URL parameter. This sentence
     describes the code; what is verified is only the exact examples in
     [Handled forms (tested)](#handled-forms-tested) (verified forms only;
     a form that is not in that table is not covered). Connectors and
     line breaks, exactly: after a symbol connector (`=`, `:`, `->`, `=>`,
     an em or en dash) the value may start up to 4 lines down (up to 4
     newlines, each followed by spaces or tabs); after a word connector
     (`is`, `was`, `are`, `is now`, `set to`, `is set to`, `equals`, `is
     equal to`, `will be`) at most 1 newline is allowed, so "The password
     is\n\n  x" is not matched; after 是 or は (Chinese and Japanese labels)
     no newline is allowed. Matching runs on a normalized copy, built one
     character at a time: each character goes through NFKC on its own
     (fullwidth `：` becomes `:`), the zero-width characters U+200B, U+200C,
     U+200D, U+2060 and U+FEFF and the soft hyphen U+00AD are removed, every
     Unicode space becomes a space, Cyrillic а е о р с х (and capitals)
     become Latin, and curly quotes ’ ‘ “ ” become ASCII `'` and `"`. NFKC
     runs per character, so a combining mark is composed with the character
     before it only when NFC of that pair gives one character (n + U+0303
     becomes ñ, so "contrasen\u0303a" matches contraseña; since stage 12);
     marks are composed pairwise in written order, not reordered first. The
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
   any node whose name, summary or evidence contains a withheld value of 3+
   characters (case-insensitive substring, checked on the text as written
   and on its normalized copy), even from a model that copies it. The
   withheld values are exactly: each masked value as written and as
   normalized (also unescaped if it holds a backslash, and the password part
   of URL userinfo); every substring `redact_secrets` masks in the
   extraction prompt; each word of those (split at whitespace, with
   `. , ; : ! ? ( ) ' "` and also `< > = `` ` `` stripped from its ends) of
   3+ characters that does not appear in the masked text the model sees;
   each piece of 5+ characters (3+ if it holds both a letter and a digit,
   since stage 13) after splitting at whitespace,
   `" ' \ , ; : ( ) < > [ ] { } = / @ & . #`, `` ` ``, curly quotes ’ ‘ “ ”
   and every boundary between a CJK run (kana, CJK ideographs, Hangul,
   half-width katakana) and a Latin letter or digit run (since stage 12:
   "plum994" in "我的密码是plum994别告诉别人"), again only if the model
   never sees it; and, for a value that starts with a bracket, each piece
   of 3+ characters between brackets. A value glued to other text by any
   other character is withheld only as a whole. The same name
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

Stage 10 (quoted and wrapped values, more shapes, redact_secrets substrings
withheld; `--runs 1` each): original 0.900 / 0.800 / 0.847, held-out 0.882 /
0.652 / 0.750, regression 0.889 / 0.800 / 0.842, all unchanged; 0 secret
leaks.

Stage 11 (third review's leaks by family, more shapes; `--runs 1` each):
original 0.900 / 0.800 / 0.847, held-out 0.882 / 0.652 / 0.750, regression
0.889 / 0.800 / 0.842, all unchanged; 0 secret leaks.

Stage 12 (curly quotes, soft hyphen, composed ñ, CJK pieces, quoted value
after a word; `--runs 1` each): original 0.900 / 0.800 / 0.847, held-out
0.882 / 0.652 / 0.750, regression 0.889 / 0.800 / 0.842, all unchanged; 0
secret leaks.

Stage 13 (base64 `=` padding not a connector, 3+ character letter+digit
pieces; `--runs 1` each): original 0.900 / 0.800 / 0.847, held-out 0.882 /
0.652 / 0.750, regression 0.889 / 0.800 / 0.842, all unchanged; 0 secret
leaks.

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
nothing here; secret masking is checked by the leak tests, verified forms
only ([Handled forms (tested)](#handled-forms-tested)).

#### Handled forms (tested)

Verified means: these exact examples, with only the secret value changed to
another value of the same kind, at least the row's minimum length and within
its case limit (both shown in the table). Rows with no minimum were checked at 4, 10 and 24 characters.
Other variants of the same shape (different quoting, spacing, extra words,
Unicode forms, line breaks) may or may not be masked and are not verified.

Each row lists the exact example(s) that a test masks before the model; the
form column only names what those examples show. Test files are
`tests/memory/test_extraction_<file>.py`. A form that is not in this table is
not covered. (The fuzz tests in the gap list combine these forms with more
labels and message shapes; a combination is verified only if a fuzz test
generates it.)

| form (as in the examples) | verified example(s) | test | minimum length |
|---|---|---|---|
| label, `is`, value | `the password is swordfish` | round3 `test_plain_word_password_in_every_shape_never_leaks` | none |
| label, ` = ` or `: `, value | `client_secret = xyz`; `pwd: orange` | round3 `test_prefixless_secret_in_every_shape_never_leaks` | none |
| label, filler words, connector, value | `The password for Harbor Log is now42abc.`; `the api key on the staging server was qz81mbt`; `The token of my admin account is now pl4netx`; `the passcode for the front door set to 9a8b7c`; `Password for prod: driftwood`; `the secret for Harbor Log equals copperleaf`; `The password (for prod) is gl4cier`; `the token for harbor.log is m00nbeam`; `the key for "Harbor Log" is r1verstone` | stage8 `test_filler_words_between_label_and_connector_are_masked` | none |
| a value that starts with a status word | `The password is not-so-secret-42.`; `The api key is in-vault-7781.`; `the token is set4you99`; `my passphrase is valid forest lantern` | stage8 `test_status_word_values_are_masked` | none |
| value on the line after `:` or `=` | `Trail Tracker password:\nmagnolia77`; `API_KEY=\nfirsthalf99` | stage9 `test_value_on_the_next_line_is_masked_and_withheld` | none |
| value after one blank line after `:` | `password:\n\n  amber22` | stage10 `test_yaml_value_after_blank_lines_is_masked_by_both_layers_and_withheld` | none |
| NBSP, fullwidth `：` `＝`, zero-width space, Cyrillic а, em dash, `->`, `will be` | `Trail Tracker password is garnet44`; `password： violet66`; `API_KEY＝ topaz99`; `PASS​WORD: indigo44`; `pаssword is orchid55`; `password — quartz55`; `My password for Trail Tracker -> marigold55.`; `My password for Trail Tracker will be juniper66.` | stage9 `test_normalized_shapes_are_masked_and_withheld` | none |
| more labels | `pass: almond42`; `pw = almond43`; `pwd is almond44`; `the PIN is 4821`; `otp: 739201`; `my 2fa code is 551903`; `recovery code: wx7k-q2m9`; `session cookie = sid88almond`; `cookie: almond45sid`; `psk is almond46`; `the secret-code is almond47`; `seed phrase: tiger ocean almond lamp`; `auth header: Bearer almond48almond48`; `credentials: admin / almond49`; `the credentials are almond50`; `mot de passe : lavande42`; `contraseña: girasol42`; `Passwort: kornblume42`; `密码: mudan42`; `パスワード: sakura42` | stage9 `test_more_labels_are_masked_and_withheld` | none |
| labels jwt, totp, hmac, license key | `jwt: Ab3dEf7gHi9jKl`; `totp = 482913`; `hmac: f00dbabe99`; `license key: ABCD-1234-EFGH` | stage10 `test_common_shape_masks_only_the_secret` | none |
| JSON / Python dict value | `{"password": "dahlia77"}`; `{'password': 'anise55'}`; `{"auth": {"pwd": "tulip66"}}`; `{"api_key": "peony8899", "token": "iris4455"}` | stage9 `test_json_and_dict_values_are_masked_and_the_rest_kept` | none |
| double-quoted value with `,` `;` `)` or `\"` inside | `{"password": "alpha,beta99", "env": "prod"}`; `DB_PASSWORD="p@ss;word88"`; `{"password": "ab)cd99", "env": "prod"}`; `{"password": "pre\"post77", "env": "prod"}` | stage10 `test_quoted_value_is_masked_through_the_closing_quote` | none |
| value in `( )` or `< >`, or after `==` | `password is (orchid77) in prod.`; `password: <topaz66>`; `api_key: ==garnet55` | stage10 `test_wrapped_value_is_masked_with_its_wrapper` | none |
| two-level nested `( )` `[ ]` `< >` `{ }` | `password is (outer(inner746)) in prod.`; `token: [alpha[beta857]]`; `pwd: <x<y179>> for prod`; `api_key = {gamma{delta968}} here` | stage11 `test_a_family_masks_the_value` | none |
| Markdown or quoted label, connector | `**Password**: sycamore38`; `` `api_key`: chestnut29 ``; `"password" is walnut64.` | stage10 `test_common_shape_masks_only_the_secret` | none |
| Markdown or quoted label, filler words, connector | `**Password** for Trail Tracker is camellia981.`; `` `api_key` for Trail Tracker = azalea192 ``; `"password" for Trail Tracker is begonia203.`; `'secret' for prod is dahlia284`; `*token* on staging = elder395` | stage11 `test_a_family_masks_the_value` | none |
| URL userinfo | `postgres://admin:sycamore99@db.example.com:5432/prod`; `https://alice:buttercup77@git.internal.local/repo.git`; `redis://:verbena55@cache.internal:6379/0`; `mongodb+srv://ops:tarragon88@cluster0.example.net/db` | stage9 `test_url_userinfo_is_masked_whole` | none |
| URL query secret (params api_key, auth, token, password, secret, key) | `https://api.example.com/v2/items?api_key=lilac7x9&page=2` | stage9 `test_url_query_secrets_are_masked` | none |
| URL query, `&` before a bracketed parameter | `https://api.example.com/search?token=violet94&filter[a]=1` | stage10 `test_query_value_ends_at_ampersand_before_a_bracketed_parameter` | none |
| base64 of 32+ characters, mixed case and digits | `Trail Tracker uses Ab3dEf7gHi9jKl1m/No3pQr5sTu7vWx9yZa1bCd3eFg= for sync.` | stage9 `test_base64_candidates_are_masked` | 32+ base64 characters, with an upper-case letter, a lower-case letter and a digit |
| padded base64 after a label | `my token QWxhZGRpbjpvcGVu== for Trail Tracker`; `auth header: Basic dXNlcjpwYXNzd29yZDEyMw==` | stage9 `test_base64_candidates_are_masked` | `==` padding (both examples): 4+ base64 characters before it, any case; the `Basic` example: as in row 24. Single `=`: see the next row |
| `=` / `==` padded base64 under 32 characters after a label, with or without words after it | `my token YWRtaW46cGFzc3dvcmQ= for Trail Tracker`; `my token YWJjZGU= for Trail Tracker`; `my api key Zm9vYmFyOTk= for Trail Tracker`; `my secret c2VjcmV0S2V5MQ== for Trail Tracker`; `my token YWRtaW46cGFzc3dvcmQ=`; `my token YWJjZGU=`; `my api key Zm9vYmFyOTk=`; `my secret c2VjcmV0S2V5MQ==` | stage13 `test_a_case_is_masked_and_withheld` | `==`: 4+ base64 characters before it, any case. Single `=` with words or a line after it: 4+ characters before it, mixed case only (an upper-case letter after the first character and a lower-case letter); all lower-case, all upper-case or capitalised-only values are not masked there (stage 15 known gap). Single `=` at the end of the message: 4+, any case |
| `Authorization: Bearer` | `Authorization: Bearer eyJhbGci...` (the round3 fixture) | round3 `test_prefixless_secret_in_every_shape_never_leaks` | none |
| JSON `Authorization` with `Token` | `{"Authorization": "Token dogwood196"}` | stage11 `test_b_shape_masks_the_value` | none |
| `Basic` token of 12+ characters without digits | `curl sends Basic YWFhOmFhYWFhYWFh to the API`; `header: Basic dXNlclBhc3NXb3Jk`; `send basic QWxhZGRpbkJvYmJ5 now` | stage11 `test_a_family_masks_the_value` | 8+ base64 characters (not counting `=`) if one is a digit or `=` padding follows; otherwise 12+, with an upper-case letter after the first character and a lower-case letter |
| SendGrid `SG.` token, whole | `Trail Tracker mails through SendGrid with SG.<22 chars>.<43 chars> in prod.` (`FAKE_SG`) | stage10 `test_sendgrid_token_is_masked_whole` | both parts after `SG.` 16+ characters each |
| `glpat-`, `whsec_`, Azure SAS `sig=` | `Trail Tracker CI uses glpat-AbCdEfGhIjKlMnOpQrSt12 for pushes`; `Webhook signing uses whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw in prod`; `...?sv=2021-08-06&sig=Ab3dEf%2BgHi9jKl%3D&se=2026`; `Upload to https://acct.blob.core.windows.net/c?sig=Q1w2E3r4T5y6%3D` | stage10 `test_common_shape_masks_only_the_secret` | glpat body 20+; whsec body 16+; `sig=` value 8+ |
| `pypi-`, `hvs.`, Discord-style three segments, `whsec_test_` | `publish with pypi-AgEIcHlwaS5vcmcCJGQ4Yjc5ZTAw tonight`; `vault gave hvs.CAESIJlU9XnE2Kq7Wn5bF3dP0aQ1 to CI`; `bot uses MTA0NjE4NzQ5MjM0NTY3ODkw.GaBcDe.abcdefghijklmnopqrstuvwxyz0123 here`; `Stripe sends whsec_test_MfKQ9r8GKYqrTwjUPD8I in dev` | stage11 `test_b_shape_masks_the_value` | pypi body 16+; hvs body 20+; Discord: first segment 24 to 28, second 6 to 7, third 27 to 64; whsec body (counting `test_`) 16+ |
| a token used as a filler word of a label match | `Webhook secret whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw is set` | stage10 `test_a_token_inside_the_filler_of_a_label_match_is_still_masked` | whsec body 16+ |
| curl `-u` / `--user` / `-b` / `--cookie`, spaced | `curl -u deploy:hazel71 https://api.example.com/v1`; `curl --user ops:maple82 https://api.example.com/v1`; `curl -b 'sid=cedar93' https://api.example.com/v1`; `curl --cookie "session=birch44" https://api.example.com/v1` | stage10 `test_common_shape_masks_only_the_secret` | none |
| curl: empty user, double-quoted with a space, attached `-u`, single-quoted `--user` and `-b` | `curl -u :marigold303 https://api.example.com`; `curl -u "bot:blue marigold414" https://api.example.com`; `curl -ubot:petunia525 https://api.example.com`; `curl --user 'ci:violet 828' https://api.example.com`; `curl -b 'sid=blue cedar606' https://api.example.com`; `curl --cookie "session=lilac717" https://api.example.com` | stage11 `test_a_family_masks_the_value` | none |
| `redis-cli -a` spaced, `mysql -p` attached, `--password=`, `sshpass -p` spaced and single-quoted | `redis-cli -a quince55 -h cache.internal`; `mysql -u admin -prowan66 shop`; `mysql -u admin --password=aspen77 shop`; `sshpass -p 'larch88' ssh ops@db.internal` | stage10 `test_common_shape_masks_only_the_secret` | none |
| `mysql -p` attached and quoted; `--password="..."`; `sshpass -p'...'`; `docker login -p` spaced; `redis-cli -a` spaced and double-quoted; `docker login --password=` | `mysql -u admin -p'foxglove969' shop`; `mysql -u admin -p"bluebell170" shop`; `tool --password="red poppy281" --verbose`; `sshpass -p'primrose392' ssh ops@db.internal`; `sshpass -p 'tansy503' ssh ops@db.internal`; `docker login -u ops -p parsley999 registry.example.com`; `redis-cli -a "ivy 614" ping`; `docker login -u ops --password=sage725 registry.example.com` | stage11 `test_a_family_masks_the_value` | none |
| YAML `\|` and `>-` block scalar (first line) | `password: \|\n  spruce99`; `api_key: >-\n  poplar11` | stage10 `test_common_shape_masks_only_the_secret` | none |
| YAML block scalar with an indentation indicator | `password: \|2\n  yew229`; `api_key: >2-\n  zelkova230` | stage11 `test_b_shape_masks_the_value` | none |
| `with` / `using` + label + unquoted value with a digit | `Connect with password juniper89 to the staging box.`; `Log in using token oak-77-ash today.` | stage10 `test_common_shape_masks_only_the_secret` | none |
| `with` / `using` + label + quoted value | `Connect with password "lotus625" to staging.`; `Log in using token 'clover736' today.`; `Sign in with key "quiet harbor words" now.`; ``Deploy using password `fern847` today.`` | stage11 `test_a_family_masks_the_value` | none |
| `login: user/PASS`, `login is user/PASS` | `login: admin/teak42`; `the login is ops/sequoia7 for staging` | stage10 `test_common_shape_masks_only_the_secret` | none |
| `login: user / PASS` | `login: admin / teak141x`; `the login is ops / sequoia152 for staging` | stage11 `test_b_shape_masks_the_value` | none |
| changed / reset / updated + label + `to` | `I changed the password to hawthorn41.`; `We reset the api key to rowan52`; `They updated our token to sorrel63 today.` | stage11 `test_b_shape_masks_the_value` | none |
| `<label>'s <value>`, `<label> as <value>` (value with a digit) | `The password's hazel74.`; `Trail Tracker token's linden85x now`; `Use the token as willow96.`; `Set the password as aspen-107 for staging` | stage11 `test_b_shape_masks_the_value` | none |
| glued upper-case env name | `PGPASSWORD=spruce118 psql -h db.internal`; `export MYPASSWORD='cedar129'`; `GITHUB_TOKEN=ghxfake130` | stage11 `test_b_shape_masks_the_value` | none |
| one-line JSON list under a plural key | `{"secrets": ["alder163", "birch174"]}`; `{"api_keys": ["cedar185"]}` | stage11 `test_b_shape_masks_the_value` | none |
| one-line XML element without attributes | `<password>elm207</password>`; `<api_key>fir218</api_key>` | stage11 `test_b_shape_masks_the_value` | none |
| value glued to CJK text, in curly quotes, or before `#` (withheld piece: 5+ characters, or 3+ if it mixes letters and digits, since stage 13) | `我的密码是plum994别告诉别人`; `私のパスワードはsakura105です`; `My password is “mango993” for staging`; `https://svc.example.com?access_key=jay213#frag` | stage12 `test_c_case_is_masked_and_withheld` | none |
| 3- and 4-character letter+digit pieces split from CJK text or `#` | `我的密码是k9x4别告诉别人`; `私のパスワードはm7q2です`; `https://svc.example.com?access_key=j8k3#frag` | stage13 `test_a_case_is_masked_and_withheld` | none |
| curly apostrophe, soft hyphen in the label, decomposed ñ in the label | `The password’s acorn329 for staging.`; `pass\u00adword: papaya104`; `La contrasen\u0303a: canela883.`; `pwd: rose\u0301x77` | stage12 `test_c_case_is_masked_and_withheld` | none |
| one word then a quoted value; `to be` then a quoted value; an apostrophe between letters in the value | `My password is really "cranberry769" for CI.`; `I set my password to be "cranberry769" for CI.`; `My password is o'brien882 for prod.` | stage12 `test_c_case_is_masked_and_withheld` | none |
| `~~~` code fence cut like ``` | `My config:\n~~~\npassword = fir23\n~~~\nTrail Tracker deploys nightly.` | stage10 `test_tilde_code_fence_is_stripped_like_backticks` | none |
| what `redact_secrets` masks in the prompt is withheld | `Trail Tracker uses token=violet94&filter[a]=1 for search`; `password:\n\n  amber22`; `Trail Tracker key Xq7Lp2Zx9Wv4Nb6Mc1Rt8Ys3Ud5Ke0Jf2Gh4 rotates weekly`; `secret: "hunter22x"` | stage10 `test_every_value_redact_secrets_masks_in_the_prompt_is_withheld` | the `Xq7Lp2…` key example only: 32+ base64 characters with an upper-case letter, a lower-case letter and a digit; the other examples: none |
| a queued error message is scrubbed | the stage-9 fuzz combinations with invalid model output | stage9 `test_fuzz9_matrix_never_leaks_and_really_writes_the_queue` | none; no literal input, so not part of the substitution check |

Substitution check (stage 14, run by the author on the code of 1df62ec, not
an independent review): every literal example in the table above (50 rows,
175 examples; row 50 has none) was run with its secret value replaced by
3 fake values of the same kind: letters and digits of 4, 10 and 24
characters, or `=`-padded base64 of 4, 12 and 24 characters on the base64
rows; a token prefix was kept and the 4, 10 or 24 characters replaced its
body, and for SendGrid and Discord only the last part was replaced. That
is 525 runs, each through the real extraction path: a hostile scripted
model with valid output (prompt, stored nodes, `memory.db`), and invalid
output so the session is queued (prompt, `memory_queue`); all 525 wrote
`memory_queue`. 37 runs leaked (prompt 36, stored node 33, `memory.db` 33,
`memory_queue` 37), all on rows 19, 20, 21, 24, 25, 26, 27, 28 and 49, and
in all 37 the value was shorter than that row's minimum length. No run
with a value at or above the row's minimum leaked. Values with special
characters, spaces or digits only were replaced by letters and digits, so
those characters were not re-tested.

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
- **Secret masking, what it covers.** Verified forms only: the exact
  examples in [Handled forms (tested)](#handled-forms-tested), with only the
  secret value changed to another value of the same kind, at least the
  row's minimum length and within its case limit. A form that is not in that table is not covered,
  even if it looks similar.
- **What "one rule set" means, exactly.** The extraction filter's own
  patterns (`quality.credential_spans`) drive the prompt mask, the withheld
  values and the node filter. The extraction prompt then also goes through
  `redact_secrets` (redact.py, unchanged). Since stage 10, every substring
  `redact_secrets` masks in that prompt, meaning in `clean_message(text)`
  and in the masked text before `clean_message` drops lines, is added to the
  withheld values. They are found by comparing the text before and after
  `redact_secrets`. Not unioned: what `redact_secrets` would mask in the
  raw text, which is not what the model sees (for example
  "hibiscus44&v=1" in "?api_key=hibiscus44&v=1", where the prompt already
  shows "?api_key=[REDACTED]&v=1"). The node filter itself checks only the
  extraction filter's patterns plus `find_secrets`, not every
  `redact_secrets` rule. A queued error message is scrubbed of withheld
  values before it is written to `memory_queue`.
- **What the fuzz tests scan, exactly.** Counts were measured with
  `len(fuzz_cases())`, `len(fuzz9_cases())`, `len(fuzz10_cases())` and
  `len(fuzz11_cases())` and
  match what each test prints under `pytest -s`. The stage-8 and stage-9
  tests take their labels from the label list, so their size grows with
  it. (The stage-9 docs said 64,680 for the stage-8 test; that was computed
  by hand with a wrong label count. The measured count then was 62,720.)
  - Stage 8 (`test_extraction_stage8.py`): 70,560 combinations (36 labels x
    0-6 filler words x 7 connectors x 5 value shapes x 8 message shapes). It
    checks the extraction prompt, the stored nodes and every file under each
    combination's own temporary home and database (`memory.db`, -wal,
    -shm). Its model always returns valid output, so it never writes
    `memory_queue`.
  - Stage 9 (`test_extraction_stage9.py`): 20,032 combinations (36 labels x
    13 connectors x 5 Unicode variants x 8 message shapes = 18,720, the 5
    value shapes taken in turn rather than as a factor; plus 36 labels x 4
    JSON / dict / nested JSON / URL query forms x 8 shapes = 1,152; plus 4
    URL schemes x 5 value shapes x 8 shapes = 160). Every third combination gets invalid output, so 5,334 really
    write `memory_queue`, and every file under the home is scanned.
  - Stage 10 (`test_extraction_stage10.py`): 5,769 combinations (36 labels x
    16 quoted, wrapped, YAML, Markdown, query and with/using families x 9
    message shapes including a `~~~` fence = 5,184; plus 13 curl, CLI, login
    and token families x 5 value shapes x 9 shapes = 585). 1,923 really write `memory_queue`. For token shapes it scans
    for the whole token.
  - Stage 11 (`test_extraction_stage11.py`): 5,391 combinations (36 labels x
    14 families: nested ( ) [ ] < > { }, wrapped labels before filler,
    with/using + quoted value, changed ... to, 's / as, XML, YAML |2, x 9
    message shapes = 4,536; plus 19 curl, CLI, env-name, login, JSON list /
    Authorization, Basic and token families x 5 value shapes x 9 shapes =
    855). 1,797 really write `memory_queue`.
  - Stages 12 and 13 add no fuzz test, only exact tests. Stage 12: each of
    its 11 cases (`len(C_CASES)`) checks the exact `mask_credentials`
    output, the withheld set and the prompt, and runs a hostile model once
    with valid and once with queued output, scanning the stored nodes,
    `memory.db` and `memory_queue`; its 6 false positives
    (`len(C_FALSE_POSITIVES)`) only check `mask_credentials(text) == text`
    and are not run through extraction. Stage 13: the same for its 11 cases
    (`len(A_CASES)`) and 4 false positives (`len(A_FALSE_POSITIVES)`), plus
    one test that runs `clean_extraction` and keeps a node holding the
    ordinary short word "frag". The four fuzz counts above were measured
    again after stage 13 and did not change.
  - All four report 0 leaks. None creates a session log: extraction never
    writes one, and session logs belong to the working layer (gap 3 below).
    Before each stage, its own matrix leaked on the code it replaced:
    stage 9's in 5,261 of 7,336 combinations, stage 10's in 4,063 of 5,193
    (on 98f6829, 32 labels), stage 11's in 4,562 of 5,391 (on 0e057e4).
- **Still not handled after stage 13 (can store a secret).** All checked
  by running them, none in the test suite as a passing case:
  - a value split across two lines ("my password is crim" / "son42");
  - a value before its label ("crimson42 is my password");
  - more than 6 filler words ("the password for a b c d e f g is x");
  - a comma, colon or sentence end inside the filler ("the password, for
    prod, is x");
  - a comma in an unquoted value ("password: elm51x,elm52x" masks only
    "elm51x"), and a quote escaped by doubling instead of a backslash
    ("'it''s elm53x'");
  - a label directly followed by manager, limit, count, budget, window or
    policy ("the password policy is hunter2");
  - labels that are not listed, such as "masterphrase", and labels in other
    languages that are not listed ("la clave es pino42", "das Kennwort ist
    tanne42");
  - connectors other than the ones in the table above (for example "la
    clave es x" uses "es", which is not a connector);
  - a wrapper nested deeper than 3 levels ("(a(b(c(deep991))))");
  - after a glued upper-case name, an unquoted value with a space
    ("PGPASSWORD=blue sky42": only "blue" is masked);
  - "<label>'s <value>" and "<label> as <value>" with a letters-only value
    ("the password's swordfish");
  - an unquoted item in a JSON-like list ('["x1y2z3", unquoted993]');
  - "with password swordfish" (letters only: not masked, so that "with
    password protection" stays);
  - `mysql -p PASS` with a space (mysql reads that as a prompt, so it is not
    masked); every cookie after the first in `-b "a=x; b=y"`; every line of
    a YAML block scalar after the first;
  - tokens that mix `/` and `-` so that no run of 32 base64 characters
    remains ("Ab3dEf7gHi9jKl1m/No3p-Qr5sTu7vWx9yZa1bCd3eFg");
  - curl `-H` header forms are not in the test suite. Three ad-hoc probes
    were masked (`X-Api-Key:`, `Authorization: Token`, `X-Auth-Token:`); other
    header names were not tested;
  - spaced-out letters ("p a s s w o r d: birch42");
  - lookalike letters outside the small Cyrillic map (Greek ο in "pοssword").
  - None of the third review's 14 inputs is still open: all are in the
    table above (stage 11).
- **Fourth review: variants of verified shapes that are not verified
  themselves.** Each was run on 236d2a3 through the real extraction path
  (a hostile scripted model, once with valid output and once with invalid
  output so the session is queued, every file under the home scanned), and
  each still reaches the prompt or a stored node, `memory.db` and
  `memory_queue`. None of them is in the verified table:
  - a blank line after a word connector: "The password is\n\n  jasper888"
    (word connectors allow only 1 newline);
  - a space after `==`: "My pwd == osprey223";
  - Markdown italics with underscores: "\_Password\_: wren101";
  - `blob=` followed by base64 containing `/` `+` `=`
    ("blob=Ab3dEf7gHi9j/Kl1mNo3pQ+r5sTu7vWx9yZ==");
  - (fixed in stage 13, no longer a gap) base64 with single `=` padding
    after a label and no connector: "My api key for prod
    YWRtaW46cGFzc3dvcmQ= works." and "My token YWRtaW46cGFzc3dvcmQ=
    works." (`YWRtaW46cGFzc3dvcmQ=` is base64 of "admin:password"). The
    `=` padding was read as the connector; both now mask only the token
    (rerun through the real path on the stage 13 code);
  - quoted base64 after a label: 'My token "ZGVwbG95OnNlY3JldA==" is
    active.';
  - `redis-cli -a` attached: "redis-cli -a'macaw215'" and
    "redis-cli -atoucan326";
  - a backticked label before `to`: "I set the \`password\` to almond762";
  - a quoted value after `as` or `'s`: 'Use the token as "filbert107"',
    "The password's 'chestnut218'";
  - an underscore earlier in a glued name: "export
    PROD_PGPASSWORD=buzzard763";
  - a JSON list under "secrets" spread over several lines;
  - XML with attributes or with the value on its own line:
    '<password type="prod">plover639</password>',
    "<password>\n  puffin740\n</password>", and CDATA
    ("<password><![CDATA[lapwing962]]></password>");
  - a quoted Basic token: 'Uses Basic "YWRtaW5AbG9jYWw6c2VjcmV0"';
  - curl with a single-quoted password only: "curl -u admin:'sable436'";
  - a modifier word before the label in "reset / changed ... to": "I reset
    the staging password to 'walnut540'", 'I changed prod password to
    "dewberry435"';
  - JSON Authorization with a scheme other than Bearer, Basic or Token:
    '{"Authorization": "ApiKey tamarind982"}';
  - a comment after a YAML block indicator: "password: | # prod";
  - "their" or "your" before the label after with / using: 'Connect with
    their password "tayberry324"' (only the, a, an, my, our are allowed).
- **Short token-shaped values.** Short token-shaped values below a
  pattern's minimum length are not masked (for example glpat-7sd5,
  whsec_rn6b, a 3-character padded base64): token shapes are recognized by
  length, so real tokens are not affected, but a short secret in these
  forms is not covered. The minimums are in the table's "minimum length"
  column. The substitution check above did not re-test values with special
  characters or spaces.
- **Single-`=` base64 in one case after a label (stage 15).** A value with
  one `=` of padding that is all lower-case, all upper-case, or capitalised
  only at the start, directly after a label and followed by more words or
  another line, is not masked: its `=` is read as the connector and the
  words after it are masked instead. Checked: "my token enp6eno= for Trail
  Tracker", "my token enp6enp6eno= for Trail Tracker", "my token QUFBQUE=
  for Trail Tracker", "my token Abcdefg= for Trail Tracker" and "my token
  enp6eno=" followed by a line "Trail Tracker deploys nightly." all leak to
  the prompt, a stored node and `memory_queue`, and so do 8-, 12- and
  20-character values of each case (`tests/memory/test_extraction_stage15.py`,
  33 strict xfails). Shorter one-case values (4 to 6 characters before the
  `=`, "my token abcd= for Trail Tracker") leak the same way. Not affected:
  `==` padding in any case, a single `=` at the end of the message, mixed-case
  values (row 21), and a value after a real connector ("api key is
  enp6eno= ...", "secret: ...", "password is ..."). The fix that was tried,
  treating 7+ base64 characters plus a single `=` directly after a label as
  the secret, also captured real filler words ("api key staging= x") and
  made the stage 9 fuzz leak in 64 combinations, so it was not applied.
- **Fifth review: more variants, not in the verified table.** Each was run
  on the stage 13 code through the real extraction path (hostile scripted
  model, valid and queued output, every file scanned). Still leak (prompt
  or stored node, `memory.db` and `memory_queue`):
  - a `.netrc` line: "machine github.com login bot password primrose446";
  - a Markdown table cell: "| prod | password | hibiscus921 |";
  - the label as a prefix of the name: "SECRET_VALUE=lavender743";
  - `curl -H "Authorization: Digest username=admin, response=mimosa654"`;
  - an unknown scheme: "Authorization: CustomScheme narcissus565";
  - `mongosh -u admin -p petunia387`;
  - three or more words between the connector and a quoted value: 'My
    password is very very really "quince332" for CI.' (one or two words
    are verified);
  - `password := "amaryllis554"`;
  - a Korean label: "비밀번호: begonia463";
  - a JSON `\u` escape inside a value: '{"password": "\\u006cilac831"}'.
    The value as written is masked and withheld, but its decoded form
    "lilac831" is not withheld, so a model that decodes the escape can
    store it;
  - a quoted value that continues on the next line ('password: "line1'
    then a line '  bluebell882"'). This input was not in the list before
    stage 13; the nearest earlier entry, "a value split across two lines",
    is kept above.
- **Why best-effort, and why only exact forms are claimed.** Independent
  reviewers invented new bypasses quickly: the second review found about 45
  leaks in 56 new inputs, and each later round found about 30 new variants
  of shapes that were already handled (the fourth review found 21 leaks in
  forms the docs claimed). That is why this is a best-effort filter and not
  a guarantee, and why since stage 12 only the exact verified examples in
  [Handled forms (tested)](#handled-forms-tested) are claimed.
- **Recall cost (security over recall).** A sentence that matches the rule
  (a label, a connector and a value) is masked and its node dropped. The longer label list
  makes this more common: "the token is valid for one hour", "the key point
  of Trail Tracker is speed", "the pass is valid until June", "auth is
  handled by Auth0", "the cookie banner is ugly" and "I set the token to
  expire hourly" all lose their value, and a glued upper-case name ending in
  KEY masks any value ("MONKEY=banana"). A value
  after `=`, and a value on a later line after `:`, is masked to the end of
  its clause.
- A credential variable whose name is not on the suffix list, such as
  `LUMEN_KEY`, is stored as a name ("Rotate LUMEN_KEY for Lumen Budget"); its
  value is never stored. This is the price of keeping `PRIMARY_KEY`, and it is
  why the held-out control group dropped in round 3 (see the table).
- **Gap 3 (closed for new working notes).** Working notes are now masked
  with the extraction filter's credential mask, in extraction's order, before
  they are written (see [Working layer](#working-layer)): verified forms only, with the same "Verified
  means" definition and minimum lengths as [Handled forms
  (tested)](#handled-forms-tested), and every gap in this list applies to
  them too, including the stage 15 one-case single-`=` base64 gap.
  `room/memory/redact.py` itself is unchanged. Only secret-shaped values are
  scrubbed across turns (the rule is under [Working layer](#working-layer));
  the cases that remain open are listed there and kept as strict xfails.
  Not covered: those boundaries, and notes written before this change whose
  session is not captured again with a new turn (they expire 14 days after
  their last update, and `room working purge --all` deletes every note).
  `room working promote` copies a note's turns as stored, so promoting an
  older note copies its older redaction.
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
  `agy -p` run, and the Antigravity desktop app and IDE are not captured).
  Before the write, each turn is masked in extraction's order
  (`working.redact_for_note`, gap 3): the extraction filter's credential mask
  on the raw text first (`quality.mask_credentials`), then `redact_secrets`
  (redact.py, unchanged; run on each piece between `[REDACTED]` markers, so
  it does not half-mask a token or swallow `&page=2` after a masked
  `api_key=`). Then secret-shaped values are scrubbed across the note:

  - **Which values.** Only secret-shaped ones, taken from each region the
    two steps above mask in a turn's raw text (code blocks included): the
    region whole, its unescaped form, its `user:PASS` and login
    `user/PASS` parts, the pieces between brackets, and its words and
    pieces. A value is secret-shaped if it is a token or key form
    (quality's token shapes, or a redact.py pattern such as `sk-`, `ghp_`,
    `AKIA`, a JWT or a PEM block), or has 8 or more characters with a
    digit, a symbol other than `_`, or an upper-case letter after the first
    one and a lower-case letter in one word. Plain words and phrases,
    numbers under 8 digits and values of 7 or fewer characters are never
    scrubbed from other turns, so a weak or plain-word secret repeated bare
    in another turn is not caught (see the list below). The marker
    `[REDACTED]` is never a value.
  - **From which turns.** Every turn the capture passes (both captures
    re-read the whole transcript, so a resumed Claude Code session or a
    later Antigravity Stop hook still sees the first turns; the raw text
    stays in memory and is never written), Antigravity replies that a later
    reply in the same turn replaced, the turns already stored, and the raw
    project folder name.
  - **Where they are replaced.** In every turn of the note. A match counts
    when at least one of its ends is not glued to a letter or digit; `_`
    and other symbols count as a boundary (`tok_<secret>_end`,
    `prod_<secret>_v2`, `xx@<secret>yy`). A match that overlaps an existing
    marker is skipped. Matching runs on the normalized text: zero-width
    characters are removed and quality's Cyrillic lookalikes (а е о р с х
    and their capitals) are read as Latin; checked with а and U+200B.
  - **Cuts.** A captured message is cut at 2,000 characters only after it
    is masked and scrubbed. When a note is updated, its earlier turns are
    rebuilt from their raw text with the new values before the cut, so a
    secret-shaped value across the cut is masked whole even when it is
    labelled only in a later capture (checked with Claude Code resume and
    Antigravity Stop hooks).
  - **Project name.** The capture passes the raw folder name; it is masked,
    scrubbed with the note's values, then made safe (letters, digits,
    space, `_ . -`) and cut to 40 characters; checked for secrets across the
    40th character and for names with underscores (`svc_<secret>`). The
    capture log records the name exactly as stored in the note.
  - **Refusal.** If a secret pattern still matches one turn on its own, or
    the project name (redact.py's, checked piece by piece, or the
    extraction filter's), nothing is written.

  What is verified, by `tests/memory/test_working_redaction.py`,
  `test_working_redaction_fuzz.py` and `test_working_redaction_review*.py`
  (three review rounds): the exact examples in [Handled forms
  (tested)](#handled-forms-tested), with only the secret value changed to
  another value of the same kind, at least the row's minimum length and
  within its case limit (3 values per example, 525 inputs, plus the 45
  hostile inputs), captured through Claude Code, Antigravity and a direct
  save and read back through `room working list/show/search` and the MCP
  tools; and the reviews' cases for secret-shaped values. Ordinary text in
  other turns is not changed: the third review's 9 cases ("auth = False"
  then "retry is False", port 8080 then 9090, a PEM block then "keep the
  repository private", and so on) and 40 more ordinary sentences are stored
  unchanged, and 20 ordinary sessions are stored exactly as before.

  Not covered (each checked by running it; the ones marked xfail are strict
  xfails in the tests):

  - a plain-word, short (7 or fewer characters) or under-8-digit secret
    repeated bare in another turn ("swordfish", "774192", `vqx"zmk`; xfail);
  - 4-letter words inside a masked free-text clause ("kiwi" from "the
    password is blue kiwi fish"; xfail), and 1-character values (xfail);
  - a secret glued inside a longer letters-and-digits token
    (`abc<secret>def`; xfail);
  - URL-decoded repeats (`lil%40c…` masked, a later `lil@c…` not; xfail) and
    JSON `\u` escapes (xfail);
  - a value split across two turns (xfail);
  - a note written before this change: it is masked again in full only when
    its session is captured again with a new turn; captured again with no
    new turn, it is not rewritten (xfail);
  - a label with no connector ("seed phrase tangerine basalt pelican
    spruce"; xfail) and a value below a code block under its label (xfail);
  - labels that are not listed, a value before its label, a label used as a
    variable prefix ("SECRET_VALUE=x"), values below a row's minimum length,
    and more than 6 filler words;
  - Cyrillic letters outside the folded set and Greek lookalikes;
  - the stage 15 one-case single-`=` base64 gap, and every other gap in
    [Known gaps](#known-gaps).

  Recall cost: within a turn, a label, a connector and a value are masked
  even when harmless ("auth = False", `sorted(xs, key=len)`, "foreign key
  constraints = on"), as in extraction. That masking no longer spreads to
  other turns: since the third review only secret-shaped values are scrubbed
  across turns, so "False", "on", "len", "8080" or "private" elsewhere in
  the note are kept. Sessions with fewer than 2 typed
  messages or
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
