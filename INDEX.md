# INDEX.md — file map

```
claude-handoff/
├── claude_handoff.py      ← the entire tool (single-file CLI, stdlib only)
├── README.md              ← what it is, install, usage, flags, roadmap
├── INDEX.md               ← this file
├── AGENTS.md              ← instructions & invariants for AI coding agents
├── CLAUDE.md              ← pointer to AGENTS.md for Claude Code
├── CONTRIBUTING.md        ← how to contribute (fixtures-first workflow)
├── CHANGELOG.md           ← release history
├── LICENSE                ← MIT
├── pyproject.toml         ← packaging; exposes the `claude-handoff` command
├── .gitignore             ← also excludes docs/RESEARCH.md (internal notes)
├── docs/
│   ├── DEVELOPMENT.md     ← full build analysis: schema notes, architecture,
│   │                        design decisions, bugs found, testing, limits
│   └── RESEARCH.md        ← (gitignored) market research: every related
│                            exporter/handoff tool found, gap analysis
└── tests/
    ├── test_basic.py      ← unittest suite (parsing, filtering, rendering)
    └── fixtures/
        └── classic_session.jsonl  ← synthetic classic-CLI-format session
```

## Where to start

- **Using it:** `README.md`
- **Understanding it:** `docs/DEVELOPMENT.md` §2 (schema) and §4 (pipeline)
- **Changing it:** `AGENTS.md` (invariants + gotchas), then `tests/`
- **Why it exists at all:** `docs/RESEARCH.md` (local only)
