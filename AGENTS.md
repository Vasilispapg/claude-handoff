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
  network — via stdlib `urllib` for API providers, or by spawning the local
  `claude` CLI for `--llm claude-cli`. No other subprocesses, no SDKs.
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
- Parsing: `parse_session` (coordinator) + single-responsibility helpers
  `_handle_assistant_record`, `_handle_user_record`, `_handle_tool_use`,
  `_update_envelope_meta`; text utils `user_text`, `tool_result_text`,
  `clean_text`, `tool_summary`
- Rendering: `render_header`, `render_activity`, `render_transcript`,
  `render_footer`, `build_deterministic`
- LLM mode: `PROVIDERS` registry (env keys + default model + call
  strategy per provider), `provider_key`, `_call_claude`, `_call_openai`,
  `_call_gemini`, `_call_claude_cli`, `http_json`, `llm_summarize`,
  `build_llm`, `SUMMARY_PROMPT`
- CLI: `build_arg_parser`, `resolve_source`, `build_document`,
  `write_output`, `main`

**Adding an LLM provider** = one `_call_*` function + one `PROVIDERS`
entry. Do not add if/elif provider chains anywhere (open/closed).

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
- Default LLM model ids in `PROVIDERS` rot; prefer fixing via `--model`
  docs over hardcoding bleeding-edge ids.
- `_call_claude_cli` scrubs `CLAUDE*` env vars (except
  `CLAUDE_CODE_OAUTH_TOKEN`) before spawning `claude -p`, so nested runs
  from inside a Claude Code session authenticate like a fresh CLI. Don't
  remove the scrub.

## When changing the parser

1. Add or extend a fixture in `tests/fixtures/` reproducing the new record
   shape (redact real content).
2. Run the test suite and a smoke run against a real session if available.
3. Update the schema notes in `docs/DEVELOPMENT.md` §2.

## Style

Small pure functions, type hints, no classes unless state truly demands it.
Keep the file readable top-to-bottom: discovery → parsing → rendering →
LLM → CLI.

SOLID, applied the Python way: single-responsibility functions (one record
type / one concern each); open/closed via the `PROVIDERS` registry —
extend by adding entries, never by editing dispatch logic; dependencies
passed in as arguments (`projects_dir`, provider `call` strategies), not
reached for globally. No class hierarchies — first-class functions are the
abstraction.
