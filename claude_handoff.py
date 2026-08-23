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
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.3.0"

PROJECTS_DIR = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")) / "projects"

# Message-level caps (deterministic mode). Head+tail are kept when truncating.
USER_MSG_CAP = 8000
ASSISTANT_MSG_CAP = 5000
TOOL_LINE_CAP = 200
DEFAULT_MAX_CHARS = 80_000       # global cap on the transcript section
LLM_INPUT_CAP = 400_000          # cap on transcript sent to an LLM

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
        "turns": [],            # {"role", "text_parts", "tools"}
        "files_written": {},    # path -> edit count
        "files_read": {},       # path -> read count
        "commands": [],
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
                                           "tools": []})
        return
    text = clean_text(user_text(message))
    if not text:
        return
    state["meta"]["n_user"] += 1
    state["turns"].append({"role": "user", "text_parts": [text], "tools": []})


def parse_session(path: Path) -> dict:
    """Parse a session JSONL into turns + metadata + activity stats."""
    state = _new_parse_state()
    for rec in load_records(path):
        rtype = rec.get("type")
        if rtype == "summary" and rec.get("summary"):
            state["meta"]["summaries"].append(rec["summary"])
        elif rtype in ("user", "assistant") and not rec.get("isSidechain"):
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
        f"- **Project:** `{m['cwd'] or '?'}`" +
        (f" (branch `{m['git_branch']}`)" if m["git_branch"] else ""),
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


def render_footer() -> str:
    return ("---\n\n_Exported with [claude-handoff]"
            "(https://github.com/Vasilispapg/claude-handoff) — continue from here._")


def build_deterministic(parsed: dict, source: Path, include_tools: bool,
                        max_chars: int) -> str:
    sections = [
        render_header(parsed, source),
        render_activity(parsed),
        render_transcript(parsed, include_tools, max_chars),
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

TRANSCRIPT:
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
})


def llm_summarize(provider: str, model: str | None, transcript: str) -> str:
    """Resolve key + model for the provider and run its call strategy."""
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise SystemExit(f"Unknown provider: {provider}. "
                         f"Available: {', '.join(sorted(PROVIDERS))}")
    key = provider_key(provider)
    if cfg["env_keys"] and not key:
        accepted = " or ".join(cfg["env_keys"])
        raise SystemExit(f"Set {accepted} to use --llm {provider}")
    prompt = SUMMARY_PROMPT + truncate(transcript, LLM_INPUT_CAP)
    return cfg["call"](key, model or cfg["default_model"], prompt)


def build_llm(parsed: dict, source: Path, provider: str, model: str | None,
              with_transcript: bool, max_chars: int) -> str:
    transcript = render_transcript(parsed, include_tools=True,
                                   max_chars=LLM_INPUT_CAP)
    activity = render_activity(parsed)
    summary = llm_summarize(provider, model,
                            activity + "\n\n" + transcript)
    sections = [render_header(parsed, source), summary.strip()]
    if with_transcript:
        sections.append(render_transcript(parsed, include_tools=False,
                                          max_chars=max_chars))
    sections.append(render_footer())
    return "\n\n".join(sections) + "\n"


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

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
    ap.add_argument("-o", "--output", default="handoff.md",
                    help="output file, or '-' for stdout (default: handoff.md)")
    ap.add_argument("--include-tools", action="store_true",
                    help="include collapsed per-tool-call detail in transcript")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                    help=f"cap transcript section size (default {DEFAULT_MAX_CHARS})")
    ap.add_argument("--llm", choices=sorted(PROVIDERS),
                    help="summarize with an LLM instead of deterministic export; "
                         "claude-cli uses your local Claude Code login, no API key")
    ap.add_argument("--model", help="override the LLM model id for --llm")
    ap.add_argument("--with-transcript", action="store_true",
                    help="with --llm: also append the cleaned transcript")
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
    sessions = find_sessions(args.project)
    if not sessions:
        raise SystemExit(
            f"No sessions found under {PROJECTS_DIR}"
            + (f" matching '{args.project}'" if args.project else "")
            + ". Pass a .jsonl path explicitly, or run --list.")
    print(f"Using latest session: {sessions[0]}", file=sys.stderr)
    return sessions[0]


def build_document(parsed: dict, source: Path,
                   args: argparse.Namespace) -> str:
    """Deterministic or LLM-summarized document, per the CLI flags."""
    if args.llm:
        return build_llm(parsed, source, args.llm, args.model,
                         args.with_transcript, args.max_chars)
    return build_deterministic(parsed, source, args.include_tools,
                               args.max_chars)


def write_output(doc: str, parsed: dict, args: argparse.Namespace) -> None:
    if args.output == "-":
        sys.stdout.write(doc)
        return
    out = Path(args.output)
    out.write_text(doc, encoding="utf-8")
    n_user = parsed["meta"]["n_user"]
    print(f"Wrote {out} ({len(doc):,} chars, {n_user} user messages"
          f"{', LLM-summarized' if args.llm else ''})", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    if args.list:
        list_sessions(args.project)
        return
    source = resolve_source(args)
    parsed = parse_session(source)
    if not parsed["turns"]:
        raise SystemExit("Session parsed but contains no conversation turns.")
    write_output(build_document(parsed, source, args), parsed, args)


if __name__ == "__main__":
    main()
