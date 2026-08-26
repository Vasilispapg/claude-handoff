"""Session discovery & selection: globbing, labels, name/content search."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .parse import looks_trivial, parse_session
from .textutil import clean_text, one_line, user_text, warn

PROJECTS_DIR = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")) / "projects"

# --------------------------------------------------------------------------- #
#  Session discovery
# --------------------------------------------------------------------------- #

def find_sessions(project_filter=None,
                  projects_dir: Path | None = None) -> list[Path]:
    """All session JSONL files, newest first. `project_filter` is a
    substring, a list of substrings (a project matches ANY of them),
    or None for every project."""
    filters = [f.lower() for f in
               ([project_filter] if isinstance(project_filter, str)
                else project_filter or [])]
    projects_dir = projects_dir or PROJECTS_DIR
    if not projects_dir.is_dir():
        return []
    sessions = []
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        if filters and not any(f in proj.name.lower() for f in filters):
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
        return None  # unreadable — caller counts it


def _find_needle(parsed: dict, needle: str) -> str | None:
    """Preview around the first hit of one needle, or None."""
    for turn in parsed["turns"]:
        if turn["role"] not in ("user", "assistant"):
            continue
        text = "\n".join(turn["text_parts"])
        idx = text.lower().find(needle)
        if idx >= 0:
            start = max(0, idx - 40)
            return one_line(text[start:idx + len(needle) + 40], 100)
    return None


def grep_sessions(patterns,
                  project_filter=None) -> list[tuple]:
    """Sessions whose conversation text (user/assistant turns) contains
    every given pattern (case-insensitive substrings, AND semantics),
    newest first, each paired with a preview of the first pattern.
    Tool noise doesn't count — only what was actually said."""
    if isinstance(patterns, str):
        patterns = [patterns]
    needles = [p.lower() for p in patterns]
    scout = next((n for n in needles if _raw_prefilter_ok(n)), None)
    hits = []
    unreadable = 0
    for path in find_sessions(project_filter):
        if scout:
            scan = _may_contain(path, scout)
            if scan is None:
                unreadable += 1
                continue
            if not scan:
                continue  # raw scan is a superset test — skip the parse
        try:
            parsed = parse_session(path)
        except OSError:
            unreadable += 1
            continue  # an unreadable file must not kill the search
        previews = [_find_needle(parsed, n) for n in needles]
        if all(previews):
            hits.append((path, previews[0]))
    if unreadable:
        warn("--grep", f"skipped {unreadable} unreadable file(s)")
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


