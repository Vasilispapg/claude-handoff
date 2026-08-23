# Changelog

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
