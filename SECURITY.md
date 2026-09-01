# Security Policy

`claude-handoff` reads your local Claude Code transcripts and produces a
document you paste somewhere else. Its security surface is therefore mostly
about **what leaves your machine**: redaction, `--llm` egress, and what the
tool writes to disk. A bug in that surface is a security bug, not a cosmetic
one — please report it.

## Supported versions

This is a single-maintainer project. Fixes land on the latest release only;
there are no backports.

| Version  | Supported                                        |
| -------- | ------------------------------------------------ |
| 0.20.x   | ✅                                               |
| < 0.20   | ❌ — upgrade (`pipx upgrade claude-handoff`)     |

If you vendored `single/claude_handoff.py`, re-download it after a fix: the
single-file build is generated from the package and ships the same code.

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.**

Report it privately through GitHub:
[**Security → Report a vulnerability**](https://github.com/Vasilispapg/claude-handoff/security/advisories/new).

Helpful in a report:

- `chf --version`, Python version, OS
- the exact command you ran (redact anything real)
- what happened vs. what you expected — for a leak, *what* reached the output
- a **redacted** minimal fixture that reproduces it, in the shape of
  `tests/fixtures/` (structure preserved, real content replaced)

What to expect: acknowledgement within 3 days, an assessment within 7, and a
fix released as soon as it is ready. You will be credited in the advisory and
`CHANGELOG.md` unless you prefer otherwise.

## In scope

- **Redaction bypass** — a secret-shaped string (API key, token, JWT,
  `Authorization:` header, PEM block, `password=`…) reaching any output:
  `handoff.md`, the memory brief, hook files, MCP replies, clipboard, stdout.
- **`--anonymize` leaking identity** — a home path, email, IPv4 or username
  surviving into the anonymized output.
- **Unexpected network egress.** Deterministic mode makes zero network calls;
  `--llm` talks only to the provider you selected. Anything else is a bug.
- **Writes outside the intended path** — path traversal through session ids,
  project names, `--output` or config values.
- **Command or argument injection** through transcript content, file names,
  or config — including the `--llm claude-cli` / `ollama` subprocess paths.
- **Prompt injection that escapes the framing** — untrusted transcript text
  breaking out of the wrappers that mark it as data, so it reaches the
  receiving assistant (or a `--brief` hook) as instructions.
- **Supply chain** — the PyPI artifacts, or `single/claude_handoff.py`
  drifting from the package it is generated from.

## Out of scope

- **Redaction missing an unrecognizable secret.** It matches known key
  shapes, precision-first — a homegrown secret with no prefix and no
  `key=` context can get through. That is documented, not a vulnerability.
  A *recognizable* pattern that is missed **is** in scope: send it.
- `--no-redact` doing what it says. It is opt-in per run and deliberately
  not settable from the config file.
- What the LLM provider you chose with `--llm` does with the text you sent it.
- Vulnerabilities in Python, in Claude Code, or in your operating system.
- Anything requiring an attacker who already has read access to your
  `~/.claude/projects` transcripts and your shell.
