"""Host integrations: clipboard, MCP server, SessionEnd hook."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ._version import __version__
from .discovery import (
    _newest_meaningful_session,
    find_session_by_name,
    find_sessions,
    session_label,
)
from .llm import build_llm
from .parse import looks_trivial, parse_session, slice_turns
from .redact import anonymize_text, redact_doc
from .render import DEFAULT_MAX_CHARS, build_deterministic
from .webexport import is_web_export, parse_web_export


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
        out.write_text(redact_doc(build_deterministic(
            parsed, transcript, include_tools=False,
            max_chars=DEFAULT_MAX_CHARS), hint=False), encoding="utf-8")
        print(f"handoff written: {out}")
    except Exception:  # deliberately swallow: see docstring
        return


