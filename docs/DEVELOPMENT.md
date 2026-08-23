# Development notes — what was built and how

A complete account of how `claude-handoff` v0.1.0 came to be: the schema
reverse-engineering, the design decisions, the bugs hit along the way, and
what was (and wasn't) tested. Written so a contributor — human or AI — can
pick the project up cold.

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

## 3. Design decisions

| Decision | Rationale |
|---|---|
| Single file, stdlib only, Python ≥3.9 | The user's brief: "without extra shits". Also makes it `curl`-able and auditable. |
| Deterministic mode is the default | Zero cost, offline, no API key, reproducible. LLM summarization is opt-in (`--llm`). |
| Parse defensively | Content may be string *or* list; unknown record types are skipped, corrupt lines tolerated (`errors="replace"`, per-line `try/except`). Schema drift is the main long-term risk. |
| Drop, don't keep: tool results, thinking, sidechains, meta | Tool results are bulky and re-derivable; thinking is private reasoning; sidechains are another agent's transcript. The *activity summary* (files touched, commands run) preserves the useful residue. |
| Merge consecutive assistant records into one turn | One user prompt can produce dozens of assistant API-call records interleaved with tool results. A reader wants turns, not records. |
| Head+tail truncation, never head-only | Per message (70/20) and globally (35/60): the opening sets the goal, the recent end is the current state — the middle is the most expendable. |
| LLM calls via raw `urllib` | No SDKs → no dependencies. Three providers (Anthropic, OpenAI, Gemini), keys from env, model overridable with `--model` since default model ids rot quickly. |
| Handoff preamble addressed to the *receiving* assistant | The output must work as a first message with zero extra prompting. |

## 4. Pipeline architecture

`claude_handoff.py`, top to bottom:

1. **Discovery** — `find_sessions()` globs `~/.claude/projects/*/*.jsonl`,
   newest first; `--project` filters by path substring; `--list` prints
   date/size/id/project plus a best-effort first prompt (`last-prompt`
   record, else first clean user message).
2. **Parsing** — `parse_session()` is the heart: single pass over records,
   producing `meta` (session id, cwd, branch, models, timestamps, counts),
   `turns` (role + text parts + tool one-liners), `files_written/read`,
   `commands`, and a `tool_use_id → name` map.
3. **Rendering** — `render_header` (preamble + session facts),
   `render_activity` (files created/modified with edit counts, deduped
   command list), `render_transcript` (🧑/🤖 turns; tools collapsed into
   `<details>` with `--include-tools`, else a `[N tool calls]` marker),
   `render_footer`.
4. **LLM mode** — `build_llm()` feeds activity + full cleaned transcript
   (capped at 400k chars) to `llm_summarize()` with a fixed prompt that
   demands six sections (Goal / Key decisions / Current state / Files &
   artifacts / Next steps / Constraints & preferences), forbids invention,
   and answers in the user's language. `--with-transcript` appends the
   cleaned transcript after the summary.

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
- **LLM path**: exercised with a stubbed `http_json` (no real API calls were
  made — the sandbox has no keys). Live provider calls are **untested**;
  treat the three request payloads as "correct per docs, unverified".
- **Error paths**: missing API key, nonexistent file, empty session — all
  exit with a one-line message, no tracebacks.
- **Packaging**: `pip install -e .` + `claude-handoff --version` +
  fixture run, verified in-container.

## 7. Known limitations (v0.1.0)

- Schema drift is a *when*, not an *if*; parsing is defensive but a future
  format change can silently drop content. The fixture suite is the canary.
- Sidechains (subagent work) are dropped entirely — their outcomes usually
  surface in the main thread, but a `--include-sidechains` flag may be
  warranted.
- Images are reduced to `[image attached]`; compaction summary records are
  not specially handled; token/cost stats (`usage`) are ignored.
- Only two session flavors tested (classic CLI, SDK/Cowork). Hours-long
  sessions with MCP tools and multiple compactions will find edge cases.
- claude.ai web exports (`conversations.json`) not yet accepted as input —
  top roadmap item, and the piece that would subsume the browser-extension
  use case.

## 8. Competitive positioning

See `docs/RESEARCH.md` (gitignored, internal). Short version: exporters
(claude-conversation-extractor, claude-code-log) stop at readable
transcripts; in-session handoff skills (thepushkarp/handoff,
claude-session-handoff) must run before the session ends and target the
next *Claude* session; browser extensions (Handoff, LLM Context Bridge)
cover web chats only; cli-continues moves sessions between terminal
coding CLIs (deterministic only, no web-chat targets). The niche here:
post-hoc + cross-model + paste-anywhere + optional LLM summary.
