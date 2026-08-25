#!/usr/bin/env python3
"""claude-handoff — summarize & export a Claude Code session for another LLM.

Reads Claude Code's local session transcripts (JSONL in ~/.claude/projects),
strips the tool-call noise, and produces a single clean handoff.md you can
paste into Gemini, GPT, another Claude — anything — so it can pick up where
the session left off.

Also reads claude.ai and ChatGPT data exports (conversations.json).
Zero dependencies. Python 3.9+. MIT license.

Usage:
    claude-handoff                      # latest session -> handoff.md
    chf                                 # same tool, shorter to type
    claude-handoff --list               # list available sessions
    claude-handoff -i                   # numbered interactive picker
    claude-handoff --name "login bug"   # newest session matching a name
    claude-handoff --grep "CORS"        # newest session that talked about it
    claude-handoff --fit 32k            # sized to fit a 32k-token context
    claude-handoff --project myrepo --merge   # whole project, one handoff
    claude-handoff --last 5 -o clipboard      # recent turns -> clipboard
    claude-handoff --llm claude-cli --focus "emphasize the API decisions"
    claude-handoff conversations.json --name "that chat"  # web exports
    claude-handoff --format json        # machine-readable output
    claude-handoff --install-hook       # auto-handoff when sessions end
    claude-handoff --mcp                # MCP server (list_sessions, handoff)

LLM summaries (--llm):
    claude-cli  -> local Claude Code login, no API key
    ollama      -> local model, fully offline
    claude|openai|gemini -> API keys, first set env var wins:
        ANTHROPIC_API_KEY (or CLAUDE_API) | OPENAI_API_KEY (or GPT_API)
        GEMINI_API_KEY (or GOOGLE_API_KEY / GEMINI_API)
Huge sessions are map-reduced in chunks with a resume cache; secrets are
redacted before anything leaves the machine.
"""

# GENERATED FILE — do not edit. Source of truth: the claude_handoff/
# package in this repo. Rebuild with: python3 scripts/build_single.py

from __future__ import annotations

import argparse
import concurrent.futures
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

__version__ = "0.13.0"


# --------------------------------------------------------------------------- #
#  textutil
# --------------------------------------------------------------------------- #

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


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) \
            .astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


# --------------------------------------------------------------------------- #
#  redact
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _home() -> Path:
    return Path.home()


def anonymize_text(text: str, home: Path | None = None) -> tuple[str, int]:
    """Strip identity for public sharing: collapse the home directory to ~,
    replace emails, IPv4s and the bare username with placeholders.

    Opt-in (--anonymize): a handoff meant to continue work needs its real
    paths; one pasted into an issue report or forum doesn't."""
    home = home or _home()
    n = 0
    for probe in {str(home), home.as_posix()}:
        hits = text.count(probe)
        if hits:
            text = text.replace(probe, "~")
            n += hits
    text, k = _EMAIL_RE.subn("[EMAIL]", text)
    n += k
    text, k = _IP_RE.subn("[IP]", text)
    n += k
    user = home.name
    if len(user) >= 3:  # short names ("vi") would mangle ordinary prose
        text, k = re.subn(rf"\b{re.escape(user)}\b", "[USER]", text)
        n += k
    return text, n

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

def redact_secrets(text: str) -> tuple[str, int]:
    """Replace secret-shaped strings with [REDACTED]; returns (text, count).

    Precision-first: known key prefixes and KEY=value assignments only —
    git hashes, URLs and normal prose are left alone.
    """
    total = 0
    for pattern in SECRET_RES:
        def _sub(m: re.Match[str]) -> str:
            keep = m.group(1) if m.groups() else ""
            return keep + "[REDACTED]"
        text, n = pattern.subn(_sub, text)
        total += n
    return text, total


def redact_doc(doc: str, hint: bool = True) -> str:
    """Redact the final document — a written or pasted handoff is egress
    too, not just what goes to an LLM."""
    doc, n = redact_secrets(doc)
    if n:
        note = " (--no-redact to disable)" if hint else ""
        print(f"Redacted {n} secret-looking string(s) in the "
              f"output{note}.", file=sys.stderr)
    return doc


# --------------------------------------------------------------------------- #
#  parse
# --------------------------------------------------------------------------- #

FILE_TOOLS_WRITE = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
FILE_TOOLS_READ = {"Read"}

def load_records(path: Path) -> Iterator[dict]:
    """Yield JSONL records one at a time; corrupt lines are skipped.

    A generator, so hundreds-of-MB sessions stream instead of landing in
    memory, and early-exit consumers stop reading the file."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate partial/corrupt lines


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
            "n_agents": 0, "tok_in": 0, "tok_out": 0, "summaries": [],
        },
        "turns": [],            # {"role", "text_parts", "tools", "ts"}
        "files_written": {},    # path -> edit count
        "files_read": {},       # path -> read count
        "commands": [],
        "sidechains": [],       # {"prompt", "texts"} — subagent branches
        "_tool_names": {},      # tool_use_id -> tool name (internal)
        "_agent_descs": {},     # tool_use_id -> Agent/Task description
        "_agent_labels": {},    # agentId -> lane description (internal)
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
    if name in ("Agent", "Task"):
        desc = (block.get("input") or {}).get("description")
        if desc:  # lane label for the agent this call spawns
            state["_agent_descs"][block.get("id", "")] = desc
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
    usage = message.get("usage")
    if isinstance(usage, dict):
        try:
            state["meta"]["tok_in"] += (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0))
            state["meta"]["tok_out"] += int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            pass
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
        # gave to AskUserQuestion, which are real user input, and Agent
        # launch results, which carry the agentId → lane-label link.
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            name = state["_tool_names"].get(b.get("tool_use_id", ""))
            if name == "AskUserQuestion":
                answer = tool_result_text(b)
                if answer:
                    state["meta"]["n_user"] += 1
                    state["turns"].append({"role": "user",
                                           "text_parts": [answer],
                                           "tools": [],
                                           "ts": rec.get("timestamp")})
            elif name in ("Agent", "Task"):
                desc = state["_agent_descs"].get(b.get("tool_use_id", ""))
                m = re.search(r"agentId:\s*([0-9a-f]+)", tool_result_text(b))
                if desc and m:
                    state["_agent_labels"][m.group(1)] = desc
        return
    text = clean_text(user_text(message))
    if not text:
        return
    # /compact leaves its machine-written history summary as a "user"
    # record — keep the content, but label it and don't count it as human.
    if rec.get("isCompactSummary") or text.startswith(
            "This session is being continued from a previous conversation"):
        state["turns"].append({"role": "compact", "text_parts": [text],
                               "tools": [], "ts": rec.get("timestamp")})
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


def _drain_agent_texts(sub: dict, texts: list, drained: int) -> int:
    """Move assistant texts accumulated in a sub-parse since the last drain
    into `texts` — keeps parent steering messages chronologically placed."""
    flat = [part for turn in sub["turns"] if turn["role"] == "assistant"
            for part in turn["text_parts"]]
    texts.extend(flat[drained:])
    return len(flat)


def _parse_agent_files(session_path: Path, state: dict) -> None:
    """Fold separate-file subagent transcripts into the parse state.

    Newer Claude Code stores each subagent's transcript next to the session
    as <session-id>/subagents/agent-<id>.jsonl instead of inline isSidechain
    records. Their text becomes sidechain groups (rendered only with
    --include-sidechains); their file/command activity always counts —
    in multi-agent sessions that is where the actual work happens."""
    agents_dir = session_path.parent / session_path.stem / "subagents"
    try:
        if not agents_dir.is_dir():
            return
        agent_files = sorted(agents_dir.glob("*.jsonl"))
    except OSError:
        return
    groups = []
    for fp in agent_files:
        sub = _new_parse_state()
        prompt = None
        texts: list = []
        drained = 0
        for rec in load_records(fp):
            rtype = rec.get("type")
            if rtype == "assistant":
                _handle_assistant_record(rec, sub)
            elif rtype == "user" and not rec.get("isMeta"):
                text = clean_text(user_text(rec.get("message") or {}))
                if text and prompt is None:
                    # the agent's first user message is its task prompt
                    prompt = text
                elif text:
                    # later user text = mid-run steering from the parent
                    drained = _drain_agent_texts(sub, texts, drained)
                    texts.append(f"🧭 Parent: {text}")
            _update_envelope_meta(rec, sub["meta"])
        _drain_agent_texts(sub, texts, drained)
        if not (prompt or texts or sub["files_written"] or sub["commands"]):
            continue  # empty or foreign file
        for key in ("files_written", "files_read"):
            for f, n in sub[key].items():
                state[key][f] = state[key].get(f, 0) + n
        state["commands"].extend(sub["commands"])
        state["meta"]["n_agents"] += 1
        agent_id = fp.stem.removeprefix("agent-")
        groups.append({"prompt": one_line(prompt, 120) if prompt else None,
                       "texts": texts, "agent_id": agent_id,
                       "label": state["_agent_labels"].get(agent_id),
                       "ts": sub["meta"]["first_ts"]})
    groups.sort(key=lambda g: (g["ts"] is None, g["ts"] or ""))
    state["sidechains"].extend(groups)


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

    _parse_agent_files(path, state)
    # drop empty turns (e.g. assistant records that carried only thinking)
    state["turns"] = [t for t in state["turns"]
                      if t["text_parts"] or t["tools"]]
    for key in [k for k in state if k.startswith("_")]:
        state.pop(key)
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

def merge_parsed(parsed_list: list[dict]) -> dict:
    """Merge several parsed sessions (oldest first) into one, with a
    session-break turn opening each — for --merge project-wide handoffs."""
    merged = _new_parse_state()
    merged.pop("_tool_names")
    m = merged["meta"]
    for n, parsed in enumerate(parsed_list, 1):
        pm = parsed["meta"]
        for key in ("cwd", "git_branch", "session_id", "version"):
            m[key] = m[key] or pm[key]
        m["models"] |= pm["models"]
        m["first_ts"] = m["first_ts"] or pm["first_ts"]
        m["last_ts"] = pm["last_ts"] or m["last_ts"]
        for key in ("n_user", "n_assistant", "n_tools", "n_agents",
                    "tok_in", "tok_out"):
            m[key] += pm.get(key, 0)
        m["summaries"].extend(pm["summaries"])
        label = (pm["summaries"][-1] if pm["summaries"]
                 else f"{pm['n_user']} user messages")
        merged["turns"].append({
            "role": "session-break", "tools": [], "ts": pm["first_ts"],
            "text_parts": [f"Session {n}/{len(parsed_list)} — "
                           f"{fmt_ts(pm['first_ts'])} → "
                           f"{fmt_ts(pm['last_ts'])} · {label}"],
        })
        merged["turns"].extend(parsed["turns"])
        for fdict, key in ((parsed["files_written"], "files_written"),
                           (parsed["files_read"], "files_read")):
            for f, count in fdict.items():
                merged[key][f] = merged[key].get(f, 0) + count
        merged["commands"].extend(parsed["commands"])
        merged["sidechains"].extend(parsed.get("sidechains", []))
    return merged


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


# --------------------------------------------------------------------------- #
#  webexport
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


def _epoch_iso(t) -> str | None:
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _convo_title(convo: dict) -> str:
    return str(convo.get("name") or convo.get("title") or "")


def _convo_updated(convo: dict) -> datetime:
    """Comparable freshness of a claude.ai (ISO) or ChatGPT (epoch) convo."""
    ts = (_parse_ts(convo.get("updated_at") or convo.get("created_at"))
          or _parse_ts(_epoch_iso(convo.get("update_time")
                                  or convo.get("create_time"))))
    return ts or datetime.min.replace(tzinfo=timezone.utc)


def _chatgpt_messages(convo: dict) -> list[tuple[str, str, str | None]]:
    """(role, text, iso_ts) triples from a ChatGPT export conversation —
    walks the canonical thread backward from current_node."""
    mapping = convo.get("mapping")
    if not isinstance(mapping, dict):
        return []
    chain = []
    node = mapping.get(convo.get("current_node"))
    hops = 0
    while isinstance(node, dict) and hops < 100_000:
        chain.append(node)
        node = mapping.get(node.get("parent"))
        hops += 1
    out = []
    for node in reversed(chain):
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        role = (msg.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue  # system prompts and tool traffic are noise here
        content = msg.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "\n".join(p for p in parts
                         if isinstance(p, str)).strip() if parts else ""
        if text:
            out.append((role, text, _epoch_iso(msg.get("create_time"))))
    return out


def _claude_web_messages(convo: dict) -> list[tuple[str, str, str | None]]:
    out = []
    for msg in convo.get("chat_messages") or []:
        if not isinstance(msg, dict):
            continue
        text = _web_message_text(msg)
        if text:
            role = "user" if msg.get("sender") == "human" else "assistant"
            out.append((role, text, msg.get("created_at")))
    return out


def parse_web_export(path: Path, name_filter: str | None = None) -> dict:
    """Parse a claude.ai or ChatGPT data export (conversations.json) into
    the same shape as parse_session. Picks the newest conversation, or the
    newest whose title matches `name_filter` (case-insensitive)."""
    convos = _load_web_conversations(path)
    if name_filter:
        q = name_filter.lower()
        convos = [c for c in convos if q in _convo_title(c).lower()]
    if not convos:
        raise SystemExit(
            f"No conversation{f' matching {name_filter!r}' if name_filter else ''} "
            f"in {path}. Run with --list to see the conversations it holds.")
    convo = max(convos, key=_convo_updated)

    state = _new_parse_state()
    meta = state["meta"]
    meta["session_id"] = convo.get("uuid") or convo.get("id")
    if _convo_title(convo):
        meta["summaries"].append(_convo_title(convo))
    messages = (_claude_web_messages(convo) if "chat_messages" in convo
                else _chatgpt_messages(convo))
    for role, text, ts in messages:
        meta["n_user" if role == "user" else "n_assistant"] += 1
        meta["first_ts"] = meta["first_ts"] or ts
        meta["last_ts"] = ts or meta["last_ts"]
        state["turns"].append({"role": role, "text_parts": [text],
                               "tools": [], "ts": ts})
    for key in [k for k in state if k.startswith("_")]:
        state.pop(key)
    state.pop("sidechains", None)
    return state


def list_export_conversations(path: Path) -> None:
    for c in sorted(_load_web_conversations(path), key=_convo_updated,
                    reverse=True):
        when = _convo_updated(c).astimezone().strftime("%Y-%m-%d %H:%M")
        n = len(c.get("chat_messages") or c.get("mapping") or [])
        cid = str(c.get("uuid") or c.get("id") or "?")[:8]
        print(f"{when}  {n:>4} msgs  {cid}  "
              f"{one_line(_convo_title(c) or '(untitled)', 70)}")


# --------------------------------------------------------------------------- #
#  discovery
# --------------------------------------------------------------------------- #

PROJECTS_DIR = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")) / "projects"

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


def _raw_prefilter_ok(needle: str) -> bool:
    """True when a raw-JSONL substring scan is a safe superset test for
    `needle` — JSON escaping (\\" \\\\ \\n \\uXXXX) would hide real matches
    for quotes, backslashes, newlines, and non-ASCII text."""
    return bool(re.fullmatch(r'[ !#-\[\]-~]+', needle))


def _may_contain(path: Path, needle: str,
                 block_size: int = 1 << 20) -> bool:
    """Cheap streaming raw check before paying for a full parse.

    Binary mode on purpose: `_raw_prefilter_ok` guarantees an ASCII-only
    needle, so `bytes.lower()` is exact while skipping both the UTF-8
    decode and Unicode case-folding of megabytes of transcript (measured
    ~70% of --grep time). ASCII patterns can never false-match inside
    multibyte sequences (continuation bytes are ≥ 0x80). Known micro-gap:
    a match reachable only through a folding expansion that yields ASCII
    (İ → i̇, K → k) is missed — non-ASCII needles never take this path."""
    want = needle.encode("ascii")
    overlap = len(want) - 1
    tail = b""
    try:
        with path.open("rb") as fh:
            while True:
                block = fh.read(block_size)
                if not block:
                    return False
                low = block.lower()
                if want in low or (tail and want in tail + low[:overlap]):
                    return True
                tail = low[-overlap:] if overlap else b""
    except OSError:
        return False


def grep_sessions(pattern: str,
                  project_filter: str | None = None) -> list[tuple]:
    """Sessions whose conversation text (user/assistant turns) contains
    `pattern` (case-insensitive substring), newest first, each paired with
    a short match preview. Tool noise doesn't count — only what the human
    and the assistant actually said."""
    needle = pattern.lower()
    prefilter = _raw_prefilter_ok(needle)
    hits = []
    for path in find_sessions(project_filter):
        if prefilter and not _may_contain(path, needle):
            continue  # raw scan is a superset test — safe to skip the parse
        try:
            parsed = parse_session(path)
        except OSError:
            continue  # unreadable file must not kill the search
        for turn in parsed["turns"]:
            if turn["role"] not in ("user", "assistant"):
                continue
            text = "\n".join(turn["text_parts"])
            idx = text.lower().find(needle)
            if idx >= 0:
                start = max(0, idx - 40)
                preview = text[start:idx + len(needle) + 40]
                hits.append((path, one_line(preview, 100)))
                break
    return hits


def _session_rows(sessions: list, previews: dict) -> list:
    """Listing data shared by the text and JSON renderings of --list."""
    rows = []
    for p in sessions:
        title, prompt = session_label(p)
        row = {"path": str(p), "session_id": p.stem,
               "project": p.parent.name.lstrip("-").replace("-", "/"),
               "mtime": datetime.fromtimestamp(p.stat().st_mtime)
               .strftime("%Y-%m-%d %H:%M"),
               "size_kb": p.stat().st_size // 1024,
               "title": title, "prompt": prompt}
        if p in previews:
            row["match"] = previews[p]
        rows.append(row)
    return rows


def list_sessions(project_filter: str | None, grep: str | None = None,
                  as_json: bool = False) -> None:
    if grep:
        pairs = grep_sessions(grep, project_filter)
        previews = dict(pairs)
        sessions = [p for p, _ in pairs]
        if not sessions:
            print(f"No session text matches {grep!r} under {PROJECTS_DIR}",
                  file=sys.stderr)
            sessions = []
    else:
        previews = {}
        sessions = find_sessions(project_filter)
        if not sessions:
            print(f"No sessions found under {PROJECTS_DIR}", file=sys.stderr)
    if as_json:  # always valid JSON on stdout, even with zero sessions
        print(json.dumps(_session_rows(sessions, previews),
                         ensure_ascii=False, indent=2))
        return
    for row in _session_rows(sessions, previews):
        label = (f"{row['title']} · {row['prompt']}" if row["title"]
                 else row["prompt"])
        print(f"{row['mtime']}  {row['size_kb']:>6} KB  "
              f"{row['session_id'][:8]}  {row['project']}")
        print(f"                              └─ {one_line(label, 110)}")
        if "match" in row:
            print(f"                              🔍 {row['match']}")


# --------------------------------------------------------------------------- #
#  Parsing
# --------------------------------------------------------------------------- #

def _parse_pick(choice: str, n: int) -> list:
    """Parse a picker selection — "2", "1,3", "2-4" — into sorted
    1-based indices. Returns [] when anything is out of range or
    unparsable (the caller re-prompts)."""
    picked = set()
    for part in choice.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part.strip())
        if not m:
            return []
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= lo <= hi <= n:
            return []
        picked.update(range(lo, hi + 1))
    return sorted(picked)


def interactive_pick(project_filter: str | None,
                     grep: str | None = None) -> list:
    """Numbered session picker (-i). Terminal only. Accepts one
    selection or several ("1,3", "2-4") — several get merged."""
    if not sys.stdin.isatty():
        raise SystemExit("-i needs a terminal (stdin is piped) — use "
                         "--name/--project for scripted selection.")
    if grep:
        pairs = grep_sessions(grep, project_filter)[:15]
        sessions = [p for p, _ in pairs]
        previews = dict(pairs)
        if not sessions:
            raise SystemExit(f"No session text matches {grep!r} — "
                             f"try --any, or run --list.")
    else:
        sessions = find_sessions(project_filter)[:15]
        previews = {}
    if not sessions:
        raise SystemExit(f"No sessions found under {PROJECTS_DIR}.")
    for n, p in enumerate(sessions, 1):
        title, prompt = session_label(p)
        label = f"{title} · {prompt}" if title else prompt
        proj = p.parent.name.lstrip("-").replace("-", "/")
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        print(f" {n:>2}. {mtime}  {one_line(proj, 44)}\n"
              f"      {one_line(label, 90)}", file=sys.stderr)
        if p in previews:
            print(f"      🔍 {previews[p]}", file=sys.stderr)
    while True:
        try:
            choice = input(f"Pick session(s) [1-{len(sessions)}, "
                           f"e.g. 2 or 1,3 or 2-4; q quits]: ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("") from None
        choice = choice.strip().lower()
        if choice in ("q", "quit", ""):
            raise SystemExit("")
        idxs = _parse_pick(choice, len(sessions))
        if idxs:
            return [sessions[i - 1] for i in idxs]
        print("Try e.g. 2, 1,3 or 2-4 (or q).", file=sys.stderr)


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


# --------------------------------------------------------------------------- #
#  render
# --------------------------------------------------------------------------- #

# Message-level caps (deterministic mode). Head+tail are kept when truncating.
USER_MSG_CAP = 8000
ASSISTANT_MSG_CAP = 5000
TOOL_LINE_CAP = 200
DEFAULT_MAX_CHARS = 80_000       # global cap on the transcript section
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
    ]
    if m.get("tok_in") or m.get("tok_out"):
        lines.append(f"- **Tokens:** {m['tok_in']:,} in (incl. cache) / "
                     f"{m['tok_out']:,} out")
    lines.append(f"- **Source:** `{source}`")
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
    n_agents = parsed["meta"].get("n_agents", 0)
    if n_agents:
        out += [f"_🤖 {n_agents} subagent(s) contributed to the work above "
                f"(--include-sidechains for their transcripts)._", ""]
    return "\n".join(out).rstrip()


def render_transcript(parsed: dict, include_tools: bool,
                      max_chars: int) -> str:
    blocks = []
    for turn in parsed["turns"]:
        text = "\n\n".join(turn["text_parts"]).strip()
        if turn["role"] == "session-break":
            blocks.append(f"### ⏱ {text}")
        elif turn["role"] == "compact":
            blocks.append("### 📜 Compacted history (auto-summary of the "
                          "earlier part of this session)\n\n"
                          + truncate(text, USER_MSG_CAP))
        elif turn["role"] == "user":
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
        label = g.get("label")
        prompt = g["prompt"] or "(task prompt not recorded)"
        out.append(f"### Subagent: {label or prompt}")
        out.append("")
        if label and g["prompt"]:
            out.append(f"Task: {g['prompt']}")
            out.append("")
        out.append(truncate("\n\n".join(g["texts"]), max_each))
        out.append("")
    return "\n".join(out).rstrip()


def render_footer() -> str:
    return ("---\n\n_Exported with [claude-handoff]"
            "(https://github.com/Vasilispapg/claude-handoff) — continue from here._")


def build_json(parsed: dict, source: Path, summary: str | None) -> str:
    """Machine-readable handoff (--format json)."""
    m = dict(parsed["meta"])
    m["models"] = sorted(m["models"])
    payload = {
        "generator": f"claude-handoff {__version__}",
        "source": str(source),
        "meta": m,
        "activity": {
            "files_written": parsed["files_written"],
            "files_read": parsed["files_read"],
            "commands": parsed["commands"],
        },
        "turns": [{"role": t["role"],
                   "text": "\n\n".join(t["text_parts"]).strip(),
                   "tools": t["tools"], "ts": t.get("ts")}
                  for t in parsed["turns"]],
        "sidechains": parsed.get("sidechains", []),
    }
    if summary is not None:
        payload["summary"] = summary
    if parsed.get("slice_note"):
        payload["slice_note"] = parsed["slice_note"]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


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


# --------------------------------------------------------------------------- #
#  llm
# --------------------------------------------------------------------------- #

LLM_INPUT_CAP = 400_000          # max transcript chars for a single LLM pass
CHUNK_CAP = 200_000              # chunk size for map-reduce over huge sessions

# Subprocess/local backends must not run chunks concurrently (nested claude
# CLIs conflict; a local Ollama box chokes); API providers fan out fine.
SERIAL_PROVIDERS = {"claude-cli", "ollama"}
PARALLEL_WORKERS = 4

# Chunk-note cache: failed/interrupted map-reduce runs resume for free, and
# re-runs (e.g. with a different --focus) reuse paid-for chunk notes.
CACHE_DIR = Path(os.environ.get(
    "CLAUDE_HANDOFF_CACHE", str(Path.home() / ".cache" / "claude-handoff")))
CACHE_VERSION = "1"              # bump when CHUNK_PROMPT changes

# LLM provider registry for --llm — see the "LLM summarization" section.
# Adding a provider = one entry here + one _call_* function; nothing else
# changes (open/closed). Populated after the call functions are defined.
PROVIDERS: dict = {}

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


def _resolve_provider(provider: str, model: str | None) -> tuple:
    """Registry lookup + key/model resolution shared by every LLM
    entry point; exits naming the accepted env vars when a key is
    missing."""
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise SystemExit(f"Unknown provider: {provider}. "
                         f"Available: {', '.join(sorted(PROVIDERS))}")
    key = provider_key(provider)
    if cfg["env_keys"] and not key:
        accepted = " or ".join(cfg["env_keys"])
        raise SystemExit(f"Set {accepted} to use --llm {provider}")
    return cfg, key, model or cfg["default_model"]


def llm_summarize(provider: str, model: str | None, transcript: str,
                  focus: str | None = None, use_cache: bool = True) -> str:
    """Summarize a transcript with the provider's call strategy.

    Transcripts beyond LLM_INPUT_CAP are map-reduced: per-chunk notes
    first (cached, retried), then one synthesis pass — nothing is
    silently dropped. `focus` carries extra user instructions.
    """
    cfg, key, model = _resolve_provider(provider, model)
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
    serial = provider in SERIAL_PROVIDERS
    workers = 1 if serial else min(PARALLEL_WORKERS, len(chunks))
    print(f"Transcript is {len(transcript):,} chars — map-reduce over "
          f"{len(chunks)} chunks (up to {len(chunks) + 1} LLM calls"
          + ("" if workers == 1 else f", {workers} in parallel") + ").",
          file=sys.stderr)
    st = _new_progress(len(chunks) + 1)  # + the reduce pass
    notes: list = [None] * len(chunks)
    todo: list = []
    for i, chunk in enumerate(chunks):
        # Focus is applied only in the reduce pass, so chunk notes stay
        # reusable across runs with different --focus.
        prompt = (CHUNK_PROMPT.format(i=i + 1, n=len(chunks))
                  + "\nPART:\n" + chunk)
        cache_file = _chunk_cache_path(chunk, provider, model)
        cached = _cache_get(cache_file) if use_cache else None
        if cached is not None:
            print(f"  part {i + 1}/{len(chunks)} — cached.", file=sys.stderr)
            notes[i] = cached
            st["done"] += 1
        else:
            todo.append((i, prompt, cache_file))

    if workers == 1 or len(todo) <= 1:
        for i, prompt, cache_file in todo:
            result = _progress_step(
                st, f"summarizing part {i + 1}/{len(chunks)} "
                    f"({len(chunks[i]):,} chars)…",
                lambda p=prompt: _call_with_retry(cfg["call"], key, model, p))
            if use_cache:
                _cache_put(cache_file, result)
            notes[i] = result
            st["done"] += 1
    else:
        lock = threading.Lock()

        def _one(item: tuple) -> None:
            i, prompt, cache_file = item
            result = _call_with_retry(cfg["call"], key, model, prompt)
            if use_cache:
                _cache_put(cache_file, result)
            with lock:
                notes[i] = result
                st["done"] += 1
                if st["tty"]:
                    _draw_progress(st, f"part {i + 1}/{len(chunks)} done")
                else:
                    print(f"  part {i + 1}/{len(chunks)} done.",
                          file=sys.stderr)

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers) as pool:
            futures = [pool.submit(_one, item) for item in todo]
            for fut in concurrent.futures.as_completed(futures):
                fut.result()  # re-raises worker failures (finished
                #               chunks are already in the cache)
        if st["tty"]:
            print(file=sys.stderr)
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


# --------------------------------------------------------------------------- #
#  brief
# --------------------------------------------------------------------------- #

BRIEFS_DIR = Path(os.environ.get("CLAUDE_HOME",
                                 str(Path.home() / ".claude"))) / "briefs"


def brief_path(project_encoded: str) -> Path:
    """Where the brief for one project lives (keyed like ~/.claude/projects)."""
    return BRIEFS_DIR / f"{project_encoded}.md"


def _session_line(parsed: dict) -> str:
    """One timeline bullet: date, short id, title (or first prompt)."""
    meta = parsed["meta"]
    title = meta["summaries"][-1] if meta["summaries"] else None
    if not title:
        texts = [t for t in parsed["turns"] if t["role"] == "user"]
        title = texts[0]["text_parts"][0] if texts else "(no prompt)"
    sid = (meta["session_id"] or "?")[:8]
    extras = [f"{meta['n_user']} msgs"]
    if parsed["files_written"]:
        extras.append(f"{len(parsed['files_written'])} files")
    if meta.get("n_agents"):
        extras.append(f"{meta['n_agents']} agents")
    return (f"- **{fmt_ts(meta['first_ts'])}** `{sid}` — "
            f"{one_line(title, 90)} ({', '.join(extras)})")


def _activity_rollup(parsed_list: list, top: int = 15) -> list:
    """Most-edited files across every session, most edits first."""
    totals: dict = {}
    for parsed in parsed_list:
        for f, n in parsed["files_written"].items():
            totals[f] = totals.get(f, 0) + n
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def build_brief_deterministic(parsed_list: list, label: str) -> str:
    """No-LLM digest: timeline of every session + cross-session activity.
    The --llm variant distills decisions/conventions; this is the honest,
    free fallback and the skeleton both share."""
    parsed_list = sorted(parsed_list,
                         key=lambda p: p["meta"]["first_ts"] or "")
    shown = parsed_list[-TIMELINE_CAP:]
    first = min((p["meta"]["first_ts"] or "9999" for p in parsed_list))
    last = max((p["meta"]["last_ts"] or "" for p in parsed_list))
    out = [f"# Project brief: {label}", "",
           f"_Distilled from {len(parsed_list)} sessions, "
           f"{fmt_ts(first)} → {fmt_ts(last)} — by claude-handoff. "
           f"Citations are session ids (`--name ID` opens one)._", "",
           "## Session timeline", ""]
    if len(parsed_list) > len(shown):
        out.append(f"_{len(parsed_list) - len(shown)} earlier "
                   f"session(s) omitted — chf --list for all._")
        out.append("")
    out += [_session_line(p) for p in shown]
    rollup = _activity_rollup(parsed_list)
    if rollup:
        out += ["", "## Most-touched files", ""]
        out += [f"- `{f}`" + (f" ({n}× edits)" if n > 1 else "")
                for f, n in rollup]
    return "\n".join(out) + "\n"


SESSION_NOTE_PROMPT = """\
You are distilling ONE Claude Code working session into compact notes for
a long-term project memory. Write terse bullets under these headings,
skipping any heading the session has nothing for:

## Decisions
## Fixed
## Conventions
## Open threads

Rules: only what the transcript actually shows — never invent or
embellish; end every bullet with the session citation `[{sid}]`; at most
200 words total; answer in the language the user wrote in.

SESSION {sid} ({when}):
{transcript}
"""

BRIEF_PROMPT = """\
You are composing the persistent memory brief of the project "{label}"
from per-session notes. Merge duplicates; when notes conflict, the later
session wins. Organize under exactly these headings:

## Decisions
## Fixed
## Conventions
## Open threads

Rules: keep the session citations like [abc123] on every bullet; never
invent anything not present in the notes; at most 600 words total; answer
in the language the notes are written in.
{focus}
NOTES (oldest session first):
{notes}
"""

NOTE_INPUT_CAP = 120_000     # per-session transcript budget for one note
BRIEF_NOTES_CAP = 200_000    # reduce-pass budget for notes (never the rules)


def _sid(parsed: dict) -> str:
    return (parsed["meta"]["session_id"] or "?")[:8]


def _session_note(parsed: dict, provider: str, model: str | None,
                  redact: bool, use_cache: bool) -> str:
    """One cached, retried LLM note for one session. The cache key hashes
    prompt+transcript, so prompt changes or session growth re-note only
    what actually changed — that is what makes --brief refreshes cheap."""
    transcript = (render_activity(parsed) + "\n\n"
                  + render_transcript(parsed, include_tools=True,
                                      max_chars=NOTE_INPUT_CAP))
    if redact:
        transcript, _ = redact_secrets(transcript)
    prompt = SESSION_NOTE_PROMPT.format(
        sid=_sid(parsed), when=fmt_ts(parsed["meta"]["first_ts"]),
        transcript=transcript)
    cfg, key, model = _resolve_provider(provider, model)
    cache = _chunk_cache_path(prompt, provider, model) if use_cache else None
    if cache is not None:
        hit = _cache_get(cache)
        if hit is not None:
            return hit
    note = _call_with_retry(cfg["call"], key, model, prompt)
    if cache is not None:
        _cache_put(cache, note)
    return note


def _map_notes(parsed_list: list, provider: str, model: str | None,
               redact: bool, use_cache: bool) -> list:
    """A note per session — sequential for SERIAL_PROVIDERS, fanned out
    for API providers (same split as the chunk pipeline)."""
    notes: list = [None] * len(parsed_list)
    done = [0]

    def work(i: int) -> None:
        notes[i] = _session_note(parsed_list[i], provider, model,
                                 redact, use_cache)
        done[0] += 1
        print(f"brief note {done[0]}/{len(parsed_list)} "
              f"({_sid(parsed_list[i])})", file=sys.stderr)

    if provider in SERIAL_PROVIDERS:
        for i in range(len(parsed_list)):
            work(i)
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=PARALLEL_WORKERS) as ex:
            list(ex.map(work, range(len(parsed_list))))
    return notes


def build_brief_llm(parsed_list: list, label: str, provider: str,
                    model: str | None, focus: str | None = None,
                    redact: bool = True, use_cache: bool = True) -> str:
    """Deterministic skeleton + LLM-distilled memory sections."""
    notes = _map_notes(parsed_list, provider, model, redact, use_cache)
    joined = "\n\n".join(f"--- session {_sid(p)} ---\n{n}"
                         for p, n in zip(parsed_list, notes))
    focus_line = (f"\nAdditional instructions from the user — follow them "
                  f"as well:\n{focus.strip()}\n" if focus else "")
    prompt = BRIEF_PROMPT.format(label=label, focus=focus_line,
                                 notes=truncate(joined, BRIEF_NOTES_CAP))
    cfg, key, model = _resolve_provider(provider, model)
    distilled = _call_with_retry(cfg["call"], key, model, prompt)
    return (build_brief_deterministic(parsed_list, label)
            + "\n## Distilled memory\n\n" + distilled.strip() + "\n")


TIMELINE_CAP = 20            # timeline bullets injected per session start

STAMP_RE = re.compile(
    r"<!-- claude-handoff-brief v=1 built=(\d+) sessions=(\d+) "
    r"newest_mtime=(\d+) distilled=(\d+) distilled_sessions=(\d+) "
    r"provider=(\S+) -->")
DISTILLED_MARK = "\n## Distilled memory\n"
_FRESHNESS_NOTE_RE = re.compile(
    r"\n_\d+ newer session\(s\) since this distillation[^\n]*_\n")


def make_stamp(sessions: int, newest_mtime: int, distilled: int,
               distilled_sessions: int, provider: str,
               now: int | None = None) -> str:
    """Machine-readable freshness marker embedded in the brief file —
    what the SessionStart/SessionEnd hooks use to reason about staleness."""
    built = int(time.time()) if now is None else int(now)
    return (f"<!-- claude-handoff-brief v=1 built={built} "
            f"sessions={sessions} newest_mtime={int(newest_mtime)} "
            f"distilled={int(distilled)} "
            f"distilled_sessions={int(distilled_sessions)} "
            f"provider={provider} -->")


def parse_stamp(text: str) -> dict | None:
    m = STAMP_RE.search(text)
    if not m:
        return None
    built, sessions, newest, dist, dsess = (int(g) for g in m.groups()[:5])
    return {"built": built, "sessions": sessions, "newest_mtime": newest,
            "distilled": dist, "distilled_sessions": dsess,
            "provider": m.group(6)}


def brief_label(parsed_list: list, scope: str) -> str:
    """Human title: the real cwd when sessions carry one, else decoded."""
    return next((p["meta"]["cwd"] for p in parsed_list if p["meta"]["cwd"]),
                scope.lstrip("-").replace("-", "/"))


def load_project_sessions(project: str) -> tuple:
    """(parsed sessions oldest-first minus trivial ones, newest mtime)."""
    sessions = find_sessions(project)
    parsed_list = [p for p in (parse_session(s) for s in reversed(sessions))
                   if p["turns"] and not looks_trivial(p)]
    newest = max((s.stat().st_mtime for s in sessions), default=0)
    return parsed_list, int(newest)


def split_distilled(text: str) -> str | None:
    """The distilled section (marker included) of an existing brief."""
    idx = text.find(DISTILLED_MARK)
    return text[idx:] if idx >= 0 else None


def update_brief_skeleton(project: str) -> bool:
    """SessionEnd refresh: rebuild the factual skeleton of an EXISTING
    stamped brief, preserving the distilled section with a freshness note.
    Touches nothing (returns False) for missing or unstamped files and
    empty projects — the hook must never create surprises, and building a
    brief is always an explicit user command."""
    path = brief_path(project)
    if not path.is_file():
        return False
    old = path.read_text(encoding="utf-8")
    stamp = parse_stamp(old)
    if stamp is None:
        return False
    parsed_list, newest = load_project_sessions(project)
    if not parsed_list:
        return False
    doc = build_brief_deterministic(parsed_list,
                                    brief_label(parsed_list, project))
    distilled = split_distilled(old)
    if distilled is not None:
        distilled = _FRESHNESS_NOTE_RE.sub("\n", distilled)
        newer = len(parsed_list) - stamp["distilled_sessions"]
        if stamp["distilled"] and newer > 0:
            note = (f"\n_{newer} newer session(s) since this distillation "
                    f"— refresh with `chf --brief --llm "
                    f"{stamp['provider']}`._\n")
            distilled = distilled.replace(DISTILLED_MARK,
                                          DISTILLED_MARK + note, 1)
        doc = doc + distilled
    new_stamp = make_stamp(len(parsed_list), newest, stamp["distilled"],
                           stamp["distilled_sessions"], stamp["provider"])
    path.write_text(new_stamp + "\n" + doc, encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
#  integrations
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
#  MCP server mode (claude-handoff --mcp)
# ------------------------------------------------------------------------- #

MCP_PROTOCOL = "2025-06-18"


def _mcp_tools(allow_llm: bool = False) -> list[dict]:
    return [
        {"name": "list_sessions",
         "description": "List Claude Code sessions on this machine, newest "
                        "first: date, project, id, title and first prompt.",
         "inputSchema": {"type": "object", "properties": {
             "project": {"type": "string",
                         "description": "substring filter on the project "
                                        "path"}}}},
        {"name": "handoff",
         "description": "Build a clean handoff document (markdown) from a "
                        "Claude Code session so another assistant can "
                        "continue the work. Deterministic — no LLM calls, "
                        "no cost."
                        + (" Pass llm for an LLM-written summary "
                           "(this server allows it)." if allow_llm
                           else ""),
         "inputSchema": {"type": "object", "properties": {
             "name": {"type": "string",
                      "description": "newest session whose title or first "
                                     "prompt contains this"},
             "project": {"type": "string"},
             "path": {"type": "string",
                      "description": "explicit session .jsonl path"},
             "last": {"type": "integer",
                      "description": "keep only the last N user turns"},
             "include_tools": {"type": "boolean"},
             "anonymize": {"type": "boolean"},
             **({"llm": {"type": "string",
                         "description": "provider for an LLM-written "
                                        "summary: claude-cli, claude, "
                                        "openai, gemini or ollama"},
                 "model": {"type": "string"},
                 "focus": {"type": "string"}} if allow_llm else {})}}},
    ]


def _mcp_call(name: str, arguments: dict | None,
              allow_llm: bool = False) -> str:
    a = arguments or {}
    if name == "list_sessions":
        lines = []
        for p in find_sessions(a.get("project"))[:30]:
            title, prompt = session_label(p)
            mtime = datetime.fromtimestamp(p.stat().st_mtime) \
                .strftime("%Y-%m-%d %H:%M")
            proj = p.parent.name.lstrip("-").replace("-", "/")
            label = f"{title} · {prompt}" if title else prompt
            lines.append(f"{mtime} | {proj} | {p.stem[:8]} | {label}")
        return "\n".join(lines) or "No sessions found."
    if name == "handoff":
        if a.get("path"):
            source = Path(str(a["path"])).expanduser()
            if not source.is_file():
                raise ValueError(f"not a file: {source}")
        elif a.get("name"):
            matches = find_session_by_name(str(a["name"]), a.get("project"))
            if not matches:
                raise ValueError(f"no session matches {a['name']!r}")
            source = matches[0]
        else:
            sessions = find_sessions(a.get("project"))
            if not sessions:
                raise ValueError("no sessions found")
            source = _newest_meaningful_session(sessions)
        parsed = (parse_web_export(source) if is_web_export(source)
                  else parse_session(source))
        if not parsed["turns"]:
            raise ValueError("session has no conversation turns")
        if a.get("last"):
            slice_turns(parsed, last=int(a["last"]))
        if a.get("llm"):
            if not allow_llm:
                raise ValueError(
                    "LLM summaries are disabled — start the server "
                    "with --allow-llm to enable them")
            doc = redact_doc(build_llm(
                parsed, source, str(a["llm"]), a.get("model"),
                with_transcript=False, max_chars=DEFAULT_MAX_CHARS,
                focus=a.get("focus")), hint=False)
        else:
            doc = redact_doc(build_deterministic(
                parsed, source, include_tools=bool(a.get("include_tools")),
                max_chars=DEFAULT_MAX_CHARS), hint=False)
        if a.get("anonymize"):
            doc, _ = anonymize_text(doc)
        return doc
    raise ValueError(f"unknown tool: {name}")


def run_mcp_server(allow_llm: bool = False) -> None:
    """Minimal MCP server over stdio: newline-delimited JSON-RPC 2.0.
    stdout carries only protocol messages; logs go to stderr."""

    def reply(mid, result=None, error=None) -> None:
        if mid is None:
            return
        out: dict = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        try:
            if method == "initialize":
                params = msg.get("params") or {}
                reply(mid, {
                    "protocolVersion": params.get("protocolVersion")
                    or MCP_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "claude-handoff",
                                   "version": __version__}})
            elif method == "tools/list":
                reply(mid, {"tools": _mcp_tools(allow_llm)})
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    text = _mcp_call(params.get("name"),
                                     params.get("arguments"),
                                     allow_llm=allow_llm)
                    reply(mid, {"content": [{"type": "text", "text": text}],
                                "isError": False})
                except Exception as e:  # tool errors → isError result
                    reply(mid, {"content": [{"type": "text",
                                             "text": f"error: {e}"}],
                                "isError": True})
            elif method == "ping":
                reply(mid, {})
            elif method is None or method.startswith("notifications/"):
                pass
            else:
                reply(mid, error={"code": -32601,
                                  "message": f"method not found: {method}"})
        except Exception as e:  # protocol must never die
            reply(mid, error={"code": -32603, "message": str(e)})


# ------------------------------------------------------------------------- #
#  Auto-handoff hook (claude-handoff --install-hook)
# ------------------------------------------------------------------------- #

HOOK_COMMAND = "claude-handoff --hook-stdin"
HANDOFFS_DIR = Path(os.environ.get("CLAUDE_HOME",
                                   str(Path.home() / ".claude"))) / "handoffs"


def _edit_hook_settings(event: str, command: str, matcher: str | None,
                        settings_path: Path | None, remove: bool) -> tuple:
    """Shared add/remove of one of our hook commands in settings.json —
    existing settings always preserved, malformed ones never clobbered."""
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
    entries = hooks.setdefault(event, [])

    def _is_ours(h: dict) -> bool:
        return h.get("command") == command

    if remove:
        for entry in entries:
            entry["hooks"] = [h for h in entry.get("hooks", [])
                              if not _is_ours(h)]
        hooks[event] = [e for e in entries if e.get("hooks")]
        if not hooks[event]:
            del hooks[event]
        if not hooks:
            del settings["hooks"]
        action = "removed from"
    else:
        present = any(_is_ours(h) for e in entries
                      for h in e.get("hooks", []))
        if not present:
            entry: dict = {"hooks": [{"type": "command",
                                      "command": command}]}
            if matcher:
                entry["matcher"] = matcher
            entries.append(entry)
        action = "installed in"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2,
                                        ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return settings_path, action


def install_hook(settings_path: Path | None = None,
                 remove: bool = False) -> None:
    """Add (or remove) a SessionEnd hook in Claude Code settings.json so
    every session leaves a handoff in ~/.claude/handoffs automatically."""
    settings_path, action = _edit_hook_settings(
        "SessionEnd", HOOK_COMMAND, None, settings_path, remove)
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
        out.write_text(redact_doc(build_deterministic(
            parsed, transcript, include_tools=False,
            max_chars=DEFAULT_MAX_CHARS), hint=False), encoding="utf-8")
        print(f"handoff written: {out}")
    except Exception:  # deliberately swallow: see docstring
        return




BRIEF_HOOK_COMMAND = "claude-handoff --brief-hook-stdin"
BRIEF_UPDATE_COMMAND = "claude-handoff --brief-update-stdin"


def install_brief_hook(settings_path: Path | None = None,
                       remove: bool = False) -> None:
    """Add (or remove) a SessionStart hook that injects the project brief
    (~/.claude/briefs/<project>.md) as context into every new session —
    long-term project memory, fully local."""
    settings_path, action = _edit_hook_settings(
        "SessionStart", BRIEF_HOOK_COMMAND,
        "startup|resume|clear|compact", settings_path, remove)
    _edit_hook_settings("SessionEnd", BRIEF_UPDATE_COMMAND, None,
                        settings_path, remove)
    print(f"Project-memory hook {action} {settings_path}.",
          file=sys.stderr)
    if not remove:
        print("New sessions now start with the project brief as context — "
              "refresh it anytime with `chf --brief` (--llm for a "
              "distilled one).", file=sys.stderr)


def run_brief_hook_mode() -> None:
    """SessionStart hook entrypoint: print the project brief to stdout —
    Claude Code adds plain stdout as session context. Silent on any
    problem: a hook must never break the host session."""
    try:
        payload = json.load(sys.stdin)
        project = cwd_project_filter(Path(payload["cwd"]))
        if not project:
            return
        path = brief_path(project)
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        stamp = parse_stamp(text)
        warn = ""
        if stamp:
            newest = max((s.stat().st_mtime
                          for s in find_sessions(project)), default=0)
            if newest > stamp["newest_mtime"]:
                warn = ("\n(warning: sessions newer than this brief "
                        "exist — refresh with `chf --brief`)\n")
        sys.stdout.write(
            '<project-memory source="claude-handoff" '
            'refresh="chf --brief">\n'
            + text + warn + "\n</project-memory>\n")
    except Exception:  # deliberately swallow: see docstring
        return



def run_brief_update_mode() -> None:
    """SessionEnd hook entrypoint: refresh the factual skeleton of an
    existing brief (never an LLM call, never creates files). Silent on
    any problem — a hook must never break the host session."""
    try:
        payload = json.load(sys.stdin)
        project = cwd_project_filter(Path(payload["cwd"]))
        if project:
            update_brief_skeleton(project)
    except Exception:  # deliberately swallow: see docstring
        return


# --------------------------------------------------------------------------- #
#  cli
# --------------------------------------------------------------------------- #

def print_completions(shell: str) -> None:
    """Emit a shell-completion snippet (works in bash and zsh)."""
    flags = sorted({opt for action in build_arg_parser()._actions
                    for opt in action.option_strings})
    words = " ".join(flags)
    print(f"# claude-handoff completions ({shell})")
    if shell == "zsh":
        print("# add to ~/.zshrc:  eval \"$(claude-handoff --completions zsh)\"")
        print("autoload -U +X bashcompinit && bashcompinit")
    else:
        print("# add to ~/.bashrc: eval \"$(claude-handoff --completions bash)\"")
    print(f'complete -W "{words}" claude-handoff chf')


class _HelpfulParser(argparse.ArgumentParser):
    """argparse's designed extension point: on any usage error, point the
    user at --help and --list instead of leaving them with a bare error."""

    def error(self, message: str) -> None:
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
    ap.add_argument("--grep", metavar="TEXT",
                    help="pick newest session whose conversation contains "
                         "TEXT (case-insensitive; combines with --list/-i "
                         "to show all matches)")
    ap.add_argument("--project", metavar="NAME",
                    help="pick latest session whose project path contains NAME")
    ap.add_argument("--brief", action="store_true",
                    help="distill EVERY session of the project into one "
                         "memory brief (~/.claude/briefs/<project>.md); "
                         "deterministic timeline by default, real "
                         "distillation with --llm")
    ap.add_argument("--merge", action="store_true",
                    help="merge every session in scope (project / cwd / "
                         "--name match) into ONE handoff, oldest first")
    ap.add_argument("--format", choices=["md", "json"], default="md",
                    help="output format (default md; json is machine-"
                         "readable and also applies to --list)")
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
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="pick the session from a numbered list")
    ap.add_argument("--install-brief-hook", action="store_true",
                    help="inject the project brief as context into "
                         "every new Claude Code session (SessionStart "
                         "hook) — long-term project memory")
    ap.add_argument("--uninstall-brief-hook", action="store_true",
                    help="remove the project-memory hook")
    ap.add_argument("--brief-hook-stdin", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--brief-update-stdin", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--install-hook", action="store_true",
                    help="auto-write a handoff when every Claude Code "
                         "session ends (SessionEnd hook)")
    ap.add_argument("--uninstall-hook", action="store_true",
                    help="remove the auto-handoff hook")
    ap.add_argument("--completions", choices=["bash", "zsh"],
                    help="print a shell-completion snippet and exit")
    ap.add_argument("--mcp", action="store_true",
                    help="run as an MCP server over stdio (tools: "
                         "list_sessions, handoff)")
    ap.add_argument("--allow-llm", action="store_true",
                    help="with --mcp: let the handoff tool run LLM "
                         "summaries (explicit opt-in — clients can "
                         "then trigger paid/API calls)")
    ap.add_argument("--hook-stdin", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--max-chars", type=int, default=None,
                    help=f"cap transcript section size (default {DEFAULT_MAX_CHARS})")
    ap.add_argument("--fit", metavar="TOKENS", type=_parse_budget,
                    help="size the deterministic handoff to a token budget "
                         "(e.g. 32k, 128k, 1m) by tightening transcript "
                         "truncation; not combinable with --llm/--max-chars")
    ap.add_argument("--llm", choices=sorted(PROVIDERS),
                    help="summarize with an LLM instead of deterministic export; "
                         "claude-cli uses your local Claude Code login, no API key")
    ap.add_argument("--model", help="override the LLM model id for --llm")
    ap.add_argument("--focus", metavar="TEXT",
                    help="with --llm: extra instructions for the summary "
                         "(e.g. --focus \"emphasize the API decisions\")")
    ap.add_argument("--with-transcript", action="store_true",
                    help="with --llm: also append the cleaned transcript")
    ap.add_argument("--anonymize", action="store_true",
                    help="strip identity for public sharing: home paths → ~, "
                         "emails/IPs/username → placeholders")
    ap.add_argument("--no-redact", action="store_true",
                    help="keep secret-looking strings (default: redacted "
                         "from every output, LLM or not)")
    ap.add_argument("--no-cache", action="store_true",
                    help="with --llm: disable the chunk-note cache "
                         f"({CACHE_DIR})")
    ap.add_argument("--version", action="version", version=__version__)
    return ap


def resolve_source(args: argparse.Namespace) -> Path:
    """The session file to export: explicit path, picker, name match, or
    newest in scope."""
    if args.grep:
        if args.session or args.name:
            raise SystemExit("--grep searches content on its own — combine "
                             "it with --project/--any, not a path or --name.")
        scope = args.project
        if not scope and not args.any:
            scope = cwd_project_filter()
        hits = grep_sessions(args.grep, scope)
        if not hits:
            raise SystemExit(f"No session text matches {args.grep!r} — try "
                             f"--any for all projects, or run --list.")
        source, preview = hits[0]
        print(f"Using session {source.stem[:8]} — 🔍 {preview}",
              file=sys.stderr)
        return source
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


def _parse_budget(spec: str) -> int:
    """'32k' / '1m' / '128000' → token count (argparse type for --fit)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", spec.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"cannot parse token budget {spec!r} — use e.g. 32k, 128k, 1m")
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return int(float(m.group(1)) * mult)


def _fmt_tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k"


def _fit_transcript_cap(parsed: dict, source: Path,
                        args: argparse.Namespace, tokens: int) -> int:
    """Char cap for the transcript so the whole document roughly fits
    `tokens` (≈4 chars/token). Fixed sections are measured, never trimmed."""
    overhead = sum(len(s) + 2 for s in (
        render_header(parsed, source),
        render_activity(parsed),
        render_sidechains(parsed) if args.include_sidechains else "",
        render_footer()))
    return max(2000, tokens * 4 - overhead)


def build_document(parsed: dict, source: Path,
                   args: argparse.Namespace) -> str:
    """Markdown or JSON document, deterministic or LLM-summarized."""
    max_chars = DEFAULT_MAX_CHARS if args.max_chars is None else args.max_chars
    if args.format == "json":
        summary = None
        if args.llm:
            outbound = (render_activity(parsed) + "\n\n"
                        + render_transcript(parsed, include_tools=True,
                                            max_chars=10**9))
            if not args.no_redact:
                outbound, n_red = redact_secrets(outbound)
                if n_red:
                    print(f"Redacted {n_red} secret-looking string(s).",
                          file=sys.stderr)
            summary = llm_summarize(args.llm, args.model, outbound,
                                    focus=args.focus,
                                    use_cache=not args.no_cache)
        doc = build_json(parsed, source, summary)
    elif args.llm:
        doc = build_llm(parsed, source, args.llm, args.model,
                        args.with_transcript, max_chars,
                        focus=args.focus, redact=not args.no_redact,
                        use_cache=not args.no_cache)
    else:
        if getattr(args, "fit", None):
            max_chars = _fit_transcript_cap(parsed, source, args, args.fit)
        doc = build_deterministic(parsed, source, args.include_tools,
                                  max_chars,
                                  include_sidechains=args.include_sidechains)
    if not args.no_redact:
        doc = redact_doc(doc)
    if getattr(args, "anonymize", False):
        doc, n_anon = anonymize_text(doc)
        if n_anon:
            print(f"Anonymized {n_anon} identifying string(s) — home paths, "
                  f"emails, IPs, username.", file=sys.stderr)
    return doc


def write_output(doc: str, parsed: dict, args: argparse.Namespace) -> None:
    tok = f"≈{_fmt_tokens(len(doc) // 4)} tokens"
    if getattr(args, "fit", None):
        tok += f" (target {_fmt_tokens(args.fit)})"
    if args.output == "-":
        sys.stdout.write(doc)
        if getattr(args, "fit", None):
            print(f"Handoff {tok}.", file=sys.stderr)
        return
    if args.output in ("clipboard", "clip"):
        tool = _copy_clipboard(doc)
        print(f"Copied to clipboard via {tool} ({len(doc):,} chars, {tok}, "
              f"{parsed['meta']['n_user']} user messages"
              f"{', LLM-summarized' if args.llm else ''}) — paste away.",
              file=sys.stderr)
        return
    out = Path(args.output)
    out.write_text(doc, encoding="utf-8")
    n_user = parsed["meta"]["n_user"]
    print(f"Wrote {out} ({len(doc):,} chars, {tok}, {n_user} user messages"
          f"{', LLM-summarized' if args.llm else ''})", file=sys.stderr)


_CONFIG_KEYS = {"llm", "model", "fit", "output", "include_tools",
                "include_sidechains", "max_chars", "anonymize",
                "focus"}


def _config_path() -> Path:
    env = os.environ.get("CLAUDE_HANDOFF_CONFIG")
    return (Path(env) if env
            else Path.home() / ".config" / "claude-handoff"
            / "config.json")


def _load_config() -> dict:
    """Defaults from ~/.config/claude-handoff/config.json; CLI flags
    win. Security switches (no_redact) are deliberately NOT
    configurable — weakening redaction must be explicit per run.
    A broken config warns and is ignored, never fatal."""
    path = _config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except json.JSONDecodeError as e:
        print(f"Ignoring malformed config {path}: {e}", file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        print(f"Ignoring config {path}: expected a JSON object.",
              file=sys.stderr)
        return {}
    cfg = {}
    for key, value in raw.items():
        if key not in _CONFIG_KEYS:
            print(f"Ignoring config key {key!r} (allowed: "
                  f"{', '.join(sorted(_CONFIG_KEYS))}).",
                  file=sys.stderr)
            continue
        if key == "fit":
            try:
                value = _parse_budget(str(value))
            except argparse.ArgumentTypeError as e:
                print(f"Ignoring config fit: {e}", file=sys.stderr)
                continue
        cfg[key] = value
    return cfg


def _run_brief(args: argparse.Namespace) -> None:
    """--brief: whole-project memory document (see brief.py)."""
    scope = args.project
    if not scope and not args.any:
        scope = cwd_project_filter()
    if not scope:
        raise SystemExit("--brief needs a project: run it inside one, "
                         "or pass --project NAME.")
    parsed_list, newest_mtime = load_project_sessions(scope)
    if not parsed_list:
        raise SystemExit(f"No sessions to brief for {scope!r} — "
                         f"run --list.")
    label = brief_label(parsed_list, scope)
    if args.llm:
        doc = build_brief_llm(parsed_list, label, args.llm,
                              args.model, focus=args.focus,
                              redact=not args.no_redact,
                              use_cache=not args.no_cache)
    else:
        doc = build_brief_deterministic(parsed_list, label)
    if not args.no_redact:
        doc = redact_doc(doc)
    if getattr(args, "anonymize", False):
        doc, _ = anonymize_text(doc)
    if args.llm:
        stamp = make_stamp(len(parsed_list), newest_mtime,
                           distilled=int(time.time()),
                           distilled_sessions=len(parsed_list),
                           provider=args.llm)
    else:
        stamp = make_stamp(len(parsed_list), newest_mtime, 0, 0,
                           "none")
    doc = stamp + "\n" + doc
    dest = (brief_path(scope) if args.output == "handoff.md"
            else args.output)
    if str(dest) == "-":
        sys.stdout.write(doc)
        return
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(doc, encoding="utf-8")
    print(f"Wrote brief {dest} ({len(parsed_list)} sessions, "
          f"\u2248{_fmt_tokens(len(doc) // 4)} tokens)", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    parser.set_defaults(**_load_config())
    args = parser.parse_args(argv)
    if args.fit and (args.llm or args.max_chars is not None):
        raise SystemExit("--fit sizes the deterministic output on its own — "
                         "drop --llm / --max-chars when using it "
                         "(--fit may also come from your config file).")
    if args.mcp:
        run_mcp_server(allow_llm=args.allow_llm)
        return
    if args.completions:
        print_completions(args.completions)
        return
    if args.hook_stdin:
        run_hook_mode()
        return
    if args.brief_hook_stdin:
        run_brief_hook_mode()
        return
    if args.brief_update_stdin:
        run_brief_update_mode()
        return
    if args.install_brief_hook or args.uninstall_brief_hook:
        install_brief_hook(remove=args.uninstall_brief_hook)
        return
    if args.install_hook or args.uninstall_hook:
        install_hook(remove=args.uninstall_hook)
        return
    if args.list:
        if args.session and is_web_export(Path(args.session).expanduser()):
            list_export_conversations(Path(args.session).expanduser())
        else:
            list_sessions(args.project, grep=args.grep,
                          as_json=args.format == "json")
        return
    if args.brief:
        _run_brief(args)
        return
    picked: list = []
    if args.interactive and not args.merge:
        scope = args.project
        if not scope and not args.any:
            scope = cwd_project_filter()
        picked = interactive_pick(scope, grep=args.grep)
    if args.merge:
        if args.session:
            raise SystemExit("--merge discovers sessions itself — drop the "
                             "explicit path, use --project/--name to scope.")
        scope = args.project
        if not scope and not args.any:
            scope = cwd_project_filter()
        sessions = find_sessions(scope)
        if args.name:
            named = set(find_session_by_name(args.name, scope))
            sessions = [s for s in sessions if s in named]
        if len(sessions) > 25:
            print(f"Merging the 25 most recent of {len(sessions)} sessions.",
                  file=sys.stderr)
            sessions = sessions[:25]
        parsed_list = []
        for s in reversed(sessions):            # oldest first
            p = parse_session(s)
            if p["turns"] and not looks_trivial(p):
                parsed_list.append(p)
        if not parsed_list:
            raise SystemExit("No sessions to merge in this scope — try "
                             "--project NAME or run --list.")
        print(f"Merging {len(parsed_list)} sessions.", file=sys.stderr)
        parsed = merge_parsed(parsed_list)
        source = Path(f"{len(parsed_list)} merged sessions"
                      + (f" [{scope}]" if scope else ""))
    elif len(picked) > 1:
        parsed_list = [parse_session(p) for p in
                       sorted(picked, key=lambda p: p.stat().st_mtime)]
        parsed_list = [p for p in parsed_list if p["turns"]]
        print(f"Merging {len(parsed_list)} picked sessions.",
              file=sys.stderr)
        parsed = merge_parsed(parsed_list)
        source = Path(f"{len(parsed_list)} merged sessions")
    else:
        source = picked[0] if picked else resolve_source(args)
        if is_web_export(source):
            parsed = parse_web_export(source, name_filter=args.name)
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
