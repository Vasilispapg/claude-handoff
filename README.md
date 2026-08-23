# claude-handoff

**Summarize & export a Claude Code session into one clean `handoff.md` you can paste into Gemini, GPT, or another Claude — without the noise.**

Claude Code stores every session locally as JSONL (`~/.claude/projects/…/*.jsonl`), full of tool calls, tool results, thinking blocks and system reminders. Existing exporters dump all of that into markdown. `claude-handoff` instead produces a **handoff document**: the actual conversation, what files were touched, what commands ran, and (optionally) an LLM-written summary of goal / decisions / current state / next steps — so the next model can just continue the work.

- **Zero dependencies.** One Python file, stdlib only. Python 3.9+.
- **Deterministic by default.** No API call, no cost, works offline.
- **`--llm` when you want a real summary.** Claude, OpenAI or Gemini via your own API key.
- **Noise-free.** Drops tool results, thinking blocks, system reminders, subagent chatter, slash-command envelopes. Keeps user intent, assistant answers, files modified, commands run.

## Install

```bash
pipx install git+https://github.com/Vasilispapg/claude-handoff
# or just grab the file — it's a single stdlib-only script:
curl -O https://raw.githubusercontent.com/Vasilispapg/claude-handoff/main/claude_handoff.py
python3 claude_handoff.py --list
```

## Usage

```bash
claude-handoff                     # latest session → handoff.md
claude-handoff --list              # what sessions do I have?
claude-handoff --project myrepo    # latest session of a specific project
claude-handoff path/to/session.jsonl -o -     # explicit file → stdout
claude-handoff --include-tools     # keep collapsed per-tool-call detail

# real LLM summary (goal / decisions / current state / next steps):
export ANTHROPIC_API_KEY=sk-...
claude-handoff --llm claude
claude-handoff --llm openai --model gpt-4o
claude-handoff --llm gemini --with-transcript   # summary + cleaned transcript
```

Then paste `handoff.md` into any other model. The document opens with instructions to the receiving assistant, so no extra prompting is needed.

## What the output looks like

```markdown
# Conversation handoff

> To the receiving assistant: … you are taking over …

## Session
- Project: /home/you/myapp (branch main)
- When: 2026-08-20 09:00 → 09:04
- Activity: 2 user messages, 4 assistant replies, 4 tool calls

## Files created / modified
- /home/you/myapp/auth.py

## Commands run
- python -m pytest tests/test_auth.py -q

## Conversation
### 🧑 User
the login breaks on unicode passwords…
### 🤖 Assistant
Found it — ascii encoding. Changed to utf-8, tests pass.
```

## Flags

| Flag | Meaning |
|---|---|
| `--list` | list sessions (date, size, project, first prompt) |
| `--project NAME` | pick latest session whose project path contains NAME |
| `-o FILE` / `-o -` | output file / stdout (default `handoff.md`) |
| `--include-tools` | collapsed `<details>` blocks with each tool call |
| `--max-chars N` | cap the transcript section (default 80 000; keeps start + recent end) |
| `--llm claude\|openai\|gemini` | LLM summary instead of raw cleaned transcript |
| `--model ID` | override the LLM model |
| `--with-transcript` | with `--llm`, also append the cleaned transcript |

API keys are read from `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`. Nothing is sent anywhere unless you pass `--llm`.

## Roadmap

- claude.ai web chat exports (`conversations.json` from the official data export) as input
- ChatGPT / Gemini exports as input (handoff in *both* directions)
- `--format json` for programmatic use

PRs welcome.

## How it compares

This space isn't empty — it's fragmented. Pick the tool that matches your situation:

- **Exporters** — [claude-conversation-extractor](https://github.com/ZeroSumQuant/claude-conversation-extractor), [claude-code-log](https://github.com/daaain/claude-code-log), [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts), [claude-to-markdown](https://github.com/legoktm/claude-to-markdown) — turn transcripts into readable Markdown/HTML, tool noise included, no handoff framing.
- **Cross-CLI session movers** — [cli-continues](https://github.com/yigitkonur/cli-continues) (`npm i -g continues`) reads 16 coding CLIs' native session stores (Claude Code included) and injects a context doc into another *terminal* tool. Excellent for Claude Code → Codex/Cursor/Gemini CLI; but it can't target web chats, does no LLM summarization, and needs Node 22.5+.
- **In-session handoff skills/plugins** — [thepushkarp/handoff](https://github.com/thepushkarp/handoff), [claude-session-handoff](https://github.com/thenguyenvn90/claude-session-handoff), [claude-code-handoff](https://github.com/Sonovore/claude-code-handoff) — great *if* you remember to run them before the session ends; the model writes the summary using your session's context, and the output targets the next *Claude* session.
- **Browser extensions** — Handoff, LLM Context Bridge, ContextSwitch — transfer *web* chats between ChatGPT/Claude/Gemini; they can't see Claude Code sessions.

`claude-handoff` is the post-hoc, paste-anywhere corner of this map: it works on the JSONL *after* the fact — old sessions, crashed sessions, sessions that hit the usage limit — needs nothing installed in advance, costs zero tokens by default, can write a real summary when you ask for one (`--llm`), and produces a document any receiving model can pick up, including claude.ai, ChatGPT and Gemini in the browser or on your phone.

## Docs

[INDEX.md](INDEX.md) — file map · [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — architecture, JSONL schema notes, design decisions · [AGENTS.md](AGENTS.md) — instructions for AI coding agents · [CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md)

## License

MIT
