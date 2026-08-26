"""CLI: argument parsing, source resolution, document assembly."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from ._version import __version__
from .brief import (
    brief_label,
    brief_path,
    build_brief_deterministic,
    build_brief_llm,
    graft_distilled,
    load_project_sessions,
    make_stamp,
    parse_stamp,
)
from .discovery import (
    PROJECTS_DIR,
    _newest_meaningful_session,
    _newest_named_session,
    cwd_project_filter,
    find_session_by_name,
    find_sessions,
    grep_sessions,
    interactive_pick,
    list_sessions,
)
from .integrations import (
    _copy_clipboard,
    install_brief_hook,
    install_hook,
    run_brief_hook_mode,
    run_brief_update_mode,
    run_hook_mode,
    run_mcp_server,
)
from .llm import CACHE_DIR, PROVIDERS, build_llm, llm_summarize
from .parse import looks_trivial, merge_parsed, parse_session, slice_turns
from .redact import anonymize_text, count_emails, redact_doc, redact_secrets
from .render import (
    DEFAULT_MAX_CHARS,
    build_deterministic,
    build_json,
    render_activity,
    render_footer,
    render_header,
    render_sidechains,
    render_transcript,
)
from .textutil import tilde
from .webexport import is_web_export, list_export_conversations, parse_web_export


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
    ap.add_argument("session", nargs="*",
                    help="path to a session .jsonl, or a name to search "
                         "for (default: latest session); several paths "
                         "merge into ONE handoff")
    ap.add_argument("--list", action="store_true",
                    help="list available sessions (title · first prompt) and exit")
    ap.add_argument("--name", metavar="QUERY",
                    help="pick newest session whose title or first prompt "
                         "contains QUERY (case-insensitive)")
    ap.add_argument("--grep", metavar="TEXT", action="append",
                    help="pick newest session whose conversation contains "
                         "TEXT (case-insensitive; repeat the flag to "
                         "require ALL terms; with --list/-i shows every "
                         "match)")
    ap.add_argument("--project", metavar="NAME", action="append",
                    help="pick latest session whose project path contains "
                         "NAME (repeat the flag to include several "
                         "projects)")
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
    ap.add_argument("--full", action="store_true",
                    help="verbatim conversation turns (classic transcript) "
                         "instead of the default condensed digest")
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
    ap.add_argument("--debug", action="store_true",
                    help="report tolerated failures (corrupt lines, "
                         "unreadable files) on stderr; "
                         "CLAUDE_HANDOFF_DEBUG=1 does the same")
    ap.add_argument("--no-cache", action="store_true",
                    help="with --llm: disable the chunk-note cache "
                         f"({CACHE_DIR})")
    ap.add_argument("--version", action="version", version=__version__)
    return ap


def resolve_source(args: argparse.Namespace) -> Path:
    """The session file to export: explicit path, picker, name match, or
    newest in scope."""
    session = args.session
    if isinstance(session, list):
        session = session[0] if session else None
    if args.grep:
        if session or args.name:
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
    if session:
        source = Path(session).expanduser()
        if source.is_file():
            return source
        looks_like_path = "/" in session or session.endswith(".jsonl")
        if looks_like_path:
            raise SystemExit(f"Not a file: {source}. "
                             f"Run `claude-handoff --list` to see sessions.")
        return _newest_named_session(session, args.project)
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
    print(f"Using latest session: {tilde(source)}", file=sys.stderr)
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
                                            max_chars=10**9, full=True))
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
                                  include_sidechains=args.include_sidechains,
                                  full=getattr(args, "full", False))
    if not args.no_redact:
        doc = redact_doc(doc)
    if getattr(args, "anonymize", False):
        doc, n_anon = anonymize_text(doc)
        if n_anon:
            print(f"Anonymized {n_anon} identifying string(s) — home paths, "
                  f"emails, IPs, username.", file=sys.stderr)
    return doc


def write_output(doc: str, parsed: dict, args: argparse.Namespace) -> None:
    # Egress heads-up, not censorship: emails pass default redaction (it
    # targets secrets), so name them before the doc gets pasted somewhere.
    if not getattr(args, "anonymize", False):
        n_mail = count_emails(doc)
        if n_mail:
            print(f"ℹ {n_mail} email address(es) in the output — "
                  f"--anonymize strips identity for public sharing.",
                  file=sys.stderr)
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
                "focus", "full"}


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
    if isinstance(scope, list):
        if len(scope) > 1:
            raise SystemExit("--brief needs a single project — pass "
                             "one --project.")
        scope = scope[0] if scope else None
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
    old_stamp = None
    if args.llm:
        doc = build_brief_llm(parsed_list, label, args.llm,
                              args.model, focus=args.focus,
                              redact=not args.no_redact,
                              use_cache=not args.no_cache)
    else:
        doc = build_brief_deterministic(parsed_list, label)
        # rebuilding the free skeleton must not discard a paid
        # distillation living in the stamped brief file (-o elsewhere
        # stays a plain skeleton)
        prior = brief_path(scope) if args.output == "handoff.md" else None
        if prior is not None and prior.is_file():
            old = prior.read_text(encoding="utf-8")
            old_stamp = parse_stamp(old)
            if old_stamp:
                doc = graft_distilled(old, doc, old_stamp, label,
                                      parsed_list)
    if not args.no_redact:
        doc = redact_doc(doc)
    if getattr(args, "anonymize", False):
        doc, _ = anonymize_text(doc)
    if args.llm:
        stamp = make_stamp(len(parsed_list), newest_mtime,
                           distilled=int(time.time()),
                           distilled_sessions=len(parsed_list),
                           provider=args.llm)
    elif old_stamp:
        stamp = make_stamp(len(parsed_list), newest_mtime,
                           old_stamp["distilled"],
                           old_stamp["distilled_sessions"],
                           old_stamp["provider"])
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
    print(f"Wrote brief {tilde(dest)} ({len(parsed_list)} sessions, "
          f"\u2248{_fmt_tokens(len(doc) // 4)} tokens)", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    parser.set_defaults(**_load_config())
    args = parser.parse_args(argv)
    if args.debug:
        os.environ["CLAUDE_HANDOFF_DEBUG"] = "1"
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
        one = args.session[0] if len(args.session) == 1 else None
        if one and is_web_export(Path(one).expanduser()):
            list_export_conversations(Path(one).expanduser())
        else:
            list_sessions(args.project, grep=args.grep,
                          as_json=args.format == "json")
        return
    if args.brief:
        _run_brief(args)
        return
    if len(args.session) > 1:
        sources = [Path(p).expanduser() for p in args.session]
        missing = [str(x) for x in sources if not x.is_file()]
        if missing:
            raise SystemExit(f"Not a file: {', '.join(missing)}. "
                             f"Several arguments merge as paths — run "
                             f"--list to find sessions by name.")
        if args.merge:
            raise SystemExit("Several paths already merge on their "
                             "own — drop --merge.")
        parsed_list = [parse_web_export(x) if is_web_export(x)
                       else parse_session(x) for x in sources]
        parsed_list = [p for p in parsed_list if p["turns"]]
        parsed_list.sort(key=lambda p: p["meta"]["first_ts"] or "")
        print(f"Merging {len(parsed_list)} sessions.", file=sys.stderr)
        parsed = merge_parsed(parsed_list)
        source = Path(f"{len(parsed_list)} merged sessions")
        slice_turns(parsed, last=args.last, since=args.since)
        write_output(build_document(parsed, source, args), parsed, args)
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
