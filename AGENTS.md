# AGENTS.md — instructions for AI coding agents

## What this project is

`claude-handoff` turns a Claude Code session transcript (JSONL in
`~/.claude/projects`) into a single clean `handoff.md` that another LLM
(Gemini, GPT, another Claude) can continue from. One Python file, stdlib
only. Read `docs/DEVELOPMENT.md` for the full architecture and the JSONL
schema notes before touching the parser.

## Hard invariants — do not break these

- **Zero runtime dependencies.** Stdlib only. Do not add anything to
  `pyproject.toml` dependencies; do not import third-party packages.
- **Single module.** All runtime code lives in `claude_handoff.py`.
- **Python ≥ 3.9** compatibility (no `match`, no 3.10+ typing syntax outside
  `from __future__ import annotations`).
- **Parse defensively.** The JSONL schema is undocumented and drifts across
  Claude Code versions. Unknown record types are skipped, never fatal;
  content may be a string or a block list; corrupt lines are tolerated.
- **Deterministic mode makes no network calls.** Only `--llm` may touch the
  network, and only via stdlib `urllib`.
- **Never invent transcript content.** The exporter reorganizes and
  truncates; it must not fabricate.

## Commands

```bash
python3 -m unittest discover -s tests -v      # run tests
python3 claude_handoff.py tests/fixtures/classic_session.jsonl -o -   # smoke run
pip install -e . && claude-handoff --version  # packaging check
```

## Architecture map (claude_handoff.py)

- Discovery: `find_sessions`, `first_prompt_of`, `list_sessions`
- Parsing: `parse_session` (the heart), `user_text`, `tool_result_text`,
  `clean_text`, `tool_summary`
- Rendering: `render_header`, `render_activity`, `render_transcript`,
  `render_footer`, `build_deterministic`
- LLM mode: `SUMMARY_PROMPT`, `http_json`, `llm_summarize`, `build_llm`
- CLI: `main`

## Gotchas that will bite you

- **SDK/Cowork sessions**: assistant prose arrives as `SendUserMessage`
  *tool calls* (not text blocks), and the human's answers arrive inside
  `AskUserQuestion` *tool results*. `parse_session` recovers both via the
  `tool_use_id → name` map. Don't "simplify" this away.
- `isSidechain: true` records are subagent transcripts — dropped by design.
- `isMeta: true` user records and `<system-reminder>` / slash-command
  envelopes are not human input — filtered by `NOISE_RE` / `CAVEAT_RE`.
- Truncation is head+tail everywhere (the middle is the expendable part);
  keep it that way.
- Default LLM model ids in `DEFAULT_MODELS` rot; prefer fixing via `--model`
  docs over hardcoding bleeding-edge ids.

## When changing the parser

1. Add or extend a fixture in `tests/fixtures/` reproducing the new record
   shape (redact real content).
2. Run the test suite and a smoke run against a real session if available.
3. Update the schema notes in `docs/DEVELOPMENT.md` §2.

## Style

Small pure functions, type hints, no classes unless state truly demands it.
Keep the file readable top-to-bottom: discovery → parsing → rendering →
LLM → CLI.
