## What & why

<!-- One or two sentences. Link the issue if there is one: Fixes #123 -->

## How to verify

<!-- The command a reviewer runs to see it work. A failing-then-passing test is best. -->

```bash
```

## Checklist

- [ ] **Zero runtime dependencies** — no third-party imports, nothing added to
      `[project].dependencies`
- [ ] Tests pass: `python3 -m unittest discover -s tests -v`
- [ ] Smoke run: `python3 -m claude_handoff tests/fixtures/agent_session.jsonl -o -`
- [ ] Single-file build is fresh: `python3 scripts/build_single.py --check`
      (run `python3 scripts/build_single.py` after any package change)
- [ ] Lint: `uvx ruff check claude_handoff scripts tests`
- [ ] Cross-platform — explicit `encoding="utf-8"`, no shell-specific tricks,
      Python 3.9 compatible
- [ ] New session flavor? A **redacted** fixture is in `tests/fixtures/`
- [ ] User-visible change? `README.md` / `docs/` and `CHANGELOG.md` updated
- [ ] Parser change? Read `docs/DEVELOPMENT.md` §2 first

<!-- See CONTRIBUTING.md for the ground rules and AGENTS.md for the invariants. -->
