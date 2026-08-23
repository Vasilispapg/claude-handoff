# INDEX.md — file map

```
claude-handoff/
├── claude_handoff.py      ← the entire tool (single-file CLI, stdlib only):
│                            discovery/picker → parsers (Claude Code JSONL,
│                            claude.ai & ChatGPT exports) → filters/merge →
│                            rendering (md/json) → LLM providers (registry,
│                            map-reduce, cache, redaction) → hook → MCP → CLI
├── README.md              ← what it is, install, usage, flags, providers,
│                            comparison to other tools, roadmap
├── INDEX.md               ← this file
├── AGENTS.md              ← instructions & invariants for AI coding agents
├── CLAUDE.md              ← pointer to AGENTS.md for Claude Code
├── CONTRIBUTING.md        ← how to contribute (fixtures-first workflow)
├── CHANGELOG.md           ← release history (0.1.0 → current)
├── LICENSE                ← MIT
├── pyproject.toml         ← packaging; exposes the `claude-handoff` command
├── .gitignore             ← also excludes docs/RESEARCH.md (internal notes)
├── .github/workflows/
│   ├── ci.yml             ← tests on Linux/macOS/Windows × Python 3.9/3.13
│   └── publish.yml        ← PyPI trusted publishing on GitHub release
├── docs/
│   ├── DEVELOPMENT.md     ← schema notes, architecture, design decisions,
│   │                        zero-trust & failure model, testing, limits
│   └── RESEARCH.md        ← (gitignored) market research & gap analysis
└── tests/
    ├── test_basic.py      ← unittest suite (~70 tests: parsing, filters,
    │                        providers, cache, redaction, merge, MCP, …)
    └── fixtures/
        ├── classic_session.jsonl    ← classic interactive-CLI session
        ├── trivial_session.jsonl    ← nearly-empty stub (auto-skip case)
        ├── compacted_session.jsonl  ← /compact records + usage stats
        ├── claude_web_export.json   ← claude.ai data-export shape
        └── chatgpt_export.json      ← ChatGPT data-export (mapping graph)
```

## Where to start

- **Using it:** `README.md`
- **Understanding it:** `docs/DEVELOPMENT.md` §2 (schemas) and §4 (pipeline)
- **Changing it:** `AGENTS.md` (invariants + gotchas), then `tests/`
- **Why it exists at all:** `docs/RESEARCH.md` (local only)

## Related repos

- [`Vasilispapg/homebrew-tap`](https://github.com/Vasilispapg/homebrew-tap) —
  Homebrew formula (`brew install Vasilispapg/tap/claude-handoff`)
