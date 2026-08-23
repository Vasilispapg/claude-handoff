#!/usr/bin/env python3
"""claude-handoff — summarize & export a Claude Code session for another LLM.

Reads Claude Code's local session transcripts (JSONL in ~/.claude/projects),
strips the tool-call noise, and produces a single clean handoff.md you can
paste into Gemini, GPT, another Claude — anything — so it can pick up where
the session left off.

Zero dependencies. Python 3.9+. MIT license.

Usage:
    claude-handoff                      # latest session -> handoff.md
    claude-handoff --list               # list available sessions
    claude-handoff --project myrepo     # latest session of a project
    claude-handoff path/to/session.jsonl -o -          # explicit file -> stdout
    claude-handoff --llm claude         # real LLM summary (needs API key in env)

API keys (only needed with --llm claude|openai|gemini):
    ANTHROPIC_API_KEY (or CLAUDE_API) | OPENAI_API_KEY (or GPT_API)
    GEMINI_API_KEY (or GOOGLE_API_KEY / GEMINI_API)
No key needed for --llm claude-cli — it uses your local Claude Code login.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.6.0"

PROJECTS_DIR = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")) / "projects"

# Message-level caps (deterministic mode). Head+tail are kept when truncating.
USER_MSG_CAP = 8000
ASSISTANT_MSG_CAP = 5000
TOOL_LINE_CAP = 200
DEFAULT_MAX_CHARS = 80_000       # global cap on the transcript section
LLM_INPUT_CAP = 400_000          # max transcript chars for a single LLM pass
CHUNK_CAP = 200_000              # chunk size for map-reduce over huge sessions

# Chunk-note cache: failed/interrupted map-reduce runs resume for free, and
# re-runs (e.g. with a different --focus) reuse paid-for chunk notes.
CACHE_DIR = Path(os.environ.get(
    "CLAUDE_HANDOFF_CACHE", str(Path.home() / ".cache" / "claude-handoff")))
CACHE_VERSION = "1"              # bump when CHUNK_PROMPT changes

# Secret-shaped strings are stripped from transcripts before any --llm call
# (zero-trust: session logs routinely contain keys pasted into commands).
SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),                    # OpenAI/Anthropic
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),             # Slack
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),                       # AWS key id
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),                   # Google
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9._-]{20,}"),  # JWT
    re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/-]{20,}=*"),
    re.compile(r"((?:api[_-]?key|access[_-]?token|secret|password|passwd"
               r"|authorization)\s*[=:]\s*[\"']?)[^\s\"'\[]{8,}",
               re.IGNORECASE),
]

# LLM provider registry for --llm — see the "LLM summarization" section.
# Adding a provider = one entry here + one _call_* function; nothing else
# changes (open/closed). Populated after the call functions are defined.
PROVIDERS: dict = {}

FILE_TOOLS_WRITE = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
FILE_TOOLS_READ = {"Read"}

NOISE_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<command-name>.*?</command-name>"
    r"|<command-message>.*?</command-message>"
    r"|<command-args>.*?</command-args>"
    r"|<local-command-stdout>.*?</local-command-stdout>"
    r"|<local-command-stderr>.*?</local-command-stderr>",
    re.DOTALL,
)
CAVEAT_RE = re.compile(r"^Caveat: The messages below were generated.*?$", re.MULTILINE)


# --------------------------------------------------------------------------- #
#  Session discovery
# --------------------------------------------------------------------------- #

def find_sessions(project_filter: str | None = None,
                  projects_dir: Path | None = None) -> list[Path]:
    """All session JSONL files, newest first."""
    projects_dir = projects_dir or PROJECTS_DIR
    if not projects_dir.is_dir():
        return []
    sessions = []
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        if project_filter and project_filter.lower() not in proj.name.lower():
            continue
        sessions.extend(p for p in proj.glob("*.jsonl") if p.stat().st_size > 0)
    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)


def encode_project_path(path: Path) -> str:
    """A filesystem path the way Claude Code encodes it into a project dir
    name under ~/.claude/projects (every non-alphanumeric char becomes -)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def cwd_project_filter(cwd: Path | None = None,
                       projects_dir: Path | None = None,
                       home: Path | None = None) -> str | None:
    """Project filter implied by the current directory, or None for global.

    - cwd (or an ancestor) is a project root  → that project.
    - cwd is a parent "master folder" of several project roots → all of
      them (prefix match). Home and / never scope.
    """
    cwd = cwd or Path.cwd()
    projects_dir = projects_dir or PROJECTS_DIR
    home = home or Path.home()
    if not projects_dir.is_dir():
        return None
    names = [d.name for d in projects_dir.iterdir() if d.is_dir()]

    # master folder: only the cwd itself, and never home or /
    encoded = encode_project_path(cwd)
    if cwd not in (home, Path("/")):
        if any(n == encoded or n.startswith(encoded + "-") for n in names):
            return encoded
    # exact project root among ancestors (covers being in a subfolder)
    for candidate in cwd.parents:
        encoded = encode_project_path(candidate)
        if encoded in names:
            return encoded
    return None


def session_label(path: Path) -> tuple[str | None, str]:
    """Best-effort (title, first human prompt) of a session.

    Titles come from Claude Code's own `summary` records. Reads only the
    head of the file — stops at the first real prompt. Used by --list and
    by name matching.
    """
    title = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype == "summary" and rec.get("summary"):
                    title = rec["summary"]
                elif rtype == "last-prompt" and rec.get("lastPrompt"):
                    return title, one_line(rec["lastPrompt"], 80)
                elif rtype == "user" and not rec.get("isMeta"):
                    text = clean_text(user_text(rec.get("message") or {}))
                    if text:
                        return title, one_line(text, 80)
    except OSError:
        pass
    return title, "(empty)"


def find_session_by_name(query: str,
                         project_filter: str | None = None) -> list[Path]:
    """Sessions whose title, first prompt, or file name contains `query`
    (case-insensitive), newest first."""
    query = query.lower()
    matches = []
    for path in find_sessions(project_filter):
        title, prompt = session_label(path)
        haystack = " ".join(filter(None, (title, prompt, path.stem))).lower()
        if query in haystack:
            matches.append(path)
    return matches


def list_sessions(project_filter: str | None) -> None:
    sessions = find_sessions(project_filter)
    if not sessions:
        print(f"No sessions found under {PROJECTS_DIR}", file=sys.stderr)
        return
    for p in sessions:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        size_kb = p.stat().st_size // 1024
        proj = p.parent.name.lstrip("-").replace("-", "/")
        title, prompt = session_label(p)
        label = f"{title} · {prompt}" if title else prompt
        print(f"{mtime}  {size_kb:>6} KB  {p.stem[:8]}  {proj}")
        print(f"                              └─ {one_line(label, 110)}")


# --------------------------------------------------------------------------- #
#  Parsing
# --------------------------------------------------------------------------- #

def is_web_export(path: Path) -> bool:
    """True for a claude.ai data export (conversations.json): a .json file
    whose first non-whitespace byte opens a JSON array."""
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(64).lstrip()
        return head.startswith("[")
    except OSError:
        return False


def _web_message_text(msg: dict) -> str:
    text = msg.get("text")
    if not text:
        content = msg.get("content")
        if isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
    text = (text or "").strip()
    if msg.get("attachments") or msg.get("files"):
        text = (text + "\n\n[attachment]").strip()
    return text


def _load_web_conversations(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"Cannot read claude.ai export {path}: {e}") from e
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) \
        else []


def parse_claude_export(path: Path, name_filter: str | None = None) -> dict:
    """Parse a claude.ai data export into the same shape as parse_session.

    Picks the newest conversation, or the newest whose title matches
    `name_filter` (case-insensitive substring).
    """
    convos = _load_web_conversations(path)
    if name_filter:
        q = name_filter.lower()
        convos = [c for c in convos if q in str(c.get("name", "")).lower()]
    if not convos:
        raise SystemExit(
            f"No conversation{f' matching {name_filter!r}' if name_filter else ''} "
            f"in {path}. Run with --list to see the conversations it holds.")
    convo = max(convos, key=lambda c: str(c.get("updated_at") or
                                          c.get("created_at") or ""))

    state = _new_parse_state()
    meta = state["meta"]
    meta["session_id"] = convo.get("uuid")
    if convo.get("name"):
        meta["summaries"].append(convo["name"])
    for msg in convo.get("chat_messages") or []:
        if not isinstance(msg, dict):
            continue
        text = _web_message_text(msg)
        if not text:
            continue
        role = "user" if msg.get("sender") == "human" else "assistant"
        meta["n_user" if role == "user" else "n_assistant"] += 1
        ts = msg.get("created_at")
        meta["first_ts"] = meta["first_ts"] or ts
        meta["last_ts"] = ts or meta["last_ts"]
        state["turns"].append({"role": role, "text_parts": [text],
                               "tools": [], "ts": ts})
    state.pop("_tool_names")
    state.pop("sidechains", None)
    return state


def list_export_conversations(path: Path) -> None:
    for c in sorted(_load_web_conversations(path),
                    key=lambda c: str(c.get("updated_at") or ""),
                    reverse=True):
        when = fmt_ts(c.get("updated_at") or c.get("created_at"))
        n = len(c.get("chat_messages") or [])
        print(f"{when}  {n:>4} msgs  {str(c.get('uuid', '?'))[:8]}  "
              f"{one_line(str(c.get('name') or '(untitled)'), 70)}")


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate partial/corrupt lines
    return records


def one_line(text: str, cap: int = 80) -> str:
    text = " ".join(text.split())
    return text[: cap - 1] + "…" if len(text) > cap else text


def clean_text(text: str) -> str:
    """Strip system-reminder / slash-command envelopes and caveats."""
    text = NOISE_RE.sub("", text)
    text = CAVEAT_RE.sub("", text)
    return text.strip()


def user_text(message: dict) -> str:
    """Human-visible text of a user message (string or block-list content)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image attached]")
        return "\n".join(parts)
    return ""


def tool_result_text(block: dict) -> str:
    """Plain text of a tool_result block (string or block-list content)."""
    content = block.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head, tail = int(cap * 0.7), int(cap * 0.2)
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}\n\n[... {omitted} chars omitted ...]\n\n{text[-tail:]}"
    )


def tool_summary(name: str, tool_input: dict) -> tuple[str, str | None, str | None]:
    """One-line description of a tool call.

    Returns (line, file_written, command).
    """
    tool_input = tool_input or {}
    file_written = command = None
    if name in FILE_TOOLS_WRITE:
        file_written = tool_input.get("file_path") or tool_input.get("notebook_path")
        line = f"{name} → {file_written or '?'}"
    elif name in FILE_TOOLS_READ:
        line = f"{name} → {tool_input.get('file_path', '?')}"
    elif name == "Bash":
        command = one_line(tool_input.get("command", ""), TOOL_LINE_CAP)
        desc = tool_input.get("description")
        line = f"Bash: `{command}`" + (f"  ({desc})" if desc else "")
    elif name in ("WebSearch", "WebFetch"):
        target = tool_input.get("query") or tool_input.get("url") or ""
        line = f"{name}: {one_line(str(target), TOOL_LINE_CAP)}"
    elif name in ("Grep", "Glob"):
        line = f"{name}: {one_line(str(tool_input.get('pattern', '')), 100)}"
    elif name in ("Task", "Agent"):
        line = f"Subagent: {one_line(str(tool_input.get('description') or tool_input.get('prompt', '')), 120)}"
    else:
        line = name
    return line, file_written, command


def _new_parse_state() -> dict:
    """Mutable accumulator shared by the per-record handlers below."""
    return {
        "meta": {
            "session_id": None, "cwd": None, "git_branch": None,
            "version": None, "models": set(), "first_ts": None,
            "last_ts": None, "n_user": 0, "n_assistant": 0, "n_tools": 0,
            "summaries": [],
        },
        "turns": [],            # {"role", "text_parts", "tools", "ts"}
        "files_written": {},    # path -> edit count
        "files_read": {},       # path -> read count
        "commands": [],
        "sidechains": [],       # {"prompt", "texts"} — subagent branches
        "_tool_names": {},      # tool_use_id -> tool name (internal)
    }


def _update_envelope_meta(rec: dict, meta: dict) -> None:
    """Fold a record's envelope fields (session id, cwd, timestamps) into meta."""
    for key, field in (("session_id", "sessionId"), ("cwd", "cwd"),
                       ("git_branch", "gitBranch"), ("version", "version")):
        if rec.get(field) and not meta[key]:
            meta[key] = rec[field]
    ts = rec.get("timestamp")
    if ts:
        meta["first_ts"] = meta["first_ts"] or ts
        meta["last_ts"] = ts


def _current_assistant_turn(state: dict) -> dict:
    """Last turn if it's an assistant turn, else a fresh one (turn merging)."""
    turns = state["turns"]
    if turns and turns[-1]["role"] == "assistant":
        return turns[-1]
    turn = {"role": "assistant", "text_parts": [], "tools": []}
    turns.append(turn)
    return turn


def _handle_tool_use(block: dict, turn: dict, state: dict) -> bool:
    """Record one tool_use block. Returns True if it produced assistant text."""
    name = block.get("name", "?")
    state["_tool_names"][block.get("id", "")] = name
    # In SDK/Cowork sessions the assistant's prose is sent via this tool —
    # recover it as normal assistant text.
    if name == "SendUserMessage":
        msg = (block.get("input") or {}).get("message", "")
        if msg.strip():
            turn["text_parts"].append(msg.strip())
            return True
        return False
    state["meta"]["n_tools"] += 1
    line, file_written, command = tool_summary(name, block.get("input"))
    turn["tools"].append(line)
    if file_written:
        fw = state["files_written"]
        fw[file_written] = fw.get(file_written, 0) + 1
    if name in FILE_TOOLS_READ:
        file_read = (block.get("input") or {}).get("file_path")
        if file_read:
            fr = state["files_read"]
            fr[file_read] = fr.get(file_read, 0) + 1
    if command:
        state["commands"].append(command)
    return False


def _handle_assistant_record(rec: dict, state: dict) -> None:
    """Merge an assistant API record into the current assistant turn."""
    message = rec.get("message") or {}
    if message.get("model"):
        state["meta"]["models"].add(message["model"])
    content = message.get("content")
    if not isinstance(content, list):
        return
    turn = _current_assistant_turn(state)
    turn.setdefault("ts", rec.get("timestamp"))
    added_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text", "").strip():
            turn["text_parts"].append(block["text"].strip())
            added_text = True
        elif btype == "tool_use":
            added_text = _handle_tool_use(block, turn, state) or added_text
        # thinking blocks are deliberately dropped
    if added_text:
        state["meta"]["n_assistant"] += 1


def _handle_user_record(rec: dict, state: dict) -> None:
    """Append a user turn, filtering meta records and tool-result echoes."""
    if rec.get("isMeta"):
        return
    message = rec.get("message") or {}
    content = message.get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    ):
        # Tool results echoed back are noise — except answers the human
        # gave to AskUserQuestion, which are real user input.
        for b in content:
            if (isinstance(b, dict) and b.get("type") == "tool_result"
                    and state["_tool_names"].get(b.get("tool_use_id", ""))
                    == "AskUserQuestion"):
                answer = tool_result_text(b)
                if answer:
                    state["meta"]["n_user"] += 1
                    state["turns"].append({"role": "user",
                                           "text_parts": [answer],
                                           "tools": [],
                                           "ts": rec.get("timestamp")})
        return
    text = clean_text(user_text(message))
    if not text:
        return
    state["meta"]["n_user"] += 1
    state["turns"].append({"role": "user", "text_parts": [text],
                           "tools": [], "ts": rec.get("timestamp")})


def _handle_sidechain_record(rec: dict, state: dict) -> None:
    """Collect subagent-branch content for --include-sidechains."""
    message = rec.get("message") or {}
    groups = state["sidechains"]
    if rec.get("type") == "user":
        text = clean_text(user_text(message))
        if text:  # a subagent's task prompt starts a new group
            groups.append({"prompt": one_line(text, 120), "texts": []})
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    if not groups:
        groups.append({"prompt": None, "texts": []})
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "text"
                and block.get("text", "").strip()):
            groups[-1]["texts"].append(block["text"].strip())


def parse_session(path: Path) -> dict:
    """Parse a session JSONL into turns + metadata + activity stats."""
    state = _new_parse_state()
    for rec in load_records(path):
        rtype = rec.get("type")
        if rtype == "summary" and rec.get("summary"):
            state["meta"]["summaries"].append(rec["summary"])
        elif rtype in ("user", "assistant"):
            if rec.get("isSidechain"):
                _handle_sidechain_record(rec, state)
                continue
            _update_envelope_meta(rec, state["meta"])
            if rtype == "assistant":
                _handle_assistant_record(rec, state)
            else:
                _handle_user_record(rec, state)
        # unknown record types (queue-operation, attachment, …) are skipped

    # drop empty turns (e.g. assistant records that carried only thinking)
    state["turns"] = [t for t in state["turns"]
                      if t["text_parts"] or t["tools"]]
    state.pop("_tool_names")
    return state


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _since_cutoff(spec: str, last_ts: str | None) -> datetime:
    """'2h'/'30m'/'1d' → that long before the session's end; else an ISO
    timestamp."""
    m = re.fullmatch(r"(\d+)\s*([mhd])", spec.strip())
    if m:
        amount = int(m.group(1))
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        base = _parse_ts(last_ts) or datetime.now(timezone.utc)
        return base - timedelta(**{unit: amount})
    cutoff = _parse_ts(spec)
    if cutoff is None:
        raise SystemExit(f"Cannot parse --since {spec!r}: use e.g. 2h, 45m, "
                         f"1d, or an ISO timestamp like 2026-08-23T18:00.")
    return cutoff


def slice_turns(parsed: dict, last: int | None = None,
                since: str | None = None) -> dict:
    """Keep only the tail of the conversation: everything after a time
    cutoff (--since) and/or from the Nth-from-last user turn (--last)."""
    turns = parsed["turns"]
    total_user = parsed["meta"]["n_user"]
    start = 0
    if since:
        cutoff = _since_cutoff(since, parsed["meta"]["last_ts"])
        start = len(turns)
        for idx, turn in enumerate(turns):
            ts = _parse_ts(turn.get("ts"))
            if ts and ts >= cutoff:
                start = idx
                break
    if last is not None:
        user_idx = [i for i, t in enumerate(turns) if t["role"] == "user"]
        if len(user_idx) > last:
            start = max(start, user_idx[-last]) if last else len(turns)
    if start:
        kept = turns[start:]
        n_user = sum(1 for t in kept if t["role"] == "user")
        parsed["turns"] = kept
        parsed["slice_note"] = (f"showing the last {n_user} of {total_user} "
                                f"user turns")
    return parsed


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #

def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) \
            .astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def render_header(parsed: dict, source: Path) -> str:
    m = parsed["meta"]
    models = ", ".join(sorted(m["models"])) or "?"
    lines = [
        "# Conversation handoff",
        "",
        "> **To the receiving assistant:** this is the context of a working session",
        "> between a human and another AI assistant (Claude). You are taking over.",
        "> Read it, then continue the work — don't re-explain this document back,",
        "> and don't redo completed steps unless asked.",
        "",
        "## Session",
        "",
    ]
    if m["cwd"]:
        lines.append(f"- **Project:** `{m['cwd']}`" +
                     (f" (branch `{m['git_branch']}`)" if m["git_branch"]
                      else ""))
    lines += [
        f"- **When:** {fmt_ts(m['first_ts'])} → {fmt_ts(m['last_ts'])}",
        f"- **Assistant model:** {models}",
        f"- **Activity:** {m['n_user']} user messages, "
        f"{m['n_assistant']} assistant replies, {m['n_tools']} tool calls",
        f"- **Source:** `{source}`",
    ]
    if m["summaries"]:
        lines += ["", f"**Session title:** {m['summaries'][-1]}"]
    return "\n".join(lines)


def render_activity(parsed: dict, max_commands: int = 30) -> str:
    out = []
    fw, fr, cmds = parsed["files_written"], parsed["files_read"], parsed["commands"]
    if fw:
        out += ["## Files created / modified", ""]
        out += [f"- `{f}`" + (f" ({n}× edits)" if n > 1 else "")
                for f, n in sorted(fw.items())]
        out.append("")
    if cmds:
        deduped = list(dict.fromkeys(cmds))
        shown = deduped[-max_commands:]
        out += ["## Commands run", ""]
        if len(deduped) > len(shown):
            out.append(f"_(last {len(shown)} of {len(deduped)} distinct commands)_")
            out.append("")
        out += [f"- `{c}`" for c in shown]
        out.append("")
    if fr and not fw:
        out += ["## Files read", ""]
        out += [f"- `{f}`" for f in sorted(fr)][:20]
        out.append("")
    return "\n".join(out).rstrip()


def render_transcript(parsed: dict, include_tools: bool,
                      max_chars: int) -> str:
    blocks = []
    for turn in parsed["turns"]:
        text = "\n\n".join(turn["text_parts"]).strip()
        if turn["role"] == "user":
            blocks.append("### 🧑 User\n\n" + truncate(text, USER_MSG_CAP))
        else:
            parts = []
            if text:
                parts.append(truncate(text, ASSISTANT_MSG_CAP))
            if include_tools and turn["tools"]:
                tool_lines = "\n".join(f"- {t}" for t in turn["tools"])
                parts.append(f"<details><summary>{len(turn['tools'])} tool "
                             f"calls</summary>\n\n{tool_lines}\n\n</details>")
            elif turn["tools"] and not text:
                parts.append(f"_[{len(turn['tools'])} tool calls]_")
            if parts:
                blocks.append("### 🤖 Assistant\n\n" + "\n\n".join(parts))

    if parsed.get("slice_note"):
        blocks.insert(0, f"_[{parsed['slice_note']}]_")
    body = "\n\n".join(blocks)
    if len(body) > max_chars:
        # Keep the opening (goal-setting) and the recent end (current state).
        head, tail = int(max_chars * 0.35), int(max_chars * 0.6)
        omitted = len(body) - head - tail
        body = (f"{body[:head]}\n\n---\n\n_[... middle of the conversation "
                f"omitted ({omitted} chars). The beginning sets the goal; "
                f"what follows is the most recent state. ...]_\n\n---\n\n"
                f"{body[-tail:]}")
    return "## Conversation\n\n" + body


def render_sidechains(parsed: dict, max_each: int = 2000) -> str:
    groups = [g for g in parsed.get("sidechains", []) if g["texts"]]
    if not groups:
        return ""
    out = ["## Subagent work (sidechains)", ""]
    for g in groups:
        prompt = g["prompt"] or "(task prompt not recorded)"
        out.append(f"### Subagent: {prompt}")
        out.append("")
        out.append(truncate("\n\n".join(g["texts"]), max_each))
        out.append("")
    return "\n".join(out).rstrip()


def render_footer() -> str:
    return ("---\n\n_Exported with [claude-handoff]"
            "(https://github.com/Vasilispapg/claude-handoff) — continue from here._")


def build_deterministic(parsed: dict, source: Path, include_tools: bool,
                        max_chars: int,
                        include_sidechains: bool = False) -> str:
    sections = [
        render_header(parsed, source),
        render_activity(parsed),
        render_transcript(parsed, include_tools, max_chars),
        render_sidechains(parsed) if include_sidechains else "",
        render_footer(),
    ]
    return "\n\n".join(s for s in sections if s.strip()) + "\n"


# --------------------------------------------------------------------------- #
#  LLM summarization (--llm)
# --------------------------------------------------------------------------- #

SUMMARY_PROMPT = """\
Below is the cleaned transcript of a working session between a human and an AI \
coding assistant. Write a handoff document in markdown so that a different AI \
assistant can continue the work seamlessly. Use exactly these sections:

## Goal
## Key decisions (and why)
## Current state (what is done, what works)
## Files & artifacts touched
## Next steps / open questions
## Constraints & user preferences

Rules: be specific; preserve exact file paths, commands, identifiers, URLs and \
version numbers; quote short code snippets only when essential; do not invent \
anything not present in the transcript; do not address the human; write it for \
the next assistant. Answer in the language the user writes in.
"""

CHUNK_PROMPT = """\
Below is part {i} of {n} of a long working session between a human and an AI \
coding assistant. Write compact chronological notes (max 500 words) for a \
later synthesis: goal and subgoals, key decisions and why, files and commands \
touched, state at the end of this part, open threads. Preserve exact paths, \
commands, identifiers and version numbers. Do not invent anything. Answer in \
the language the user writes in.
"""


def provider_key(provider: str) -> str | None:
    """First non-empty API key among the provider's accepted env vars."""
    for name in PROVIDERS[provider]["env_keys"]:
        value = os.environ.get(name)
        if value:
            return value
    return None


def http_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise SystemExit(f"LLM API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"LLM API unreachable: {e.reason}") from e


def _call_claude(key: str, model: str, prompt: str) -> str:
    """Anthropic Messages API."""
    data = http_json(
        "https://api.anthropic.com/v1/messages",
        {"model": model, "max_tokens": 4096,
         "messages": [{"role": "user", "content": prompt}]},
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    return "".join(b.get("text", "") for b in data.get("content", []))


def _call_openai(key: str, model: str, prompt: str) -> str:
    """OpenAI Chat Completions API."""
    data = http_json(
        "https://api.openai.com/v1/chat/completions",
        {"model": model,
         "messages": [{"role": "user", "content": prompt}]},
        {"Authorization": f"Bearer {key}"},
    )
    return data["choices"][0]["message"]["content"]


def _call_gemini(key: str, model: str, prompt: str) -> str:
    """Google Gemini generateContent API."""
    data = http_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent",
        {"contents": [{"parts": [{"text": prompt}]}]},
        {"x-goog-api-key": key},
    )
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_claude_cli(key: str | None, model: str | None, prompt: str) -> str:
    """Locally-installed Claude Code CLI (`claude -p`) — the user's existing
    Claude subscription pays for the call; no API key involved."""
    del key  # the CLI carries its own authentication
    if shutil.which("claude") is None:
        raise SystemExit(
            "`claude` CLI not found on PATH. Install Claude Code "
            "(https://claude.ai/code) and authenticate once, or use "
            "--llm claude with an API key instead.")
    cmd = ["claude", "-p", "--output-format", "json",
           "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    # Scrub host-session variables so a nested run (claude-handoff invoked
    # from inside a Claude Code session) authenticates like a fresh CLI.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE") or k == "CLAUDE_CODE_OAUTH_TOKEN"}
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", timeout=600, check=False, env=env)
    except subprocess.TimeoutExpired:
        raise SystemExit("claude CLI timed out after 600s") from None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        envelope = {}
    result = (envelope.get("result") or "").strip()
    if proc.returncode != 0 or envelope.get("is_error"):
        detail = result or proc.stderr.strip()[:500] or proc.stdout[:300]
        raise SystemExit(f"claude CLI failed (exit {proc.returncode}): {detail}")
    if not result:
        raise SystemExit("claude CLI returned an empty summary")
    return result


def _call_ollama(key: str | None, model: str, prompt: str) -> str:
    """Local Ollama server (OpenAI-compatible endpoint) — fully offline
    summaries; nothing leaves the machine."""
    base = os.environ.get("OLLAMA_BASE_URL",
                          "http://localhost:11434/v1").rstrip("/")
    headers = {}
    token = os.environ.get("OLLAMA_API_KEY")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        data = http_json(
            f"{base}/chat/completions",
            {"model": model, "stream": False,
             "messages": [{"role": "user", "content": prompt}]},
            headers)
    except SystemExit as e:
        raise SystemExit(
            f"{e} — is Ollama running? Start it with `ollama serve` "
            f"(endpoint: {base}, override with OLLAMA_BASE_URL).") from e
    return data["choices"][0]["message"]["content"]


# Each provider: accepted key env vars (first hit wins, graphify-style;
# empty tuple = no key needed), a default model (None = provider decides),
# and the call strategy. Adding a provider touches nothing but this table.
PROVIDERS.update({
    "claude": {
        "env_keys": ("ANTHROPIC_API_KEY", "CLAUDE_API"),
        "default_model": "claude-sonnet-4-5",
        "call": _call_claude,
    },
    "openai": {
        "env_keys": ("OPENAI_API_KEY", "GPT_API"),
        "default_model": "gpt-4o-mini",
        "call": _call_openai,
    },
    "gemini": {
        "env_keys": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API"),
        "default_model": "gemini-2.5-flash",
        "call": _call_gemini,
    },
    "claude-cli": {
        "env_keys": (),
        "default_model": None,
        "call": _call_claude_cli,
    },
    "ollama": {
        "env_keys": (),  # local server; OLLAMA_API_KEY only if you set one
        "default_model": os.environ.get("OLLAMA_MODEL", "llama3.1"),
        "call": _call_ollama,
    },
})


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace secret-shaped strings with [REDACTED]; returns (text, count).

    Precision-first: known key prefixes and KEY=value assignments only —
    git hashes, URLs and normal prose are left alone.
    """
    total = 0
    for pattern in SECRET_RES:
        def _sub(m: "re.Match[str]") -> str:
            keep = m.group(1) if m.groups() else ""
            return keep + "[REDACTED]"
        text, n = pattern.subn(_sub, text)
        total += n
    return text, total


def _chunk_cache_path(chunk: str, provider: str, model: str | None) -> Path:
    digest = hashlib.sha256(
        f"{CACHE_VERSION}|{provider}|{model}|{chunk}".encode()).hexdigest()
    return CACHE_DIR / f"chunk-{digest[:32]}.json"


def _cache_get(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["notes"]
    except (OSError, ValueError, KeyError):
        return None


def _cache_put(path: Path, notes: str) -> None:
    try:  # best-effort — a failing cache must never fail the run
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"notes": notes}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def _fmt_secs(seconds: float) -> str:
    seconds = int(seconds)
    return (f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60
            else f"{seconds}s")


def _new_progress(total: int) -> dict:
    """Progress state for the chunk loop. Interactive only when stderr is a
    terminal; plain one-line-per-event otherwise (pipes, CI, tests)."""
    return {"total": total, "done": 0, "start": time.time(),
            "durations": [], "tty": sys.stderr.isatty()}


def _draw_progress(st: dict, label: str) -> None:
    if not st["tty"]:
        return
    width = 24
    filled = int(width * st["done"] / st["total"])
    bar = "█" * filled + "░" * (width - filled)
    eta = ""
    if st["durations"] and st["done"] < st["total"]:
        avg = sum(st["durations"]) / len(st["durations"])
        eta = f" | ~{_fmt_secs(avg * (st['total'] - st['done']))} left"
    line = (f"[{bar}] {st['done']}/{st['total']} chunks | "
            f"{_fmt_secs(time.time() - st['start'])} elapsed{eta} | {label}")
    print(f"\r{line[:118]:<118}", end="", file=sys.stderr, flush=True)


def _progress_step(st: dict, label: str, work) -> str:
    """Run `work()` for one chunk with a live-updating stderr line (TTY) or
    a plain printed line (non-TTY). Returns work()'s result."""
    if not st["tty"]:
        print(label, file=sys.stderr)
        return work()
    started = time.time()
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(1.0):
            _draw_progress(st, label)

    ticker = threading.Thread(target=tick, daemon=True)
    _draw_progress(st, label)
    ticker.start()
    try:
        result = work()
    finally:
        stop.set()
        ticker.join(timeout=2)
    st["durations"].append(time.time() - started)
    return result


def _progress_finish(st: dict) -> None:
    if st["tty"]:
        _draw_progress(st, "done")
        print(file=sys.stderr)


def _call_with_retry(call, key: str | None, model: str | None,
                     prompt: str, attempts: int = 2) -> str:
    """One retry on provider failure — transient 429/5xx shouldn't waste a
    long map-reduce run. Chunk progress is cached, so even a final failure
    resumes cheaply."""
    for attempt in range(1, attempts + 1):
        try:
            return call(key, model, prompt)
        except SystemExit as e:
            if attempt == attempts:
                raise SystemExit(
                    f"{e} — completed chunks are cached; rerun to resume."
                ) from e
            print(f"  provider error ({e}); retrying…", file=sys.stderr)
            time.sleep(3)
    raise AssertionError("unreachable")


def _chunk_text(text: str, cap: int) -> list[str]:
    """Split rendered transcript into ≤cap chunks on turn boundaries."""
    parts = text.split("\n\n### ")
    blocks = [parts[0]] + ["### " + p for p in parts[1:]]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > cap:
            block = truncate(block, cap)
        if current and len(current) + len(block) + 2 > cap:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


def llm_summarize(provider: str, model: str | None, transcript: str,
                  focus: str | None = None, use_cache: bool = True) -> str:
    """Summarize a transcript with the provider's call strategy.

    Transcripts beyond LLM_INPUT_CAP are map-reduced: per-chunk notes
    first (cached, retried), then one synthesis pass — nothing is
    silently dropped. `focus` carries extra user instructions.
    """
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise SystemExit(f"Unknown provider: {provider}. "
                         f"Available: {', '.join(sorted(PROVIDERS))}")
    key = provider_key(provider)
    if cfg["env_keys"] and not key:
        accepted = " or ".join(cfg["env_keys"])
        raise SystemExit(f"Set {accepted} to use --llm {provider}")
    model = model or cfg["default_model"]
    extra = ("\nAdditional instructions from the user — follow them as "
             f"well:\n{focus.strip()}\n" if focus else "")

    if len(transcript) <= LLM_INPUT_CAP:
        prompt = SUMMARY_PROMPT + extra + "\nTRANSCRIPT:\n" + transcript
        st = _new_progress(1)
        result = _progress_step(
            st, f"summarizing ({len(transcript):,} chars, one pass)…",
            lambda: cfg["call"](key, model, prompt))
        _progress_finish(st)
        return result

    chunks = _chunk_text(transcript, CHUNK_CAP)
    print(f"Transcript is {len(transcript):,} chars — map-reduce over "
          f"{len(chunks)} chunks (up to {len(chunks) + 1} LLM calls).",
          file=sys.stderr)
    st = _new_progress(len(chunks) + 1)  # + the reduce pass
    notes = []
    for i, chunk in enumerate(chunks, 1):
        # Focus is applied only in the reduce pass, so chunk notes stay
        # reusable across runs with different --focus.
        prompt = (CHUNK_PROMPT.format(i=i, n=len(chunks))
                  + "\nPART:\n" + chunk)
        cache_file = _chunk_cache_path(chunk, provider, model)
        cached = _cache_get(cache_file) if use_cache else None
        if cached is not None:
            print(f"  part {i}/{len(chunks)} — cached.", file=sys.stderr)
            notes.append(cached)
            st["done"] += 1
            continue
        result = _progress_step(
            st, f"summarizing part {i}/{len(chunks)} "
                f"({len(chunk):,} chars)…",
            lambda p=prompt: _call_with_retry(cfg["call"], key, model, p))
        if use_cache:
            _cache_put(cache_file, result)
        notes.append(result)
        st["done"] += 1
    joined = "\n\n".join(f"[Part {i}/{len(chunks)} notes]\n{n.strip()}"
                         for i, n in enumerate(notes, 1))
    overhead = (
        SUMMARY_PROMPT + extra
        + "\nThe session was too long for one pass. Below are chronological "
          "notes from each of its parts — synthesize them into ONE handoff "
          "document:\n\nNOTES:\n")
    # Truncate only the notes — instructions and focus must never be cut.
    budget = max(LLM_INPUT_CAP - len(overhead), 1000)
    reduce_prompt = overhead + truncate(joined, budget)
    result = _progress_step(
        st, f"synthesizing final summary from {len(chunks)} parts…",
        lambda: _call_with_retry(cfg["call"], key, model, reduce_prompt))
    st["done"] += 1
    _progress_finish(st)
    return result


def build_llm(parsed: dict, source: Path, provider: str, model: str | None,
              with_transcript: bool, max_chars: int,
              focus: str | None = None, redact: bool = True,
              use_cache: bool = True) -> str:
    transcript = render_transcript(parsed, include_tools=True,
                                   max_chars=10**9)  # chunking handles size
    activity = render_activity(parsed)
    outbound = activity + "\n\n" + transcript
    if redact:
        outbound, n_redacted = redact_secrets(outbound)
        if n_redacted:
            print(f"Redacted {n_redacted} secret-looking string(s) before "
                  f"sending to the LLM (--no-redact to disable).",
                  file=sys.stderr)
    summary = llm_summarize(provider, model, outbound, focus=focus,
                            use_cache=use_cache)
    sections = [render_header(parsed, source), summary.strip()]
    if with_transcript:
        sections.append(render_transcript(parsed, include_tools=False,
                                          max_chars=max_chars))
    sections.append(render_footer())
    return "\n\n".join(sections) + "\n"


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def _copy_clipboard(text: str) -> str:
    """Copy text to the system clipboard; returns the tool used."""
    for cmd in (["pbcopy"], ["wl-copy"],
                ["xclip", "-selection", "clipboard"], ["clip"]):
        if shutil.which(cmd[0]):
            proc = subprocess.run(cmd, input=text, text=True,
                                  encoding="utf-8", check=False)
            if proc.returncode == 0:
                return cmd[0]
    raise SystemExit("No clipboard tool found — expected pbcopy (macOS), "
                     "wl-copy/xclip (Linux) or clip (Windows).")


# ------------------------------------------------------------------------- #
#  Auto-handoff hook (claude-handoff --install-hook)
# ------------------------------------------------------------------------- #

HOOK_COMMAND = "claude-handoff --hook-stdin"
HANDOFFS_DIR = Path(os.environ.get("CLAUDE_HOME",
                                   str(Path.home() / ".claude"))) / "handoffs"


def install_hook(settings_path: Path | None = None,
                 remove: bool = False) -> None:
    """Add (or remove) a SessionEnd hook in Claude Code's settings.json so
    every session leaves a handoff in ~/.claude/handoffs automatically."""
    settings_path = settings_path or (
        Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")))
        / "settings.json")
    settings: dict = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise SystemExit(
                f"{settings_path} is not valid JSON ({e}) — fix it first; "
                f"refusing to overwrite it.") from e
    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("SessionEnd", [])

    def _is_ours(h: dict) -> bool:
        return h.get("command") == HOOK_COMMAND

    if remove:
        for entry in entries:
            entry["hooks"] = [h for h in entry.get("hooks", [])
                              if not _is_ours(h)]
        hooks["SessionEnd"] = [e for e in entries if e.get("hooks")]
        if not hooks["SessionEnd"]:
            del hooks["SessionEnd"]
        if not hooks:
            del settings["hooks"]
        action = "removed from"
    else:
        present = any(_is_ours(h) for e in entries
                      for h in e.get("hooks", []))
        if not present:
            entries.append({"hooks": [{"type": "command",
                                       "command": HOOK_COMMAND}]})
        action = "installed in"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2,
                                        ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"Auto-handoff hook {action} {settings_path}.", file=sys.stderr)
    if not remove:
        print(f"Every Claude Code session now writes a handoff to "
              f"{HANDOFFS_DIR}/<session>.md when it ends.\n"
              f"Undo with: claude-handoff --uninstall-hook", file=sys.stderr)


def run_hook_mode() -> None:
    """SessionEnd hook entrypoint: reads Claude Code's hook JSON on stdin,
    writes a deterministic handoff for that session. Silent on problems —
    a hook must never break the host session."""
    try:
        payload = json.load(sys.stdin)
        transcript = Path(payload["transcript_path"])
        if not transcript.is_file():
            return
        parsed = parse_session(transcript)
        if not parsed["turns"] or looks_trivial(parsed):
            return
        HANDOFFS_DIR.mkdir(parents=True, exist_ok=True)
        out = HANDOFFS_DIR / f"{transcript.stem}.md"
        out.write_text(build_deterministic(parsed, transcript,
                                           include_tools=False,
                                           max_chars=DEFAULT_MAX_CHARS),
                       encoding="utf-8")
        print(f"handoff written: {out}")
    except Exception:  # noqa: BLE001 — deliberately swallow: see docstring
        return


class _HelpfulParser(argparse.ArgumentParser):
    """argparse's designed extension point: on any usage error, point the
    user at --help and --list instead of leaving them with a bare error."""

    def error(self, message: str) -> None:  # noqa: D102 (argparse contract)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n"
                     f"Run `{self.prog} --help` to see every option, or "
                     f"`{self.prog} --list` to see your sessions.\n")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = _HelpfulParser(
        prog="claude-handoff",
        description="Summarize & export a Claude Code session for another LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='examples:\n'
               '  claude-handoff --list\n'
               '  claude-handoff --name "login bug"       # newest session whose title/prompt matches\n'
               '  claude-handoff "login bug" --llm claude-cli   # positional works as a name too\n'
               '  claude-handoff --project myrepo -o -\n',
    )
    ap.add_argument("session", nargs="?",
                    help="path to a session .jsonl, or a name to search for "
                         "(default: latest session)")
    ap.add_argument("--list", action="store_true",
                    help="list available sessions (title · first prompt) and exit")
    ap.add_argument("--name", metavar="QUERY",
                    help="pick newest session whose title or first prompt "
                         "contains QUERY (case-insensitive)")
    ap.add_argument("--project", metavar="NAME",
                    help="pick latest session whose project path contains NAME")
    ap.add_argument("--any", action="store_true",
                    help="ignore the current directory; consider sessions "
                         "of every project (default when outside a project)")
    ap.add_argument("-o", "--output", default="handoff.md",
                    help="output file, or '-' for stdout (default: handoff.md)")
    ap.add_argument("--include-tools", action="store_true",
                    help="include collapsed per-tool-call detail in transcript")
    ap.add_argument("--include-sidechains", action="store_true",
                    help="append a section with subagent (sidechain) work")
    ap.add_argument("--last", type=int, metavar="N",
                    help="keep only the last N user turns")
    ap.add_argument("--since", metavar="WHEN",
                    help="keep only turns after WHEN: 2h, 45m, 1d (before "
                         "session end) or an ISO timestamp")
    ap.add_argument("--install-hook", action="store_true",
                    help="auto-write a handoff when every Claude Code "
                         "session ends (SessionEnd hook)")
    ap.add_argument("--uninstall-hook", action="store_true",
                    help="remove the auto-handoff hook")
    ap.add_argument("--hook-stdin", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                    help=f"cap transcript section size (default {DEFAULT_MAX_CHARS})")
    ap.add_argument("--llm", choices=sorted(PROVIDERS),
                    help="summarize with an LLM instead of deterministic export; "
                         "claude-cli uses your local Claude Code login, no API key")
    ap.add_argument("--model", help="override the LLM model id for --llm")
    ap.add_argument("--focus", metavar="TEXT",
                    help="with --llm: extra instructions for the summary "
                         "(e.g. --focus \"emphasize the API decisions\")")
    ap.add_argument("--with-transcript", action="store_true",
                    help="with --llm: also append the cleaned transcript")
    ap.add_argument("--no-redact", action="store_true",
                    help="with --llm: do not strip secret-looking strings "
                         "from the transcript before sending it")
    ap.add_argument("--no-cache", action="store_true",
                    help="with --llm: disable the chunk-note cache "
                         f"({CACHE_DIR})")
    ap.add_argument("--version", action="version", version=__version__)
    return ap


def _newest_named_session(query: str, project_filter: str | None) -> Path:
    matches = find_session_by_name(query, project_filter)
    if not matches:
        raise SystemExit(
            f"No session matches {query!r}"
            + (f" in projects matching '{project_filter}'" if project_filter
               else "")
            + ". Run `claude-handoff --list` to see titles and prompts.")
    if len(matches) > 1:
        print(f"{len(matches)} sessions match {query!r}; using the newest. "
              f"Run --list to see all of them.", file=sys.stderr)
    print(f"Using session: {matches[0]}", file=sys.stderr)
    return matches[0]


def looks_trivial(parsed: dict) -> bool:
    """True for sessions with (almost) no conversation — e.g. the stub
    session `claude /login` leaves behind, or a chat someone typed a single
    shell command into. Auto-selection skips these; explicit choices don't.
    """
    meta = parsed["meta"]
    if meta["n_user"] + meta["n_assistant"] > 2:
        return False
    chars = sum(len(part) for turn in parsed["turns"]
                for part in turn["text_parts"])
    return chars < 600


def _newest_meaningful_session(sessions: list[Path],
                               max_probe: int = 15) -> Path:
    """First session (newest-first) that isn't nearly empty."""
    for path in sessions[:max_probe]:
        if not looks_trivial(parse_session(path)):
            return path
        title, prompt = session_label(path)
        print(f"Skipping nearly-empty session {path.stem[:8]} "
              f"({title or prompt}) — pass a path or --name to force it.",
              file=sys.stderr)
    return sessions[0]


def resolve_source(args: argparse.Namespace) -> Path:
    """The session file to export: explicit path, name match, or newest."""
    if args.session:
        source = Path(args.session).expanduser()
        if source.is_file():
            return source
        looks_like_path = "/" in args.session or args.session.endswith(".jsonl")
        if looks_like_path:
            raise SystemExit(f"Not a file: {source}. "
                             f"Run `claude-handoff --list` to see sessions.")
        return _newest_named_session(args.session, args.project)
    if args.name:
        return _newest_named_session(args.name, args.project)
    scope = args.project
    if not scope and not args.any:
        scope = cwd_project_filter()
        if scope:
            print("Scoped to this directory's project(s) — pass --any "
                  "for all projects.", file=sys.stderr)
    sessions = find_sessions(scope)
    if not sessions and scope and not args.project:
        print("No sessions for this directory; falling back to all "
              "projects.", file=sys.stderr)
        sessions = find_sessions(None)
    if not sessions:
        raise SystemExit(
            f"No sessions found under {PROJECTS_DIR}"
            + (f" matching '{args.project}'" if args.project else "")
            + ". Pass a .jsonl path explicitly, or run --list.")
    source = _newest_meaningful_session(sessions)
    print(f"Using latest session: {source}", file=sys.stderr)
    return source


def build_document(parsed: dict, source: Path,
                   args: argparse.Namespace) -> str:
    """Deterministic or LLM-summarized document, per the CLI flags."""
    if args.llm:
        return build_llm(parsed, source, args.llm, args.model,
                         args.with_transcript, args.max_chars,
                         focus=args.focus, redact=not args.no_redact,
                         use_cache=not args.no_cache)
    return build_deterministic(parsed, source, args.include_tools,
                               args.max_chars,
                               include_sidechains=args.include_sidechains)


def write_output(doc: str, parsed: dict, args: argparse.Namespace) -> None:
    if args.output == "-":
        sys.stdout.write(doc)
        return
    if args.output in ("clipboard", "clip"):
        tool = _copy_clipboard(doc)
        print(f"Copied to clipboard via {tool} ({len(doc):,} chars, "
              f"{parsed['meta']['n_user']} user messages"
              f"{', LLM-summarized' if args.llm else ''}) — paste away.",
              file=sys.stderr)
        return
    out = Path(args.output)
    out.write_text(doc, encoding="utf-8")
    n_user = parsed["meta"]["n_user"]
    print(f"Wrote {out} ({len(doc):,} chars, {n_user} user messages"
          f"{', LLM-summarized' if args.llm else ''})", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    if args.hook_stdin:
        run_hook_mode()
        return
    if args.install_hook or args.uninstall_hook:
        install_hook(remove=args.uninstall_hook)
        return
    if args.list:
        if args.session and is_web_export(Path(args.session).expanduser()):
            list_export_conversations(Path(args.session).expanduser())
        else:
            list_sessions(args.project)
        return
    source = resolve_source(args)
    if is_web_export(source):
        parsed = parse_claude_export(source, name_filter=args.name)
    else:
        parsed = parse_session(source)
    if not parsed["turns"]:
        raise SystemExit("Session parsed but contains no conversation turns.")
    slice_turns(parsed, last=args.last, since=args.since)
    if not parsed["turns"]:
        raise SystemExit("Nothing left after --last/--since — widen the "
                         "range or drop the filter.")
    write_output(build_document(parsed, source, args), parsed, args)


if __name__ == "__main__":
    main()
