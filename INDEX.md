# INDEX.md — file map

```
claude-handoff/
├── claude_handoff/        ← the tool as a package (stdlib only, no cycles):
│   ├── textutil.py        ← text cleaning, truncation, tiny formatters
│   ├── redact.py          ← secret redaction (LLM egress + final documents)
│   ├── parse.py           ← JSONL records → turns/meta/activity/sidechains
│   ├── webexport.py       ← claude.ai & ChatGPT conversations.json input
│   ├── discovery.py       ← session globbing, labels, --name/--grep, picker
│   ├── render.py          ← markdown sections + --format json
│   ├── llm.py             ← provider registry, map-reduce, cache, progress
│   ├── brief.py           ← --brief: whole-project memory (timeline + LLM
│   │                        distillation, cached per session)
│   ├── integrations.py    ← clipboard, graphify corpus, MCP server, hooks
│   ├── cli.py             ← argparse, source resolution, main
│   └── _version.py / __init__.py / __main__.py
├── single/
│   └── claude_handoff.py  ← GENERATED single-file build for curl installs
│                            (scripts/build_single.py; CI keeps it fresh)
├── scripts/
│   └── build_single.py    ← stitches the package into single/…, --check mode
├── skills/
│   └── claude-handoff/
│       └── SKILL.md       ← the /claude-handoff Claude Code skill
│                            (--install-skill; embedded as SKILL_MD in
│                            integrations.py, pinned byte-identical)
├── README.md              ← 60-second scenarios, install, project memory,
│                            recipes, privacy, troubleshooting, full flag
│                            reference, comparison, roadmap
├── INDEX.md               ← this file
├── AGENTS.md              ← agent onboarding: green-in-4-commands, invariants,
│                            architecture map, workflow & release checklists
├── CLAUDE.md              ← pointer to AGENTS.md for Claude Code
├── CONTRIBUTING.md        ← how to contribute (fixtures-first workflow)
├── CHANGELOG.md           ← release history (0.1.0 → current)
├── LICENSE                ← MIT
├── pyproject.toml         ← packaging (`claude-handoff` + `chf` commands),
│                            ruff lint config
├── glama.json             ← Glama MCP directory metadata (maintainers)
├── .gitignore             ← also excludes docs/RESEARCH.md (internal notes)
├── .github/workflows/
│   ├── ci.yml             ← tests on Linux/macOS/Windows × Python 3.9/3.13
│   └── publish.yml        ← PyPI trusted publishing on GitHub release
├── docs/
│   ├── GUIDE.md           ← a day with the tool: walkthrough, --brief
│   │                        step-by-step, honest cost table, cheatsheet
│   ├── DEVELOPMENT.md     ← schema notes, architecture, design decisions,
│   │                        zero-trust & failure model, testing, limits
│   └── RESEARCH.md        ← (gitignored) market research & gap analysis
└── tests/
    ├── test_basic.py      ← unittest suite (~100 tests: parsing, filters,
    │                        agents, grep/fit, providers, cache, redaction,
    │                        merge, MCP, …)
    └── fixtures/
        ├── classic_session.jsonl    ← classic interactive-CLI session
        ├── agent_session.jsonl (+ agent_session/subagents/) ← multi-agent
        ├── secret_session.jsonl     ← secret-bearing session (redaction)
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
