# A day with claude-handoff

A practical walkthrough — what the tool actually does for you across a
working day, plus an honest look under the hood of `--brief`. Assumes:

```bash
pipx install claude-handoff        # gives you claude-handoff + chf
chf --install-brief-hook           # one-time, per machine
chf --brief --llm claude-cli       # one-time per project: build the memory
```

---

## 09:00 — start work: Claude already knows the project

You open a new Claude Code session in your project. Before you type
anything, the SessionStart hook has already injected the project brief as
context:

```
<project-memory source="claude-handoff" refresh="chf --brief">
# Project brief: /home/you/shop-api
...
## Distilled memory
### Decisions
- JWT lives in httpOnly cookies, not localStorage — smaller XSS surface [a1b2c3d4]
...
</project-memory>
```

So when you say *"continue the rate limiter work"*, Claude already knows
what the limiter is, why it moved to Redis, and that refresh-token
rotation is still open — without you explaining anything and without it
burning twenty tool calls rediscovering the codebase.

If sessions newer than the brief exist, the injection carries a warning
(`sessions newer than this brief exist — refresh with chf --brief`), so
neither you nor the model trusts stale memory silently.

## 11:30 — the session dies mid-debugging

Usage limit. Crash. Closed laptop. Doesn't matter — the transcript is on
disk, and nothing needed to be installed *before* the accident:

```bash
chf -o clipboard
```

```
Using latest session: ~/.claude/projects/-home-you-shop-api/e5f6a7b8.jsonl
Copied to clipboard via pbcopy (48,112 chars, ≈12.0k tokens, 14 user messages) — paste away.
```

Paste into claude.ai, ChatGPT, or Gemini and keep working. The document
opens with instructions to the receiving assistant, so no extra prompting.
Moving to a small-context model? Size it first:

```bash
chf --fit 32k -o clipboard
```

Truncation is head+tail — the goal (start) and the current state (end)
survive; the noisy middle is the expendable part. For a *complete* account
instead, ask for the LLM summary (`chf --llm claude-cli`), which
map-reduces the whole transcript and drops nothing.

## 14:00 — "where did we decide that?"

You remember deciding something about CORS weeks ago. Which session?

```bash
chf --list --grep "CORS"                 # every match, 🔍 preview each
chf --grep "CORS" --grep "preflight"     # must contain BOTH (AND)
chf --grep "CORS" -o -                   # export the newest match now
```

Search covers what was actually *said* (user + assistant turns — tool
noise doesn't count) across your entire store in ~half a second. And every
bullet in your brief carries a citation like `[a1b2c3d4]` — open the
source session any time:

```bash
chf --name a1b2c3d4 -o -
```

## 16:00 — filing a public bug report

You want to paste a handoff into a GitHub issue. Secrets (API keys,
tokens, `password=`…) are already redacted from every output by default.
For *public* sharing, go further:

```bash
chf --anonymize -o -
```

Home paths collapse to `~`, emails/IPs/your username become placeholders.
What leaves your machine is your explicit choice, always.

## 18:00 — end of day: nothing to do

The SessionEnd hook refreshes the factual side of the brief (timeline,
touched files) for free, and PreCompact snapshots run before Claude Code
squeezes a long session's context. Hooks never call an LLM and never
create files on their own — cost is always an explicit decision you make.

## Friday — refresh the distilled memory

```bash
chf --brief --llm claude-cli
```

```
brief note 4/5 (5db308c0)      ← cache hit, instant
brief note 5/5 (b101c829)      ← the only session that changed
Wrote brief ~/.claude/briefs/-home-you-shop-api.md (5 sessions, ≈1.7k tokens)
```

Per-session notes are cached by content — you pay only for sessions that
are new or grew, plus one synthesis pass.

---

## Under the hood: how `--brief` works, step by step

**Every size:**

1. Find the project (cwd or `--project X`), collect ALL its sessions,
   parse them streaming (low memory), drop trivial stubs, sort by time.
2. Build the deterministic skeleton — free, always: title (real project
   path), session timeline (capped at the 20 most recent), most-touched
   files across every session, subagent work included.

**With `--llm` — one note per session (map):**

```
small session (≤120k chars ≈ 30k tokens — ~90% of sessions)
   └─► 1 call → note (≤200 words, every bullet cited [sid])

monster session (a 2M-char marathon)
   ├─► split into ~17 contiguous chunks on turn boundaries — ALL of them,
   │   not head/tail, not samples: full coverage
   ├─► 1 mini-note per chunk (≤150 words, cited)
   └─► 1 synthesis → a single session note
```

Full coverage is deliberate: the key decision of a marathon often lives at
a random point of the middle. Sampling would save a few one-off calls in
exchange for a permanent blind spot — and since every note is cached
(keyed on prompt+content), the full read is paid **once per session,
ever**.

**Reduce:** all session notes (chronological — on conflicts the later
session wins) → one `## Distilled memory` of ≤600 words, citations kept,
in your language. `--focus` applies only here, so cached notes stay
reusable across different focuses.

**Then:** redaction → freshness stamp (session count, newest mtime,
distillation age) → written to `~/.claude/briefs/<project>.md`.

## What it costs, honestly

| Action | Calls | Notes |
|---|---|---|
| `chf` / `--fit` / `--grep` / skeleton `--brief` | 0 | Deterministic — free, offline, forever |
| First `--brief --llm` on a project | ~1/session + 1 | e.g. 5 sessions → 6 calls (measured) |
| …if one session is a 2M monster | +~17 for that session | Once ever — cached afterwards |
| Refresh after new work | only the delta + 1 | e.g. 1 new session → 2 calls |
| Brief injection at session start | 0 calls | ~1.7k tokens of context |
| Hooks (all of them) | 0, always | Hard rule — no silent spending |

With `--llm claude-cli` the calls bill your existing Pro/Max subscription
(no API key); with `--llm ollama` they are free and fully offline. And the
counterfactual isn't free either: without the brief, every session pays
for re-explaining or for the model's tool-call archaeology — every time.

## The memory hierarchy

The brief is *not* a lossy replacement of your history — it's the top of
a hierarchy where nothing is more than one step away:

| Layer | Question it answers | Cost |
|---|---|---|
| **Brief** (~1.7k tokens, always present) | "What do we know?" — decisions, why, conventions | ~zero per session |
| **`--grep`** | "Where exactly did we say that?" | 0.5s, free |
| **`chf --name <sid>`** | The full ground truth behind any citation | free |

## Cheatsheet

```bash
chf                                  # latest session → handoff.md
chf -o clipboard                     # …straight to the clipboard
chf --fit 32k -o clipboard           # sized for a smaller context window
chf --llm claude-cli                 # real summary, nothing dropped, no API key
chf a.jsonl b.jsonl                  # merge specific sessions into one
chf -i                               # picker; "1,3" or "2-4" merges
chf --grep "CORS" --grep "auth"      # find by content (AND)
chf --anonymize -o -                 # public-safe output
chf --brief                          # free factual memory
chf --brief --llm claude-cli         # distilled memory (cached, cited)
chf --install-brief-hook             # inject memory into every session
chf --install-hook                   # auto-handoff on session end
chf --debug ...                      # show every tolerated failure
```
