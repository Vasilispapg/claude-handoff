# Contributing

Thanks for helping! Ground rules, in order of importance:

**Zero dependencies is the product.** PRs adding runtime dependencies will be
declined — the whole point is one auditable stdlib-only file you can `curl`.
Dev-only tooling is fine as long as `claude_handoff.py` stays standalone.

**Fixtures first.** The Claude Code JSONL schema is undocumented and drifts
between versions. If the parser breaks on your session: strip the session
down to the few offending lines, **redact all real content** (replace text
with placeholders, keep the structure), drop it in `tests/fixtures/`, and
open an issue or PR with it. A failing fixture is worth more than a
description.

**Run the tests.**

```bash
python3 -m unittest discover -s tests -v
python3 claude_handoff.py tests/fixtures/classic_session.jsonl -o - --include-tools
```

CI runs the suite on Linux, macOS and Windows (Python 3.9 and 3.13) —
keep changes cross-platform (explicit `encoding="utf-8"` everywhere, no
shell-specific tricks).

**Scope.** Good PR targets: new session flavors (fixtures + parser fixes),
Gemini Takeout input (bring a real, redacted export!), new `--llm`
providers (one `_call_*` function + one `PROVIDERS` entry), MCP server
extensions, provider fixes. Before building something large, open an
issue first.

**Style.** Match the existing code: small pure functions, type hints,
readable top-to-bottom. Python ≥ 3.9. See `AGENTS.md` for the invariants —
they apply to humans too.
