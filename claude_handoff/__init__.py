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

from __future__ import annotations

# Re-exported stdlib modules keep the historical `ch.subprocess` /
# `ch.shutil` / `ch.sys` / `ch.time` / `ch.os` patch points working.
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import time  # noqa: F401

from ._version import __version__  # noqa: F401
from .brief import (  # noqa: F401
    BRIEFS_DIR,
    brief_path,
    build_brief_deterministic,
    build_brief_llm,
)
from .cli import (  # noqa: F401
                       _fit_transcript_cap,
                       _fmt_tokens,
                       _HelpfulParser,
                       _parse_budget,
                       build_arg_parser,
                       build_document,
                       main,
                       print_completions,
                       resolve_source,
                       write_output,
)
from .discovery import (  # noqa: F401
                       PROJECTS_DIR,
                       _newest_meaningful_session,
                       _newest_named_session,
                       cwd_project_filter,
                       encode_project_path,
                       find_session_by_name,
                       find_sessions,
                       grep_sessions,
                       interactive_pick,
                       list_sessions,
                       session_label,
)
from .integrations import (  # noqa: F401
    BRIEF_HOOK_COMMAND,
    BRIEF_UPDATE_COMMAND,
                       HANDOFFS_DIR,
                       HOOK_COMMAND,
                       MCP_PROTOCOL,
                       SKILL_MD,
                       SKILL_TRIGGER_BLOCK,
                       _copy_clipboard,
                       _mcp_call,
                       _mcp_tools,
                       install_brief_hook,
                       install_hook,
                       install_skill,
                       run_brief_hook_mode,
                       run_brief_update_mode,
                       run_hook_mode,
                       run_mcp_server,
)
from .llm import (  # noqa: F401
                       CACHE_DIR,
                       CACHE_VERSION,
                       CHUNK_CAP,
                       CHUNK_PROMPT,
                       LLM_INPUT_CAP,
                       PARALLEL_WORKERS,
                       PROVIDERS,
                       SERIAL_PROVIDERS,
                       SUMMARY_PROMPT,
                       _call_with_retry,
                       _chunk_text,
                       build_llm,
                       http_json,
                       llm_summarize,
                       provider_key,
)
from .parse import (  # noqa: F401
                       FILE_TOOLS_READ,
                       FILE_TOOLS_WRITE,
                       _current_assistant_turn,
                       _drain_agent_texts,
                       _handle_assistant_record,
                       _handle_sidechain_record,
                       _handle_tool_use,
                       _handle_user_record,
                       _new_parse_state,
                       _parse_agent_files,
                       _parse_ts,
                       _since_cutoff,
                       _update_envelope_meta,
                       load_records,
                       looks_trivial,
                       merge_parsed,
                       parse_session,
                       slice_turns,
                       tool_summary,
)
from .redact import SECRET_RES, anonymize_text, redact_doc, redact_secrets  # noqa: F401
from .render import (  # noqa: F401
                       ASSISTANT_MSG_CAP,
                       DEFAULT_MAX_CHARS,
                       TOOL_LINE_CAP,
                       USER_MSG_CAP,
                       build_deterministic,
                       build_json,
                       render_activity,
                       render_footer,
                       render_header,
                       render_sidechains,
                       render_transcript,
)
from .textutil import (  # noqa: F401
                       CAVEAT_RE,
                       NOISE_RE,
                       clean_text,
                       fmt_ts,
                       one_line,
                       tool_result_text,
                       truncate,
                       user_text,
)
from .webexport import (  # noqa: F401
                       is_web_export,
                       list_export_conversations,
                       parse_web_export,
)
