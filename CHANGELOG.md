# Changelog

## 0.11.0 — 2026-08-25

- **Package split**: the single 2100-line module became the
  `claude_handoff/` package — nine single-responsibility modules with
  acyclic imports (`textutil`, `redact`, `parse`, `webexport`,
  `discovery`, `render`, `llm`, `integrations`, `cli`). Proven
  mechanical: outputs byte-identical before/after on all fixtures and
  a real multi-agent session.
- **curl still works**: `scripts/build_single.py` stitches the package
  into the generated `single/claude_handoff.py`; CI fails if it goes
  stale or drifts behaviorally (`--check`).
- **Performance**: `load_records` streams (peak RSS on a 20 MB
  session: 80 MB → 26 MB); `--grep` prefilters with a raw-text
  superset scan in binary mode when JSON escaping cannot hide a
  match (worst-case store-wide search 3.3 s → 0.5 s).
- **Lint**: ruff config in `pyproject.toml` (E/F/I/PLW/RUF), codebase
  clean; new CI lint job.

## 0.10.0 — 2026-08-25

- **Output redaction by default**: secret-looking strings are now stripped
  from every final document (CLI markdown/JSON, `--install-hook` files,
  MCP replies) — the pasted handoff is egress too, not just `--llm`
  traffic. `--no-redact` opts out on the CLI; hook and MCP always redact.
- **`--grep TEXT`**: find sessions by *conversation content* (user +
  assistant turns; tool noise excluded). Alone it picks the newest match;
  with `--list`/`-i` every match shows a 🔍 context preview.
- **`--fit TOKENS`**: size the deterministic handoff to a token budget
  (`32k`, `128k`, `1m`) — fixed sections are measured, the transcript cap
  absorbs the rest. Every write now reports a ≈token estimate.
- **Subagent lane labels**: groups are titled by the spawning
  `Agent`/`Task` call's `description` (linked via `agentId`), with the
  task prompt as a "Task:" line; mid-run parent steering messages
  interleave as "🧭 Parent: …".

## 0.9.0 — 2026-08-25

- **Multi-agent sessions**: separate-file subagent transcripts
  (`<session-id>/subagents/agent-*.jsonl`, newer Claude Code) are now
  discovered and parsed. Their file edits and commands always merge into
  the activity summary (marked with a 🤖 count line — previously a
  multi-agent session's handoff showed almost no activity); full agent
  transcripts render with `--include-sidechains`, ordered by start time.
  `meta.n_agents` lands in `--format json` too.
- **`chf`**: short console alias for `claude-handoff` (both installed;
  shell completions cover both).

## 0.8.1 — 2026-08-23

- MCP Registry metadata: `server.json` for
  registry.modelcontextprotocol.io and the `mcp-name` ownership marker in
  the README. No code changes.

## 0.8.0 — 2026-08-23

- **`-i` interactive picker**: choose the session from a numbered list.
- **`--mcp`**: minimal MCP server over stdio (newline-delimited JSON-RPC)
  with tools `list_sessions` and `handoff` — any MCP client can pull
  deterministic handoffs; no implicit LLM calls.
- **`--completions bash|zsh`**: tab-completion snippet generated from the
  live argument parser.
- **CI matrix**: Linux + macOS + Windows × Python 3.9/3.13
  (`PYTHONUTF8=1`).
- **Homebrew**: `brew install Vasilispapg/tap/claude-handoff`.
- Docs refreshed across the board (INDEX, DEVELOPMENT pipeline & limits,
  CONTRIBUTING, module docstring) to match the current tool.

## 0.7.0 — 2026-08-23

- **ChatGPT exports as input**: the OpenAI data-export
  `conversations.json` (mapping graph) parses like any session — the
  canonical thread is followed via `current_node`, system/tool records
  skipped. Same `--list` / `--name` flow as claude.ai exports.
- **`--merge`**: every session in scope (project / cwd / `--name` match)
  becomes ONE chronological handoff with ⏱ session-break markers, merged
  activity and summed token counts (25 most recent max, nearly-empty
  sessions skipped).
- **`--format json`**: machine-readable handoff (meta, activity, turns,
  sidechains, optional LLM summary).
- **Token stats**: sessions with API `usage` data get a
  `**Tokens:** X in (incl. cache) / Y out` header line.
- **Parallel map-reduce**: chunk summaries run 4-way concurrent on API
  providers; `claude-cli`/`ollama` stay sequential (subprocess/local-box
  constraints). Failures still land in the cache-resume path.

## 0.6.1 — 2026-08-23

- **/compact handled properly**: the machine-written history summary that
  `/compact` leaves behind (an `isCompactSummary` "user" record) is now
  rendered as "📜 Compacted history" instead of masquerading as a giant
  🧑 User message, and no longer counts toward user-turn totals or
  `--last N`. Verified against a real 264MB compacted session.

## 0.6.0 — 2026-08-23

- **claude.ai web chats as input**: point it at the `conversations.json`
  from the official data export (Settings → Privacy → Export data);
  `--list` lists the chats, `--name` picks one, newest by default.
- **`-o clipboard`**: copy the handoff straight to the clipboard
  (pbcopy / wl-copy / xclip / clip).
- **`--last N` and `--since 2h|45m|1d|ISO`**: export only the tail of a
  conversation, with an honest "showing the last X of Y user turns" note.
- **Auto-handoff hook**: `--install-hook` adds a Claude Code SessionEnd
  hook that writes a deterministic handoff to `~/.claude/handoffs/` when
  each session ends (nearly-empty sessions skipped; never breaks the host
  session). `--uninstall-hook` removes it; existing settings preserved.
- **`--llm ollama`**: summaries via a local Ollama server — fully
  offline, nothing leaves the machine. `OLLAMA_MODEL`, `OLLAMA_BASE_URL`.
- **`--include-sidechains`**: append subagent (sidechain) work as its own
  section instead of dropping it.

## 0.5.0 — 2026-08-23

- **Live progress display** for `--llm` runs in a terminal: progress bar,
  chunks done/total, elapsed time, ETA (from the average chunk duration),
  and what is being summarized right now (with its size). Falls back to
  plain lines when stderr is piped (CI, logs). stdout stays clean for
  `-o -`. The reduce pass now also gets the per-chunk retry.

## 0.4.0 — 2026-08-23

- **Directory-aware defaults**: run from inside a project (or a subfolder)
  and "latest session" means *that project's* latest; run from a parent
  "master folder" and it covers every project under it; `--any` ignores
  the directory. Explicit `--project`/`--name`/paths always win.
- **`--focus TEXT`**: extra instructions for the LLM summary ("emphasize
  the API decisions", "answer in English", …). Applied to the synthesis
  pass; never truncated away.
- **Huge sessions (1M+ tokens)**: map-reduce summarization — chunk notes
  on turn boundaries, then one synthesis. Nothing silently dropped.
- **Chunk-note cache** (`~/.cache/claude-handoff`, content-addressed):
  interrupted or repeated runs reuse paid-for chunk notes; `--no-cache`
  to disable. One retry per chunk on provider errors.
- **Secret redaction before egress** (zero-trust): API-key/token/password
  shapes are stripped from transcripts before any `--llm` call;
  `--no-redact` to opt out. Deterministic mode never sends anything.

## 0.3.1 — 2026-08-23

- **Auto-selection skips nearly-empty sessions.** Running `claude /login`
  (or typing a stray line into a chat) leaves a stub session behind that
  used to become the "latest session" — so a summary run right after
  logging in summarized nothing. Auto-pick now skips sessions with ≤2
  turns and under 600 chars, with a stderr note. An explicit path,
  `--name`, or fixture choice is never skipped.

## 0.3.0 — 2026-08-23

- **Pick sessions by name**: `--name QUERY` (or just
  `claude-handoff "login bug"`) selects the newest session whose title,
  first prompt, or file name contains QUERY, case-insensitive. Ambiguous
  matches say how many matched and which one was used.
- **`--list` shows session titles**: Claude Code's own session title
  (when present) next to the first prompt — `Title · first prompt`.
- **Helpful errors**: wrong flags and missing sessions now point to
  `--help` and `--list` instead of a bare error; `--help` gained an
  examples section.

## 0.2.0 — 2026-08-23

- **`--llm claude-cli`**: real LLM summaries through the locally-installed
  Claude Code CLI — billed to your Pro/Max plan, **no API key needed**.
  Scrubs inherited `CLAUDE*` session env vars so it also works when
  claude-handoff is invoked from inside a Claude Code session.
- **API-key aliases** (graphify-style, first set var wins):
  `ANTHROPIC_API_KEY`/`CLAUDE_API`, `OPENAI_API_KEY`/`GPT_API`,
  `GEMINI_API_KEY`/`GOOGLE_API_KEY`/`GEMINI_API`.
- **Maintainability pass for the public repo** (SOLID, Python-idiomatic):
  provider registry (`PROVIDERS`) — adding an LLM provider is one function
  plus one table entry; `parse_session` and `main` split into
  single-responsibility helpers. Deterministic output verified
  byte-identical to v0.1.0 on real sessions.
- Better `--llm` errors: missing-key messages name every accepted env var;
  claude-cli failures surface the CLI's own error message.

## 0.1.0 — 2026-08-23

Initial release.

- Deterministic export of Claude Code JSONL sessions to a single `handoff.md`
  addressed to the receiving assistant.
- Session auto-discovery (`~/.claude/projects`), `--list`, `--project`.
- Noise filtering: tool results, thinking blocks, system reminders,
  slash-command envelopes, meta messages, subagent sidechains.
- Activity summary: files created/modified (with edit counts), commands run.
- SDK/Cowork session support: recovers assistant prose from
  `SendUserMessage` tool calls and user answers from `AskUserQuestion`
  tool results.
- Head+tail truncation per message and globally (`--max-chars`).
- Optional LLM summarization (`--llm claude|openai|gemini`, `--model`,
  `--with-transcript`) via stdlib `urllib` — no SDKs. **Note:** live
  provider calls are untested as of this release; the request payloads
  follow each provider's documented API.
- `--include-tools` collapsed per-call detail.
- Packaging: `pipx install claude-handoff` → `claude-handoff` command.
