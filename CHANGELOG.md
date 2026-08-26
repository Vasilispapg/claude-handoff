# Changelog

## 0.17.0 — 2026-08-26

- **The transcript is a digest by default**: the `## Conversation`
  section now renders one condensed bullet per turn (user ~300 chars,
  assistant ~500, every cut visible as `…`) instead of near-verbatim
  messages — it's a summarize tool. On a real 270 MB, 13-day session
  the conversation coverage went from ~6% of turns (verbatim head+tail)
  to ~43%, and the tail still ends at the current state. `--full`
  restores the classic verbatim transcript; LLM summarization
  (`--llm`, `--brief`) always reads the full text, never the digest.
- **Task-notifications are no longer "the user"**: `<task-notification>`
  records render as one-line `🔔` turns (summary only, entities
  unescaped, payload dropped) and are counted separately — a marathon
  multi-agent session dropped from "333 user messages" to the 94 the
  human actually sent, with "240 background notification(s)" named in
  the header. `<local-command-caveat>` wrappers are stripped as noise.
- **File inventory capped, ranked, and relative**: `## Files created /
  modified` shows the top 15 by edit count (was: every file,
  alphabetically — 546 bullets on that same session, starving the
  transcript budget), with paths relative to the project root the
  header already names (`~`-collapsed when outside it);
  scratchpad/temp files fold into one `…plus N` line; the full
  absolute list stays available with `--format json`.
- **Honest token line**: cache re-reads are split out —
  `39,185,271 in / 7,722,484 out (+3,900,024,478 cached reads)`
  instead of one meaningless 3.9-billion "in (incl. cache)" figure.
- **Header stats a reader can trust**: assistant replies now count
  merged turns (what the document shows), not API records (~3× fewer);
  subagent tool calls appear as `(+N in subagents)` so the command list
  can no longer exceed the tool-call count; `gitBranch: "HEAD"` renders
  as `(detached HEAD)` instead of ``branch `HEAD` ``. Sessions that
  dispatched subagents are never auto-skipped as trivial.
- **Egress heads-up for emails**: when the written document contains
  email addresses (default redaction targets secrets, not identity),
  the CLI prints `ℹ N email address(es) in the output — --anonymize
  strips identity for public sharing`. Informational only.
- **`Fixed` means fixes**: every distill prompt now defines the
  category — only defects actually diagnosed and resolved; releases,
  version bumps, docs, badges, listings and promotion are filed under
  Decisions or Open threads instead (non-fixes like README badges were
  landing under `## Fixed`). Prompt text is part of the note cache key,
  so affected notes re-distill on the next `--llm` run.
- **Freshness notes list the delta, not just a count**: "N newer
  session(s)" now names those sessions (date, id, title) and "N
  commit(s) landed after this distillation" lists sha + subject bullets
  (capped at 6, newest last). The SessionStart injection additionally
  lists commits the brief file has never seen (landed after its last
  rebuild). A bare count once let a fresh session trust distilled open
  threads the repo had already closed; the delta shows what changed.

## 0.16.0 — 2026-08-26

- **Plain `--brief` no longer discards a paid distillation**: rebuilding
  the free skeleton onto the stamped brief file now grafts the existing
  distilled section (with freshness notes re-derived) and carries the
  distillation stamp forward — same behavior the SessionEnd refresh
  always had. An explicit `-o` elsewhere still writes a plain skeleton.
- **Git-aware freshness**: the brief now knows the repo, not just the
  session store. The skeleton carries a `Repo HEAD` line (short hash +
  date + subject of the last commit); the SessionEnd refresh and the
  SessionStart injection warn when commits landed *after* the last
  distillation ("N commit(s) newer than this memory"), so outdated
  memory is never trusted silently. Read straight from
  `.git/logs/HEAD` — no `git` subprocess, keeping the
  deterministic-mode invariant; silently absent outside a repo.
- **Brief layout fix**: the LLM-distilled sections (Decisions / Fixed /
  Conventions / Open threads) now render as `###` sub-headings nested
  under `## Distilled memory` instead of rivaling it. Demoted
  deterministically at assembly — prompts unchanged, so every cached
  session note survives.

## 0.15.0 — 2026-08-26

- **No size limit for project memory**: sessions beyond the per-note
  budget are now map-reduced *inside* the note (chunk notes on turn
  boundaries + one synthesis, all cached) — the brief inherits the
  same nothing-silently-dropped guarantee as `--llm`, at any size.
- **Multi params**: `chf a.jsonl b.jsonl …` merges several sessions
  into one handoff; `--grep X --grep Y` requires ALL terms (AND);
  `--project A --project B` searches several projects together.
- **Docs**: new `docs/GUIDE.md` (day-with-it walkthrough, `--brief`
  step-by-step, honest cost table, cheatsheet); a second demo GIF shows
  project memory end-to-end; brief/`Using latest session` messages print
  ~-collapsed paths.

## 0.14.0 — 2026-08-26

- **Prompt-injection defense**: every LLM prompt that consumes a
  transcript, the handoff preamble, and the brief injection wrapper
  now explicitly frame session content as *data, not instructions* —
  pinned by tests. (Prompts changed → the chunk cache version bumped
  and brief notes re-distill once on next refresh.)
- **PreCompact hooks**: `--install-hook` and `--install-brief-hook`
  also register PreCompact — a handoff snapshot before compaction
  squeezes detail away, and a free brief-skeleton refresh
  mid-session.
- Session paths in messages are now ~-collapsed; the README opens
  with a 5-second vhs demo (`docs/assets/demo.tape` regenerates it).
- **Tolerant, not mute**: hooks now report swallowed errors on stderr
  (still exit 0, stdout untouched); `--grep` says how many unreadable
  files it skipped; new `--debug` flag / `CLAUDE_HANDOFF_DEBUG=1`
  surfaces tolerated failures (corrupt JSONL lines) with file and
  line number.

## 0.13.0 — 2026-08-26

- **Project memory — `--brief`**: distill EVERY session of a project
  into one living document at `~/.claude/briefs/<project>.md` — a
  factual session timeline + most-touched files, and with `--llm` a
  distilled memory (Decisions / Fixed / Conventions / Open threads),
  every bullet citing the session it came from. Per-session notes are
  cached, so refreshing after new sessions costs only the new ones.
- **`--install-brief-hook`**: two hooks — SessionStart injects the
  brief as context (Claude starts already knowing the project);
  SessionEnd auto-refreshes the factual skeleton for free. Hooks
  never trigger LLM calls or create files; a freshness stamp makes
  staleness visible in the file and as an injection warning, and the
  timeline is capped at 20 bullets so injection stays bounded.
- Internals: `_resolve_provider` extracted (shared key/model
  resolution), hook settings editing generalized, single-file build
  now handles multi-line module docstrings.

## 0.12.0 — 2026-08-26

- **`--anonymize`**: strip identity for public sharing — home paths
  collapse to `~`, emails/IPv4s/username become placeholders. Opt-in,
  applied at the same egress seam as redaction; also available as an
  `anonymize` argument on the MCP `handoff` tool.
- **Picker multi-select**: `-i` accepts `1,3` or `2-4` and merges the
  picked sessions into one handoff.
- **`--mcp --allow-llm`**: explicit opt-in that lets MCP clients
  request LLM-written summaries (`llm`/`model`/`focus` tool args);
  without it the server stays strictly deterministic.
- **`--list --format json`**: machine-readable session listing (path,
  id, project, mtime, size, title, prompt, 🔍 match with `--grep`).
- **Config defaults**: `~/.config/claude-handoff/config.json`
  (`CLAUDE_HANDOFF_CONFIG` overrides) feeds allow-listed defaults —
  CLI flags always win; `no_redact` is deliberately not configurable.

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
