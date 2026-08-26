# Development notes — what was built and how

A complete account of how `claude-handoff` came to be — the schema
reverse-engineering, the design decisions, the bugs hit along the way, and
what was (and wasn't) tested. Written so a contributor — human or AI — can
pick the project up cold. Sections marked with a version reflect when a
decision landed; everything else dates to v0.1.0.

## 1. The problem

Claude Code stores every session locally as JSONL under
`~/.claude/projects/<encoded-project-path>/<session-id>.jsonl`. The format is
**undocumented and unstable across versions**. A raw session is mostly noise
for a handoff: tool calls, tool results, thinking blocks, system reminders,
subagent chatter. Existing exporters convert this to readable Markdown/HTML
but keep the noise; existing handoff skills run *inside* the session and must
be invoked before it ends. The goal here: a **post-hoc, external CLI** that
turns any session file — including old or crashed ones — into a single
`handoff.md` another LLM can continue from.

## 2. Schema reverse-engineering

Instead of trusting third-party writeups, the schema was derived from a live
session file (Claude Code v2.1.241). Method: a throwaway Python script that
counted record `type`s, collected key frequencies, and printed one sample
record per type.

Input formats accepted today: Claude Code JSONL sessions, claude.ai data
exports (`conversations.json`, `chat_messages` arrays), and ChatGPT data
exports (`conversations.json`, `mapping` node graphs walked backward from
`current_node`). The JSONL schema notes below apply to the first.

Record types observed (one JSON object per line):

- `user` / `assistant` — the conversation. Carry `message`, plus envelope
  metadata: `uuid`, `parentUuid`, `timestamp`, `sessionId`, `cwd`,
  `gitBranch`, `version`, `isSidechain`, sometimes `isMeta`.
- `summary` — session title, written by Claude Code itself (older format).
- `queue-operation`, `attachment`, `atis-latch`, `last-prompt` — newer
  operational records (v2.1.x); all ignorable for a handoff except
  `last-prompt`, which is handy for `--list`.

Message content shapes:

- `user` content: either a plain **string** or a **block list**
  (`text`, `image`, `tool_result`).
- `assistant` content: always a block list — `text`, `thinking`
  (with signature), `tool_use` (`id`, `name`, `input`).
- Tool results come back as *user* records whose content is
  `tool_result` blocks (string or block-list `content`), plus a top-level
  `toolUseResult` field.

Noise conventions discovered:

- `<system-reminder>…</system-reminder>` injected into user messages.
- Slash-command envelopes: `<command-name>`, `<command-message>`,
  `<command-args>`, `<local-command-stdout>`, `<local-command-stderr>`,
  `<local-command-caveat>`.
- `Caveat: The messages below were generated…` preamble on meta messages.
- `<task-notification>` blocks — background-task completions delivered as
  user-role records; the `<summary>` tag is the only human-relevant part
  (Claude Code HTML-escapes the payload it embeds, so unescape it).
- `isMeta: true` user records (not real human input).
- `isSidechain: true` records — subagent branches.

Subagent transcripts, separate-file shape (v0.9.0, observed on Claude
Code v2.x): newer versions no longer inline subagent records — each agent
gets its own file at `<project>/<session-id>/subagents/agent-<id>.jsonl`,
next to the session's JSONL. Same `user`/`assistant` record schema as the
main file (all with `isSidechain: true`), plus extra envelope fields:
`agentId` (matches the filename), `promptId`, `sourceToolAssistantUUID`
(links back to the main-session assistant record that issued the `Agent`
tool call), and `attributionAgent`/`attributionSkill`/`attributionPlugin`.
The file's **first user record is the agent's task prompt**; the parent
session references the agent only through the `Agent` tool_use/tool_result
pair (`agentId: <id>` inside the result text). Discovery is
directory-based (`_parse_agent_files`), not id-based, so it needs no
fragile parsing of that result text.

## 3. Design decisions

| Decision | Rationale |
|---|---|
| Single file, stdlib only, Python ≥3.9 | The user's brief: "without extra shits". Also makes it `curl`-able and auditable. |
| Deterministic mode is the default | Zero cost, offline, no API key, reproducible. LLM summarization is opt-in (`--llm`). |
| Parse defensively | Content may be string *or* list; unknown record types are skipped, corrupt lines tolerated (`errors="replace"`, per-line `try/except`). Schema drift is the main long-term risk. |
| Drop, don't keep: tool results, thinking, sidechains, meta | Tool results are bulky and re-derivable; thinking is private reasoning; sidechains are another agent's transcript. The *activity summary* (files touched, commands run) preserves the useful residue. |
| Subagent activity merged by default, transcripts opt-in, v0.9.0 | With separate-file agent transcripts the "useful residue" rule broke: in multi-agent sessions the real edits happen in agent lanes, so a default handoff claimed "no files changed". Now agent files/commands always merge into the activity summary (with a 🤖 marker line + count), while full agent texts stay behind `--include-sidechains` — same lean-by-default philosophy. |
| Output redaction by default, v0.10.0 | The tool's main use is pasting the handoff into a web chat — the document itself is egress, not just what goes to an LLM under `--llm`. `redact_doc` runs on every final document (CLI markdown/JSON, hook files, MCP replies); `--no-redact` opts out on the CLI, hook/MCP always redact. LLM-input redaction stays as defense in depth. |
| `--grep` content search, v0.10.0 | `--name` only sees title + first prompt; "which session talked about CORS?" needs the conversation itself. Case-insensitive substring over parsed user/assistant turns (tool noise deliberately excluded), match preview shown in `--list`/`-i`. Substring, not regex — YAGNI until asked. |
| `--fit` token budgets, v0.10.0 | Receivers have context limits; chars÷4 approximates tokens with zero dependencies. Fixed sections (header, activity, sidechains) are measured, the transcript cap absorbs the rest — instructions and structure are never trimmed, honoring the "never truncate instructions" invariant. Every write now reports a ≈token estimate. |
| Merge consecutive assistant records into one turn | One user prompt can produce dozens of assistant API-call records interleaved with tool results. A reader wants turns, not records. |
| Head+tail truncation, never head-only | Per message (70/20) and globally (35/60): the opening sets the goal, the recent end is the current state — the middle is the most expendable. |
| LLM calls via raw `urllib` | No SDKs → no dependencies. Keys from env, model overridable with `--model` since default model ids rot quickly. |
| Provider registry (`PROVIDERS`), v0.2.0 | Each provider = accepted env keys (first hit wins, graphify-style — incl. `CLAUDE_API`/`GPT_API`/`GEMINI_API` aliases) + default model + a call strategy function. Adding a provider touches nothing else (open/closed). |
| `--llm claude-cli`, v0.2.0 | Shells out to the locally-installed Claude Code CLI (`claude -p --output-format json --no-session-persistence`), so Pro/Max subscribers get real summaries with **no API key**. `CLAUDE*` env vars are scrubbed from the subprocess (except `CLAUDE_CODE_OAUTH_TOKEN`) so nested runs from inside a Claude Code session authenticate like a fresh CLI. Same pattern graphify uses for its `claude-cli` backend. |
| SOLID without classes, v0.2.0 | Public-repo maintainability pass: `parse_session` and `main` split into single-responsibility helpers; behavior verified byte-identical against v0.1.0 output on a real 4 MB session and the fixture. |
| Handoff preamble addressed to the *receiving* assistant | The output must work as a first message with zero extra prompting. |
| Package split + generated single file, v0.11.0 | The module hit ~2100 lines; readability won. Nine single-responsibility modules with acyclic `from .module import name` imports; `scripts/build_single.py` stitches them into `single/claude_handoff.py` so the curl-an-auditable-file install survives. The package is the only source of truth — CI fails when the artifact is stale, and the split was proven mechanical: outputs byte-identical before/after on all fixtures and a real multi-agent session. |
| Streaming parse + grep prefilter, v0.11.0 | `load_records` became a generator (peak RSS parsing a 20 MB session: 80 MB → 26 MB) and `--grep` runs a cheap raw-text superset scan before paying for a full parse — only for needles JSON escaping cannot hide (ASCII without quotes, backslashes, newlines); anything else keeps the full-parse path. The scan is binary — ASCII needles compare against `bytes.lower()`ed raw blocks, skipping UTF-8 decode and Unicode folding (profiled at ~70% of --grep time). Worst-case store-wide grep: 3.3 s → 0.5 s. |
| `--anonymize`, v0.12.0 | Redaction removes secrets; it doesn't remove *identity*. Anonymize collapses the home dir to `~` and replaces emails/IPv4s/the bare username with placeholders so a handoff can be pasted publicly (issues, forums). Opt-in — a handoff meant to continue work needs its real paths. Same egress seam as `redact_doc`; hook always redacts, MCP exposes it as a tool argument. |
| Config defaults, v0.12.0 | `~/.config/claude-handoff/config.json` feeds `argparse.set_defaults`, so precedence is CLI flag > config > built-in with zero custom merging logic. Allow-listed keys only; `no_redact` is deliberately excluded — weakening redaction must be explicit per run. Broken configs warn and are ignored, never fatal. |
| MCP LLM opt-in (`--allow-llm`), v0.12.0 | The MCP server stays deterministic by default ("no implicit paid calls"); starting it with `--allow-llm` adds `llm`/`model`/`focus` to the handoff tool schema, so cost is a server-operator decision, never a client's. |
| Project memory (`--brief` + SessionStart hook), v0.13.0 | The store's unique asset is the user's ENTIRE history; distilling it into one cited brief and injecting it at session start turns claude-handoff from a handoff tool into a local long-term memory layer. Deterministic skeleton (timeline + rollup) is always factual; the LLM adds Decisions/Fixed/Conventions/Open-threads sections with per-bullet session citations (never-invent, verifiable). Per-session notes are cached by prompt+content hash — refreshes pay only for new sessions plus one reduce. Injection uses the SessionStart hook's plain-stdout-becomes-context contract (matcher startup\|resume\|clear\|compact); hooks never call LLMs — the SessionEnd hook auto-refreshes only the free factual skeleton (existing, stamped briefs only — it never creates files), while distillation stays an explicit user command. A machine-readable stamp (sessions count, newest mtime, distillation age) makes staleness visible: the file gets a "N newer sessions since this distillation" note and the injection adds a warning when the brief lags the store. Timeline is capped at 20 bullets so injection cost stays bounded. |
| Brief notes map-reduce + multi params, v0.15.0 | The memory path must not inherit head+tail loss: sessions beyond `NOTE_INPUT_CAP` get chunk notes on turn boundaries plus one synthesis (all cached) — same no-silent-drop guarantee as `--llm`, at any size; only monster sessions pay extra. Deterministic handoffs keep head+tail by design (the quick paste-able doc). Also: several positional paths merge into one handoff, `--grep` repeats with AND semantics (the raw prefilter scouts with the first ASCII-safe term), `--project` repeats with OR. `--brief` still demands a single project. Sampling variants (head/tail + random-middle notes, a possible `--skim`) were considered and deliberately parked: the one-off saving is small next to the cache, the blind spot permanent — revisit only if first-build cost draws real user complaints. |
| Distilled sub-headings demoted at assembly, v0.16.0 | The note prompts ask for `## Decisions` etc. and their text is part of the note-cache hash, so restructuring the headings in the prompts would re-distill every cached note. Instead `build_brief_llm` demotes H1/H2 in the LLM output to `###` deterministically at assembly — the sections nest under `## Distilled memory` instead of rivaling the skeleton's H2s, prompts stay untouched, and it holds even when the model ignores heading instructions. |
| Git-aware brief freshness, v0.16.0 | Sessions are not the only thing that moves — commits land between distillations (or entirely outside Claude). The skeleton now shows `Repo HEAD` (hash + date + subject) and both the SessionEnd refresh and the SessionStart injection warn when commits postdate the last distillation. Implemented by reading `.git/logs/HEAD` (reflog) directly — spawning `git` would break the no-subprocess invariant of the deterministic path. Commit-action entries only, best-effort like the cache: any unreadable/absent repo silently yields nothing. Compared against `distilled` (not `built`): the factual skeleton self-refreshes, it is the distilled memory that rots. |
| Plain `--brief` grafts, never clobbers, v0.16.0 | The deterministic rebuild used to overwrite the stamped brief file wholesale, silently discarding the LLM-distilled section the user paid for. The graft logic (preserve distilled + re-derive freshness notes + carry the stamp) was extracted from `update_brief_skeleton` into `graft_distilled` and the CLI path reuses it when the destination is the stamped brief file. An explicit `-o` elsewhere stays a plain skeleton — exporting a clean copy remains possible, losing paid work does not. |
| Freshness notes carry the delta, not a count, v0.17.0 | Live failure (2026-08-26): a fresh session read "3 commit(s) landed after this distillation — may lag", still trusted the distilled open threads, and recommended work the repo had already shipped. Counts warn; deltas inform. The graft notes now list the undistilled sessions (date + id + title) and the commits since distillation (sha + subject), capped at `STALE_LIST_CAP=6` newest with an "earlier omitted" bullet — head+tail convention, newest is the informative end. The SessionStart injection separately lists commits newer than the file's own `built` stamp: those appear in no note on disk, so the hook is the only place that can surface them. Everything stays deterministic — reflog-read only, no LLM, hooks stay inert. |
| `Fixed` category defined in every distill prompt, v0.17.0 | Non-fixes (README badges, releases, listings) kept landing under `## Fixed` — the prompts named the heading but never defined it, so the model guessed. All four note prompts now pin the same rule: only defects actually diagnosed and resolved; releases/version bumps/docs/badges/listings/promotion file under Decisions or Open threads. Pinned by `BriefCategoryTests` (contiguous phrases — mind the line wrap). Prompt text is part of the note cache key, so stale notes self-invalidate; no CACHE_VERSION bump needed. |
| Digest transcript by default, `--full` for verbatim, v0.17.0 | Dogfooding a 270 MB / 13-day session showed verbatim head+tail keeping ~6% of turns while dumping raw recent messages — the user's verdict: "it's a summarize tool". The default now renders one capped bullet per turn (user 300 / assistant 500 / compact 800 chars, `one_line` head-only — the lead carries the outcome), lifting coverage to ~43% with head+tail as overflow fallback. A notice under `## Conversation` names both escape hatches. LLM consumption points (`build_llm`, brief notes, json+llm outbound, `--with-transcript`) pass `full=True` explicitly — the summarizer must never read a digest. |
| Notification role, v0.17.0 | `<task-notification>` records arrive as user-role JSONL but are machine messages; rendering them as 🧑 misattributes words to the human and inflated one real session from 94 to "333 user messages". They become `notification` turns — `<summary>` only (entities unescaped, payload dropped, same rule as tool results), counted separately in the header. Detection is `startswith` after `clean_text`, so a human *pasting* a notification stays a user turn. `<local-command-caveat>` joined `NOISE_RE`. |
| File inventory ranked, capped at 15, relative paths, v0.17.0 | That session listed 546 files alphabetically (~35 kB) while the transcript budget starved — the least informative section ate the context. Now: sorted by edit count desc, top `FILES_CAP=15` (with the digest carrying the narrative, the inventory is orientation, not the record), displayed relative to `meta.cwd` since the header already names the root (`~`-collapsed outside it — `_rel_path`), `…plus N scratchpad/temp file(s)` folds tempdir paths, and the cap line points at `--format json`, which keeps full absolutes (no silent caps, JSON is ground truth). In `--fit` the section counts as untrimmable overhead, so shrinking it directly buys transcript room. |
| Token line splits cache re-reads, v0.17.0 | `tok_in` summed input+cache_read+cache_creation → "3,934,092,604 in (incl. cache)", a number that reads as cost and informs nobody. `tok_in` is now fresh input (+cache writes), `tok_cache_read` renders as `(+N cached reads)`. |
| Header stats count what the reader sees, v0.17.0 | `n_assistant` is derived from merged turns after parsing (records overcounted ~3×); subagent tool calls surface as `(+N in subagents)` — previously the command list could exceed the "tool calls" figure, which read as impossible; `gitBranch: "HEAD"` renders as `(detached HEAD)`. `looks_trivial` gained an `n_agents` guard: turn-counting shrank the threshold sum, and a session that dispatched subagents must never be auto-skipped (a brief silently lost one before the guard). |
| Email egress heads-up, v0.17.0 | Default redaction targets secrets, not identity — an Apple ID in a terminal paste sails through, correctly. The CLI now counts distinct emails in the final document and prints one ℹ stderr line naming `--anonymize`. Informational only, CLI-only (hooks stay quiet), never a rewrite. |
| Brief opens with `## What this is`, v0.18.0 | Field report (2026-08-26): the user read their own finished brief and couldn't tell what app it described — decisions/fixes/threads assume an identity the document never states, and the receiving model (a real cross-model handoff to Gemini) had to infer it from crumbs. The reduce prompt now leads with a 2-3 sentence identity section (product, stack, current state; no citations — it synthesizes the whole). Deliberately NOT added to the session-note prompts: their text is the note cache key, and identity synthesis needs no per-note help — so an existing brief re-distills with one LLM call instead of re-paying every note. Pinned both ways by `test_brief_opens_with_what_this_is`. |
| Brief session selection (`--exclude`, `--keep`) is sticky via the stamp, v0.18.0 | Twin duplicated sessions polluted a real brief with double citations, and an 88-agent monster dominated another — users need to leave sessions out and to bound huge histories. Selection must survive refreshes: the SessionEnd hook rebuilds the skeleton, so a CLI-only flag would resurrect excluded sessions in the timeline. Both settings therefore live in the stamp as OPTIONAL fields (`exclude=`, `keep=`) — stamps without them stay byte-identical to the old format, so old CLIs keep parsing unselected briefs. `--exclude` matches id prefixes only (citations/`--list` show them; fuzzy names could silently drop the wrong session), bare opens a picker composed from `_parse_pick`; only prefixes that matched persist (typos warn, they don't live in the stamp forever). `--keep first:N,last:M` re-applies chronologically on every refresh — a sliding window. Excludes apply after the keep window; hooks honor both and skip excluded sessions in staleness nags. |
| Brief maps state and orders the resume plan, v0.18.0 | Second field report from the same cross-model handoff (2026-08-26): the receiving model produced a decent plan, but the *user* had to judge it — the brief listed open threads without saying what was done, half-done, or untouched, and in no order. `## Where things stand` is the done/not-done map (shipped-and-verified / in flight with where it stopped / not started — only status the notes show), and `## Open threads` became the resume plan: ordered for pickup, one concrete next action per bullet, status-tagged. Citations extended to every section including `## What this is`, closing the "every claim cites its session" promise. Word cap 600→800 to pay for the new section. Reduce-prompt-only, same reasoning as the identity section: notes are cache keys, and both sections synthesize across sessions. |
| Brief distillation shows live progress, v0.18.0 | The brief path printed its first stderr line only after the first LLM call completed — a minute of silence a user read as "hung" (live report, 2026-08-26). Instead of new machinery, the brief now composes the chunk pipeline's existing progress kit (`_new_progress`/`_progress_step`/`_progress_finish`): a plan line lands before the first call, every call (chunk, session reduce, final reduce, cache hit) is one bar step, and `st["total"]` grows as big sessions reveal their part counts — the bar is honest about work it hasn't discovered yet. `_announce` routes events onto the live bar on a TTY and to plain lines everywhere else, so pipes/CI/hook contexts stay line-oriented. |
| claude-cli scrubs stray API keys, v0.18.0 | Live failure (2026-08-26): `chf --brief --llm claude-cli` died with "Credit balance is too low" for a Max subscriber — `claude -p` prefers an exported `ANTHROPIC_API_KEY` over the login, and one lived in `~/.zshrc` pointing at an unfunded Console account (interactively harmless: the user had told claude "don't use this key", but `-p` doesn't ask). `_call_claude_cli` now drops `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` alongside the `CLAUDE*` scrub — the mode's contract is "the subscription pays; no API key involved", so a stray key must never rebill it. API providers (`--llm claude`) still read the key, of course. |
| Injection defense + PreCompact hooks, v0.14.0 | Transcripts embed untrusted external text (tool results, pasted docs); the LLM consumption points (SUMMARY/CHUNK/NOTE/BRIEF prompts), the handoff preamble, and the brief injection wrapper now all frame it as data-not-instructions, pinned by tests — a mitigation, not a proof. Both hook installers also register PreCompact (matcher manual\|auto): a handoff snapshot before compaction squeezes detail away, and a free brief-skeleton refresh mid-session. CHUNK_PROMPT changed → CACHE_VERSION bumped to 2. |
| Picker multi-select, v0.12.0 | `-i` accepts `1,3` / `2-4` (`_parse_pick`) and merges the picked sessions through the same machinery as `--merge` — composition over new code paths. |

## 4. Pipeline architecture

The `claude_handoff/` package — `textutil` → `redact` → `parse` →
`webexport` → `discovery` → `render` → `llm` → `integrations` → `cli`,
stitched by `scripts/build_single.py` into `single/claude_handoff.py`
for curl installs — in pipeline order:

1. **Discovery & selection** — `find_sessions()` globs
   `~/.claude/projects/*/*.jsonl`, newest first; `--project` filters by
   path substring, `cwd_project_filter()` scopes to the current
   directory's project (or a parent "master folder"), `--any` disables
   scoping. `session_label()` (title + first prompt) powers `--list`,
   `--name` matching and the `-i` numbered picker. Auto-selection skips
   nearly-empty sessions (`looks_trivial`) — e.g. the stub `claude /login`
   leaves behind. Web exports (claude.ai `chat_messages`, ChatGPT
   `mapping`/`current_node`) are detected by `is_web_export()` and parsed
   by `parse_web_export()` into the same shape as JSONL sessions.
2. **Parsing** — `parse_session()` is the heart: single pass over records,
   producing `meta` (session id, cwd, branch, models, timestamps, counts),
   `turns` (role + text parts + tool one-liners), `files_written/read`,
   `commands`, and a `tool_use_id → name` map. `_parse_agent_files()` then
   folds separate-file subagent transcripts (`<session-id>/subagents/
   agent-*.jsonl`) into the same state: their files/commands join the
   activity dicts, their prompt + assistant texts become sidechain groups
   (sorted by first timestamp), and `meta.n_agents` counts them. Groups
   carry a `label` — the `description` of the `Agent`/`Task` tool call that
   spawned them, linked through the `agentId` in the launch tool_result —
   and mid-run parent steering messages interleave into `texts` as
   "🧭 Parent: …" lines (v0.10.0).
3. **Rendering** — `render_header` (preamble + session facts),
   `render_activity` (files ranked by edit count, capped at 40, temp
   files folded; deduped command list), `render_transcript` (default: a
   condensed per-turn digest — 🧑/🤖 bullets, 🔔 notification one-liners;
   `--full` for classic verbatim turns; tools collapsed into `<details>`
   with `--include-tools`, else a `[N tool calls]` marker),
   `render_footer`.
4. **LLM mode** — `build_llm()` redacts secrets (`redact_secrets`), then
   feeds activity + full cleaned transcript to `llm_summarize()` with a
   fixed prompt that demands six sections (Goal / Key decisions / Current
   state / Files & artifacts / Next steps / Constraints & preferences),
   forbids invention, and answers in the user's language; `--focus` adds
   user instructions (applied in the reduce pass only). Keys resolve via
   `provider_key()` (aliases, first hit wins) and dispatch through the
   `PROVIDERS` registry: `_call_claude`/`_call_openai`/`_call_gemini` over
   `urllib`, `_call_claude_cli` over `subprocess` (env-scrubbed),
   `_call_ollama` against a local server. Transcripts beyond
   `LLM_INPUT_CAP` are map-reduced on turn boundaries (`_chunk_text`),
   with a content-addressed note cache, per-chunk retry, live progress
   bar, and 4-way parallelism for API providers (`SERIAL_PROVIDERS`
   excluded). `--with-transcript` appends the cleaned transcript.

5. **Filters & composition** — `slice_turns()` (`--last`, `--since`)
   keeps only the conversation tail with an honest note; `merge_parsed()`
   (`--merge`) folds every session in scope into one chronological
   document with ⏱ session-break turns; `/compact` summaries render as
   "📜 Compacted history"; `--include-sidechains` appends subagent work.

6. **Outputs** — markdown (default), `--format json`
   (`build_json`), `-o -` stdout, `-o clipboard` (`_copy_clipboard`).

7. **Integrations** — `--install-hook` adds a Claude Code SessionEnd hook
   (`run_hook_mode` reads the hook JSON on stdin and writes to
   `~/.claude/handoffs/`, swallowing every error by design);
   `--mcp` runs a stdio MCP server (`run_mcp_server`: newline-delimited
   JSON-RPC; tools `list_sessions` and `handoff`); `--completions`
   emits shell completion snippets.

## 5. Bugs found while dogfooding

The tool was tested against the very session that built it, which surfaced
two real bugs:

**The vanishing assistant.** First run produced an almost empty conversation:
one assistant line for a whole working session. Cause: in SDK/Cowork
sessions the assistant's prose is not emitted as `text` blocks — it is sent
through a `SendUserMessage` **tool call**, and the human's multiple-choice
answers come back inside `AskUserQuestion` **tool results**. Fix: treat
`SendUserMessage.input.message` as assistant text, and surface
`AskUserQuestion` tool results as user turns (this is what the
`tool_use_id → name` map exists for). Classic interactive CLI sessions are
unaffected.

**Inflated reply count.** `n_assistant` incremented whenever the current
turn had *any* accumulated text, so every tool-only record after the first
text bumped the counter. First fixed with a per-record `added_text` flag —
still ~3× what a reader sees (one visual reply = many text-bearing API
records). Final form: derived from merged turns after parsing.

## 6. Testing

- **Real session** (v2.1.241, SDK/Cowork flavor): dogfooded end-to-end;
  output manually inspected.
- **Synthetic fixture** (`tests/fixtures/classic_session.jsonl`): classic
  interactive-CLI format — string content, `summary` record, `isMeta`
  caveat message, slash-command envelope, string-content tool results,
  a sidechain record, Greek text. Verifies filtering, turn merging,
  file/command extraction, `<details>` rendering.
  `notification_session.jsonl` adds a `<task-notification>` record — it
  also runs in `build_single.py --check`, pinning the stitched artifact's
  `import html` (a runtime-only import no other fixture exercises).
- **LLM path** (v0.2.0): `claude-cli` exercised end-to-end through a shim
  `claude` binary emitting the real envelope format, plus mocked-subprocess
  unit tests (envelope parsing, missing binary, nonzero exit, is_error
  surfacing). Key-alias resolution verified **live** against the Anthropic
  API (fake `CLAUDE_API` key → correct 401 `authentication_error`, proving
  the full alias → registry → `http_json` wiring). Full live summaries via
  paid API keys remain unverified; treat the three HTTP payloads as
  "correct per docs". A live `--llm claude-cli` run requires a
  logged-in CLI (`claude` → `/login` once).
- **Error paths**: missing API key, nonexistent file, empty session — all
  exit with a one-line message, no tracebacks.
- **Packaging**: `pip install -e .` + `claude-handoff --version` +
  fixture run, verified in-container.

## 7. Known limitations

- Schema drift is a *when*, not an *if*; parsing is defensive but a future
  format change can silently drop content. The fixture suite is the canary.
- Images are reduced to `[image attached]` / `[attachment]`.
- Gemini exports are not accepted — Google Takeout ships HTML only, with
  no stable structure; a parser should be built fixtures-first against a
  real export, not guessed.
- Live-call coverage: deterministic mode, `claude-cli` (incl. a 1.5M-char
  map-reduce) and clipboard are verified live; `ollama` and the three
  HTTP providers are verified via mocks plus a live 401 round-trip
  against the Anthropic API (fake key). Full paid-key summaries remain
  spot-checked, not CI-tested.
- The MCP server exposes deterministic handoffs only (no `--llm` — an
  MCP client shouldn't trigger paid calls implicitly).
- Sessions from other coding CLIs (Codex, Cursor, …) are out of scope;
  cli-continues covers CLI→CLI moves well.

## 8. Zero-trust & failure model (v0.4.0)

**Trust boundaries.** Everything the tool touches is treated as untrusted:

- *JSONL input*: undocumented, drifts, may be corrupt or mid-write (live
  sessions) — per-line `try/except`, `errors="replace"`, unknown record
  types skipped non-fatally, nothing evaluated.
- *Transcript content*: routinely contains secrets pasted into commands —
  secret-shaped strings (`sk-…`, `ghp_…`, `AKIA…`, `AIza…`, JWTs, Slack
  tokens, `KEY=value` assignments) are **redacted before any egress**
  (`redact_secrets`, on by default, `--no-redact` to opt out). Since
  v0.10.0 "egress" includes the final document itself (`redact_doc` on CLI
  markdown/JSON, hook files, and MCP replies) — the handoff's whole point
  is to be pasted somewhere else. Hook and MCP outputs always redact.
  `--anonymize` (v0.12.0) goes one step further for *public* sharing:
  identity (home paths, emails, IPs, username), not just secrets.
- *LLM output*: untrusted text — only ever written into markdown, never
  executed or parsed as commands.
- *LLM input & downstream readers* (v0.14.0): transcript content fed to
  summarizers, and our own outputs read by receiving models, are framed as
  data-not-instructions at every point (prompts, handoff preamble, brief
  injection wrapper). Prompt-level guards are mitigation, not proof.
- *Subprocess (`claude-cli`)*: fixed argv list (no `shell=True`), scrubbed
  environment, hard timeout, stdout parsed strictly as JSON.
- *Network*: only under `--llm`, only to the three hardcoded provider
  hosts over HTTPS; keys read from env, never logged or persisted.
- *Cache*: best-effort local files; unreadable entries ignored; a failing
  cache can never fail a run.

**Failure points & mitigations.**

| Failure | Mitigation |
|---|---|
| Schema drift silently drops content | Defensive parse; fixture suite is the canary; add a fixture per new shape |
| Session file being written while read | Corrupt/partial lines tolerated per line |
| Huge sessions (1M+ tokens) | Map-reduce chunking (`CHUNK_CAP`) instead of dropping the middle; only the notes budget is ever truncated, **never the instructions or --focus** |
| Provider 429/5xx mid-run | Per-chunk retry (one, 3s backoff) + chunk-note cache → a rerun resumes instead of re-paying |
| `claude` CLI missing / logged out | Preflight `shutil.which`, CLI's own error surfaced verbatim (e.g. the revoked-token 401), `/login` hint |
| Nested run inside a Claude Code session | `CLAUDE*` env scrub (keeps `CLAUDE_CODE_OAUTH_TOKEN`) |
| `claude /login` stub becomes "latest session" | Auto-selection skips nearly-empty sessions |
| Typos / wrong flags | `_HelpfulParser` points to `--help` / `--list` |
| Stale or corrupt cache entries | Content-addressed keys — sha256(CACHE_VERSION, provider, model, chunk); bump `CACHE_VERSION` when `CHUNK_PROMPT` changes; `--no-cache` |

**Chunk pipeline ("batches").** Chunks split on turn boundaries, processed
sequentially (parallel `claude -p` subprocesses conflict — same reason
graphify serializes its claude-cli backend). Each chunk note is cached
before moving on, so an interrupted 30-chunk run redoes only what's
missing. `--focus` is applied **only in the reduce pass** so cached chunk
notes stay reusable across runs with different focus instructions.

**Known residual risks.** Redaction is pattern-based — an exotic secret
format can slip through (`--no-redact` exists precisely because the
inverse — false positives — is also possible). The `--llm` providers see
whatever survives redaction; users summarizing sensitive sessions should
prefer `--llm claude-cli` (their own Claude account) or deterministic mode.

## 9. Competitive positioning

See `docs/RESEARCH.md` (gitignored, internal). Short version: exporters
(claude-conversation-extractor, claude-code-log) stop at readable
transcripts; in-session handoff skills (thepushkarp/handoff,
claude-session-handoff) must run before the session ends and target the
next *Claude* session; browser extensions (Handoff, LLM Context Bridge)
cover web chats only; cli-continues moves sessions between terminal
coding CLIs (deterministic only, no web-chat targets). The niche here:
post-hoc + cross-model + paste-anywhere + optional LLM summary.
