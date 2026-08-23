# Changelog

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
