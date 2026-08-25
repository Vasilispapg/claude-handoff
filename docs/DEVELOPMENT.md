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
  `<command-args>`, `<local-command-stdout>`, `<local-command-stderr>`.
- `Caveat: The messages below were generated…` preamble on meta messages.
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

## 4. Pipeline architecture

`claude_handoff.py`, top to bottom:

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
   `render_activity` (files created/modified with edit counts, deduped
   command list), `render_transcript` (🧑/🤖 turns; tools collapsed into
   `<details>` with `--include-tools`, else a `[N tool calls]` marker),
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
text bumped the counter. Fixed with a per-record `added_text` flag.

## 6. Testing

- **Real session** (v2.1.241, SDK/Cowork flavor): dogfooded end-to-end;
  output manually inspected.
- **Synthetic fixture** (`tests/fixtures/classic_session.jsonl`): classic
  interactive-CLI format — string content, `summary` record, `isMeta`
  caveat message, slash-command envelope, string-content tool results,
  a sidechain record, Greek text. Verifies filtering, turn merging,
  file/command extraction, `<details>` rendering.
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
- *LLM output*: untrusted text — only ever written into markdown, never
  executed or parsed as commands.
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
