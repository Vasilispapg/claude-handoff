"""JSONL session parsing: records -> turns/meta/activity/sidechains."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .render import TOOL_LINE_CAP
from .textutil import (
    clean_text,
    debug,
    fmt_ts,
    one_line,
    tool_result_text,
    user_text,
)

FILE_TOOLS_WRITE = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
FILE_TOOLS_READ = {"Read"}

def load_records(path: Path) -> Iterator[dict]:
    """Yield JSONL records one at a time; corrupt lines are skipped.

    A generator, so hundreds-of-MB sessions stream instead of landing in
    memory, and early-exit consumers stop reading the file."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # tolerated by design; --debug surfaces it
                debug(path.name, f"corrupt line {n} skipped")


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
            "n_agents": 0, "n_agent_tools": 0, "n_notifications": 0,
            "tok_in": 0, "tok_cache_read": 0, "tok_out": 0, "summaries": [],
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
            # cache re-reads are counted separately — summed into tok_in
            # they inflate a long session into a meaningless billions figure
            state["meta"]["tok_in"] += (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0))
            state["meta"]["tok_cache_read"] += int(
                usage.get("cache_read_input_tokens") or 0)
            state["meta"]["tok_out"] += int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            pass
    content = message.get("content")
    if not isinstance(content, list):
        return
    turn = _current_assistant_turn(state)
    turn.setdefault("ts", rec.get("timestamp"))
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text", "").strip():
            turn["text_parts"].append(block["text"].strip())
        elif btype == "tool_use":
            _handle_tool_use(block, turn, state)
        # thinking blocks are deliberately dropped
    # n_assistant is derived from merged turns at the end of parse_session —
    # counting records here triple-counts what the reader sees as one reply


NOTIF_LINE_CAP = 160
_NOTIF_RE = re.compile(r"<task-notification>(.*?)</task-notification>\s*",
                       re.DOTALL)
_NOTIF_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_NOTIF_TYPE_RE = re.compile(r"<task-type>(.*?)</task-type>", re.DOTALL)


def _notification_lines(text: str) -> tuple[list[str], str]:
    """One digest line per <task-notification> block + the leftover text.

    Notifications arrive as user-role records but are machine messages —
    the payload is bulky and re-derivable (same rule as tool results), so
    only the <summary> survives, entities unescaped."""
    blocks = _NOTIF_RE.findall(text)
    if blocks:
        remainder = _NOTIF_RE.sub("", text).strip()
    else:                       # unclosed/truncated block — take it whole
        blocks, remainder = [text], ""
    lines = []
    for body in blocks:
        m = _NOTIF_SUMMARY_RE.search(body) or _NOTIF_TYPE_RE.search(body)
        lines.append(one_line(html.unescape(m.group(1)) if m
                              else "background task update", NOTIF_LINE_CAP))
    return lines, remainder


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
    # Background task-notifications are user-role records but not the human
    # speaking — a separate role keeps them out of the user count.
    if text.startswith("<task-notification>"):
        notes, text = _notification_lines(text)
        for line in notes:
            state["meta"]["n_notifications"] += 1
            state["turns"].append({"role": "notification",
                                   "text_parts": [line], "tools": [],
                                   "ts": rec.get("timestamp")})
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
        state["meta"]["n_agent_tools"] += sub["meta"]["n_tools"]
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
    # replies as the reader sees them: merged turns, not API records
    state["meta"]["n_assistant"] = sum(
        1 for t in state["turns"] if t["role"] == "assistant")
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
                    "n_agent_tools", "n_notifications",
                    "tok_in", "tok_cache_read", "tok_out"):
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
    # a session that dispatched subagents did real work by definition,
    # however short its main lane reads
    if meta["n_user"] + meta["n_assistant"] > 2 or meta["n_agents"]:
        return False
    chars = sum(len(part) for turn in parsed["turns"]
                for part in turn["text_parts"])
    return chars < 600


