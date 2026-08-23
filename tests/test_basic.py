"""Basic tests for claude_handoff. Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_handoff as ch  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "classic_session.jsonl"
TRIVIAL = ROOT / "tests" / "fixtures" / "trivial_session.jsonl"


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = ch.parse_session(FIXTURE)

    def test_meta(self):
        m = self.parsed["meta"]
        self.assertEqual(m["session_id"], "abc123")
        self.assertEqual(m["cwd"], "/home/vspapg/myapp")
        self.assertEqual(m["git_branch"], "main")
        self.assertIn("claude-sonnet-4-5", m["models"])
        self.assertEqual(m["n_user"], 2)        # meta + tool_result msgs excluded
        self.assertEqual(m["n_assistant"], 4)   # text-bearing records only
        self.assertEqual(m["summaries"], ["Fix login bug in auth.py"])

    def test_noise_filtered(self):
        text = str(self.parsed["turns"])
        self.assertNotIn("command-name", text)          # slash-command envelope
        self.assertNotIn("subagent chatter", text)      # sidechain dropped
        self.assertNotIn("Caveat:", text)
        self.assertNotIn("3 passed", text)              # tool result dropped

    def test_activity_extraction(self):
        self.assertIn("/home/vspapg/myapp/auth.py", self.parsed["files_written"])
        self.assertTrue(any("pytest" in c for c in self.parsed["commands"]))
        self.assertTrue(any("git add" in c for c in self.parsed["commands"]))

    def test_turn_merging(self):
        roles = [t["role"] for t in self.parsed["turns"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.parsed = ch.parse_session(FIXTURE)

    def test_deterministic_document(self):
        doc = ch.build_deterministic(self.parsed, FIXTURE,
                                     include_tools=True, max_chars=80_000)
        for expected in ("# Conversation handoff", "## Session",
                         "## Files created / modified", "## Commands run",
                         "## Conversation", "auth.py", "🧑 User",
                         "🤖 Assistant", "<details>"):
            self.assertIn(expected, doc)

    def test_tools_hidden_by_default(self):
        doc = ch.build_deterministic(self.parsed, FIXTURE,
                                     include_tools=False, max_chars=80_000)
        self.assertNotIn("<details>", doc)

    def test_sidechains_only_with_flag(self):
        doc = ch.build_deterministic(self.parsed, FIXTURE,
                                     include_tools=False, max_chars=80_000)
        self.assertNotIn("subagent chatter", doc)
        doc2 = ch.build_deterministic(self.parsed, FIXTURE,
                                      include_tools=False, max_chars=80_000,
                                      include_sidechains=True)
        self.assertIn("## Subagent work", doc2)
        self.assertIn("subagent chatter", doc2)

    def test_global_truncation_keeps_head_and_tail(self):
        doc = ch.render_transcript(self.parsed, include_tools=False,
                                   max_chars=150)
        self.assertIn("omitted", doc)
        self.assertIn("το login σπάει"[:10], doc)   # opening survives


class DiscoveryTests(unittest.TestCase):
    """Session titles, find-by-name, and CLI error hints."""

    def test_session_label_title_and_prompt(self):
        title, prompt = ch.session_label(FIXTURE)
        self.assertEqual(title, "Fix login bug in auth.py")
        self.assertIn("login", prompt)

    def test_find_session_by_name(self):
        with unittest.mock.patch.object(ch, "find_sessions",
                                        lambda *a, **k: [FIXTURE]):
            self.assertEqual(ch.find_session_by_name("login BUG"), [FIXTURE])
            self.assertEqual(ch.find_session_by_name("unicode"), [FIXTURE])
            self.assertEqual(ch.find_session_by_name("classic_session"),
                             [FIXTURE])
            self.assertEqual(ch.find_session_by_name("zzz-no-match"), [])

    def test_resolve_source_positional_name(self):
        import contextlib
        import io
        with unittest.mock.patch.object(ch, "find_sessions",
                                        lambda *a, **k: [FIXTURE]), \
                contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args(["login bug"])
            self.assertEqual(ch.resolve_source(args), FIXTURE)

    def test_resolve_source_name_flag_no_match_mentions_list(self):
        with unittest.mock.patch.object(ch, "find_sessions",
                                        lambda *a, **k: [FIXTURE]):
            args = ch.build_arg_parser().parse_args(["--name", "zzz"])
            with self.assertRaises(SystemExit) as cm:
                ch.resolve_source(args)
        self.assertIn("--list", str(cm.exception))

    def test_explicit_path_still_errors_as_file(self):
        args = ch.build_arg_parser().parse_args(["missing/session.jsonl"])
        with self.assertRaises(SystemExit) as cm:
            ch.resolve_source(args)
        self.assertIn("Not a file", str(cm.exception))

    def test_bad_flag_points_to_help(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), self.assertRaises(SystemExit):
            ch.build_arg_parser().parse_args(["--bogus"])
        self.assertIn("--help", buf.getvalue())

    def test_looks_trivial(self):
        self.assertTrue(ch.looks_trivial(ch.parse_session(TRIVIAL)))
        self.assertFalse(ch.looks_trivial(ch.parse_session(FIXTURE)))

    def test_auto_selection_skips_trivial_sessions(self):
        import contextlib
        import io
        buf = io.StringIO()
        with unittest.mock.patch.object(ch, "find_sessions",
                                        lambda *a, **k: [TRIVIAL, FIXTURE]), \
                contextlib.redirect_stderr(buf):
            args = ch.build_arg_parser().parse_args([])
            self.assertEqual(ch.resolve_source(args), FIXTURE)
        self.assertIn("nearly-empty", buf.getvalue())

    def test_explicit_choice_never_skipped(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args([str(TRIVIAL)])
            self.assertEqual(ch.resolve_source(args), TRIVIAL)

    def test_all_trivial_falls_back_to_newest(self):
        import contextlib
        import io
        with unittest.mock.patch.object(ch, "find_sessions",
                                        lambda *a, **k: [TRIVIAL]), \
                contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args([])
            self.assertEqual(ch.resolve_source(args), TRIVIAL)


class ProviderTests(unittest.TestCase):
    """API-key resolution (graphify-style env fallbacks) and claude-cli backend."""

    def setUp(self):
        self._saved = {}
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_API", "OPENAI_API_KEY",
                     "GPT_API", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API"):
            self._saved[name] = os.environ.pop(name, None)

    def tearDown(self):
        for name, val in self._saved.items():
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val

    def test_primary_env_key_wins(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-primary"
        os.environ["CLAUDE_API"] = "sk-alias"
        self.assertEqual(ch.provider_key("claude"), "sk-primary")

    def test_alias_env_keys(self):
        os.environ["CLAUDE_API"] = "sk-claude-alias"
        os.environ["GPT_API"] = "sk-gpt-alias"
        os.environ["GEMINI_API"] = "sk-gem-alias"
        self.assertEqual(ch.provider_key("claude"), "sk-claude-alias")
        self.assertEqual(ch.provider_key("openai"), "sk-gpt-alias")
        self.assertEqual(ch.provider_key("gemini"), "sk-gem-alias")

    def test_missing_key_message_names_all_accepted_vars(self):
        with self.assertRaises(SystemExit) as cm:
            ch.llm_summarize("openai", None, "transcript")
        self.assertIn("OPENAI_API_KEY", str(cm.exception))
        self.assertIn("GPT_API", str(cm.exception))

    def test_claude_cli_parses_envelope_and_builds_command(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input", "")

            class P:
                returncode = 0
                stdout = json.dumps({"result": "## Goal\nShip it."})
                stderr = ""
            return P()

        with unittest.mock.patch.object(ch.subprocess, "run", fake_run), \
                unittest.mock.patch.object(ch.shutil, "which",
                                           lambda _: "/usr/bin/claude"):
            out = ch.llm_summarize("claude-cli", "haiku", "the transcript")
        self.assertEqual(out, "## Goal\nShip it.")
        self.assertEqual(captured["cmd"][:2], ["claude", "-p"])
        self.assertIn("--output-format", captured["cmd"])
        self.assertIn("json", captured["cmd"])
        self.assertIn("--no-session-persistence", captured["cmd"])
        self.assertIn("--model", captured["cmd"])
        self.assertIn("haiku", captured["cmd"])
        self.assertIn("the transcript", captured["input"])

    def test_claude_cli_missing_binary(self):
        with unittest.mock.patch.object(ch.shutil, "which", lambda _: None):
            with self.assertRaises(SystemExit) as cm:
                ch.llm_summarize("claude-cli", None, "x")
        self.assertIn("claude", str(cm.exception).lower())

    def test_claude_cli_nonzero_exit(self):
        def fake_run(cmd, **kwargs):
            class P:
                returncode = 1
                stdout = ""
                stderr = "boom"
            return P()

        with unittest.mock.patch.object(ch.subprocess, "run", fake_run), \
                unittest.mock.patch.object(ch.shutil, "which",
                                           lambda _: "/usr/bin/claude"):
            with self.assertRaises(SystemExit) as cm:
                ch.llm_summarize("claude-cli", None, "x")
        self.assertIn("boom", str(cm.exception))

    def test_claude_cli_surfaces_envelope_error(self):
        def fake_run(cmd, **kwargs):
            class P:
                returncode = 1
                stdout = json.dumps({"is_error": True,
                                     "result": "Failed to authenticate."})
                stderr = ""
            return P()

        with unittest.mock.patch.object(ch.subprocess, "run", fake_run), \
                unittest.mock.patch.object(ch.shutil, "which",
                                           lambda _: "/usr/bin/claude"):
            with self.assertRaises(SystemExit) as cm:
                ch.llm_summarize("claude-cli", None, "x")
        self.assertIn("authenticate", str(cm.exception))


class CwdScopeTests(unittest.TestCase):
    """Current-directory-aware default scoping."""

    def test_encode_project_path(self):
        self.assertEqual(ch.encode_project_path(Path("/home/vspapg/my app")),
                         "-home-vspapg-my-app")

    def test_cwd_project_filter(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pd = Path(td)
            (pd / "-data-work-appA").mkdir()
            (pd / "-data-work-appB").mkdir()
            home = Path("/data/home")
            self.assertEqual(
                ch.cwd_project_filter(Path("/data/work/appA"), pd, home),
                "-data-work-appA")                      # project root
            self.assertEqual(
                ch.cwd_project_filter(Path("/data/work/appA/src/x"), pd, home),
                "-data-work-appA")                      # subfolder
            self.assertEqual(
                ch.cwd_project_filter(Path("/data/work"), pd, home),
                "-data-work")                           # master folder
            self.assertIsNone(
                ch.cwd_project_filter(Path("/somewhere/else"), pd, home))
            self.assertIsNone(ch.cwd_project_filter(home, pd, home))

    def test_any_flag_ignores_cwd_scope(self):
        import contextlib
        import io
        seen = []

        def fake_find(project_filter=None, projects_dir=None):
            seen.append(project_filter)
            return [FIXTURE]

        with unittest.mock.patch.object(ch, "find_sessions", fake_find), \
                contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args(["--any"])
            ch.resolve_source(args)
        self.assertEqual(seen, [None])


class SummarizeTests(unittest.TestCase):
    """--focus instructions and map-reduce chunking for huge transcripts."""

    def _fake_provider(self, calls):
        def fake_call(key, model, prompt):
            calls.append(prompt)
            return f"NOTES{len(calls)}"
        return unittest.mock.patch.dict(ch.PROVIDERS["claude-cli"],
                                        {"call": fake_call})

    def test_focus_reaches_the_prompt(self):
        calls = []
        with self._fake_provider(calls):
            out = ch.llm_summarize("claude-cli", None, "tiny transcript",
                                   focus="Focus on the auth bug")
        self.assertEqual(out, "NOTES1")
        self.assertIn("Focus on the auth bug", calls[0])
        self.assertIn("tiny transcript", calls[0])

    def test_huge_transcript_is_map_reduced(self):
        import contextlib
        import io
        calls = []
        big = "\n\n".join(f"### 🧑 User\n\nmessage {i} " + "x" * 300
                          for i in range(10))
        with self._fake_provider(calls), \
                unittest.mock.patch.object(ch, "LLM_INPUT_CAP", 1000), \
                unittest.mock.patch.object(ch, "CHUNK_CAP", 800), \
                contextlib.redirect_stderr(io.StringIO()):
            out = ch.llm_summarize("claude-cli", None, big,
                                   focus="care about X", use_cache=False)
        self.assertGreaterEqual(len(calls), 3)      # ≥2 map + 1 reduce
        self.assertEqual(out, f"NOTES{len(calls)}")
        self.assertIn("NOTES1", calls[-1])          # reduce sees map notes
        self.assertIn("care about X", calls[-1])    # focus reaches reduce
        for prompt in calls[:-1]:
            self.assertIn("part", prompt.lower())   # map prompts are labeled


WEB_EXPORT = ROOT / "tests" / "fixtures" / "claude_web_export.json"
COMPACTED = ROOT / "tests" / "fixtures" / "compacted_session.jsonl"


CHATGPT_EXPORT = ROOT / "tests" / "fixtures" / "chatgpt_export.json"


class PickerTests(unittest.TestCase):
    def test_interactive_pick_by_number(self):
        import contextlib
        import io
        with unittest.mock.patch.object(ch, "find_sessions",
                                        lambda *a, **k: [TRIVIAL, FIXTURE]), \
                unittest.mock.patch.object(ch.sys.stdin, "isatty",
                                           lambda: True), \
                unittest.mock.patch.object(ch.sys.stderr, "isatty",
                                           lambda: True, create=True), \
                unittest.mock.patch("builtins.input",
                                    side_effect=["nope", "2"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ch.interactive_pick(None), FIXTURE)

    def test_interactive_needs_terminal(self):
        with unittest.mock.patch.object(ch, "find_sessions",
                                        lambda *a, **k: [FIXTURE]), \
                unittest.mock.patch.object(ch.sys.stdin, "isatty",
                                           lambda: False):
            with self.assertRaises(SystemExit):
                ch.interactive_pick(None)


class CompletionsTests(unittest.TestCase):
    def test_completions_cover_flags(self):
        import contextlib
        import io
        for shell in ("bash", "zsh"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ch.print_completions(shell)
            out = buf.getvalue()
            for flag in ("--llm", "--merge", "--focus", "--list"):
                self.assertIn(flag, out)
            self.assertIn("complete -W", out)


class McpTests(unittest.TestCase):
    def test_stdio_server_initialize_list_call(self):
        import subprocess as sp
        req = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "handoff",
                                   "arguments": {"path": str(FIXTURE),
                                                 "last": 1}}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "handoff",
                                   "arguments": {"path": "/nope.jsonl"}}}),
        ]) + "\n"
        proc = sp.run(
            [sys.executable, str(ROOT / "claude_handoff.py"), "--mcp"],
            input=req, capture_output=True, text=True, encoding="utf-8",
            timeout=60, env={**os.environ, "PYTHONUTF8": "1"})
        replies = {m["id"]: m for m in
                   (json.loads(l) for l in proc.stdout.splitlines()
                    if l.strip())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"],
                         "claude-handoff")
        tools = {t["name"] for t in replies[2]["result"]["tools"]}
        self.assertEqual(tools, {"list_sessions", "handoff"})
        ok = replies[3]["result"]
        self.assertFalse(ok["isError"])
        self.assertIn("Conversation handoff", ok["content"][0]["text"])
        self.assertIn("last 1 of 2", ok["content"][0]["text"])
        self.assertTrue(replies[4]["result"]["isError"])


class ChatGPTExportTests(unittest.TestCase):
    def test_detection_and_parse(self):
        self.assertTrue(ch.is_web_export(CHATGPT_EXPORT))
        parsed = ch.parse_web_export(CHATGPT_EXPORT)
        self.assertEqual(parsed["meta"]["summaries"],
                         ["Fix docker compose networking"])
        roles = [t["role"] for t in parsed["turns"]]
        self.assertEqual(roles, ["user", "assistant", "assistant"])
        text = str(parsed["turns"])
        self.assertIn("bridge network", text)
        self.assertNotIn("system", roles)

    def test_timestamps_are_iso(self):
        parsed = ch.parse_web_export(CHATGPT_EXPORT)
        self.assertTrue(parsed["meta"]["first_ts"].startswith("2026-"))


class MergeTests(unittest.TestCase):
    def test_merge_two_sessions(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            merged = ch.merge_parsed([ch.parse_session(COMPACTED),
                                      ch.parse_session(FIXTURE)])
        self.assertEqual(merged["meta"]["n_user"], 3)      # 1 + 2
        roles = [t["role"] for t in merged["turns"]]
        self.assertEqual(roles.count("session-break"), 2)
        doc = ch.build_deterministic(merged, Path("2 sessions"),
                                     include_tools=False, max_chars=80_000)
        self.assertIn("Session 1", doc)
        self.assertIn("Session 2", doc)
        self.assertIn("rate limiting", doc)                # from COMPACTED
        self.assertIn("unicode", doc)                      # from FIXTURE
        self.assertIn("auth.py", str(merged["files_written"]))


class JsonFormatTests(unittest.TestCase):
    def test_json_document_structure(self):
        parsed = ch.parse_session(FIXTURE)
        doc = ch.build_json(parsed, FIXTURE, summary=None)
        data = json.loads(doc)
        self.assertEqual(data["meta"]["n_user"], 2)
        self.assertIn("auth.py", str(data["activity"]["files_written"]))
        self.assertEqual(data["turns"][0]["role"], "user")
        self.assertIn("generator", data)

    def test_json_includes_summary_when_given(self):
        parsed = ch.parse_session(FIXTURE)
        data = json.loads(ch.build_json(parsed, FIXTURE, summary="## Goal\nx"))
        self.assertEqual(data["summary"], "## Goal\nx")


class TokenStatsTests(unittest.TestCase):
    def test_usage_summed_and_rendered(self):
        parsed = ch.parse_session(COMPACTED)   # assistant record has usage
        self.assertEqual(parsed["meta"]["tok_in"], 1200)
        self.assertEqual(parsed["meta"]["tok_out"], 45)
        doc = ch.build_deterministic(parsed, COMPACTED,
                                     include_tools=False, max_chars=80_000)
        self.assertIn("**Tokens:**", doc)

    def test_no_usage_no_line(self):
        parsed = ch.parse_session(FIXTURE)     # fixture has no usage fields
        doc = ch.build_deterministic(parsed, FIXTURE,
                                     include_tools=False, max_chars=80_000)
        self.assertNotIn("**Tokens:**", doc)


class ParallelMapTests(unittest.TestCase):
    def test_api_provider_chunks_run_in_threads_and_stay_ordered(self):
        import contextlib
        import io
        import threading as th
        seen_threads = set()

        def fake_call(key, model, prompt):
            seen_threads.add(th.get_ident())
            time.sleep(0.05)
            part = prompt.split("PART:\n", 1)[1][:20] if "PART:\n" in prompt \
                else "reduce"
            return f"note<{part}>"

        import time
        big = "\n\n".join(f"### 🧑 User\n\nmsg {i} " + "x" * 300
                          for i in range(10))
        with unittest.mock.patch.dict(ch.PROVIDERS["openai"],
                                      {"call": fake_call}), \
                unittest.mock.patch.object(ch, "LLM_INPUT_CAP", 1000), \
                unittest.mock.patch.object(ch, "CHUNK_CAP", 800), \
                unittest.mock.patch.dict(ch.os.environ,
                                         {"OPENAI_API_KEY": "sk-test"}), \
                contextlib.redirect_stderr(io.StringIO()):
            out = ch.llm_summarize("openai", None, big, use_cache=False)
        self.assertTrue(out.startswith("note<"))
        self.assertGreater(len(seen_threads), 1)   # genuinely parallel


class CompactionTests(unittest.TestCase):
    """Sessions that went through /compact."""

    def test_compact_summary_labeled_not_counted_as_user(self):
        parsed = ch.parse_session(COMPACTED)
        roles = [t["role"] for t in parsed["turns"]]
        self.assertEqual(roles, ["compact", "user", "assistant"])
        self.assertEqual(parsed["meta"]["n_user"], 1)   # only the human turn

    def test_compact_summary_rendering(self):
        parsed = ch.parse_session(COMPACTED)
        doc = ch.build_deterministic(parsed, COMPACTED,
                                     include_tools=False, max_chars=80_000)
        self.assertIn("Compacted history", doc)
        self.assertIn("Primary Request and Intent", doc)
        self.assertEqual(doc.count("🧑 User"), 1)

    def test_prefix_fallback_without_flag(self):
        rec = {"type": "user", "message": {
            "role": "user",
            "content": "This session is being continued from a previous "
                       "conversation that ran out of context. Summary: x"}}
        state = ch._new_parse_state()
        ch._handle_user_record(rec, state)
        self.assertEqual(state["turns"][0]["role"], "compact")
        self.assertEqual(state["meta"]["n_user"], 0)


class WebExportTests(unittest.TestCase):
    """claude.ai data-export (conversations.json) as input."""

    def test_detection(self):
        self.assertTrue(ch.is_web_export(WEB_EXPORT))
        self.assertFalse(ch.is_web_export(FIXTURE))

    def test_parse_picks_newest_by_default(self):
        parsed = ch.parse_web_export(WEB_EXPORT)
        self.assertEqual(parsed["meta"]["session_id"], "web-conv-002")
        self.assertEqual(parsed["meta"]["summaries"], ["Trip planning notes"])

    def test_parse_by_name(self):
        parsed = ch.parse_web_export(WEB_EXPORT, name_filter="webhook")
        self.assertEqual(parsed["meta"]["session_id"], "web-conv-001")
        self.assertEqual(parsed["meta"]["n_user"], 2)
        self.assertEqual(parsed["meta"]["n_assistant"], 2)
        text = str(parsed["turns"])
        self.assertIn("signature validation", text)
        self.assertIn("[attachment]", text)

    def test_renders_without_project_line(self):
        parsed = ch.parse_web_export(WEB_EXPORT, name_filter="webhook")
        doc = ch.build_deterministic(parsed, WEB_EXPORT,
                                     include_tools=False, max_chars=80_000)
        self.assertNotIn("**Project:**", doc)
        self.assertIn("Debug payment webhook", doc)
        self.assertIn("🧑 User", doc)

    def test_no_match_errors_with_hint(self):
        with self.assertRaises(SystemExit) as cm:
            ch.parse_web_export(WEB_EXPORT, name_filter="zzz")
        self.assertIn("--list", str(cm.exception))


class SliceTests(unittest.TestCase):
    """--last N and --since filters."""

    def test_last_keeps_tail_user_turns(self):
        parsed = ch.parse_session(FIXTURE)     # 2 user turns
        ch.slice_turns(parsed, last=1)
        roles = [t["role"] for t in parsed["turns"]]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertIn("last 1 of 2", parsed["slice_note"])

    def test_since_absolute_timestamp(self):
        parsed = ch.parse_session(FIXTURE)     # msgs at 12:00→12:04 +03:00
        ch.slice_turns(parsed, since="2026-08-20T12:02:30+03:00")
        self.assertEqual(len(parsed["turns"]), 2)
        self.assertEqual(parsed["turns"][0]["role"], "user")

    def test_since_duration_relative_to_session_end(self):
        parsed = ch.parse_session(FIXTURE)
        ch.slice_turns(parsed, since="1m")     # last minute of the session
        self.assertLess(len(parsed["turns"]), 4)
        self.assertGreater(len(parsed["turns"]), 0)

    def test_no_filter_is_noop(self):
        parsed = ch.parse_session(FIXTURE)
        n = len(parsed["turns"])
        ch.slice_turns(parsed)
        self.assertEqual(len(parsed["turns"]), n)
        self.assertNotIn("slice_note", parsed)


class ClipboardTests(unittest.TestCase):
    def test_copies_via_first_available_tool(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")

            class P:
                returncode = 0
            return P()

        with unittest.mock.patch.object(ch.subprocess, "run", fake_run), \
                unittest.mock.patch.object(
                    ch.shutil, "which",
                    lambda name: "/usr/bin/pbcopy" if name == "pbcopy"
                    else None):
            tool = ch._copy_clipboard("hello doc")
        self.assertEqual(tool, "pbcopy")
        self.assertEqual(captured["input"], "hello doc")

    def test_errors_when_no_tool(self):
        with unittest.mock.patch.object(ch.shutil, "which", lambda _: None):
            with self.assertRaises(SystemExit):
                ch._copy_clipboard("x")


class HookTests(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_removes(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "settings.json"
            sp.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                ch.install_hook(settings_path=sp)
                ch.install_hook(settings_path=sp)          # idempotent
            data = json.loads(sp.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "opus")        # untouched
            entries = data["hooks"]["SessionEnd"]
            cmds = [h["command"] for e in entries for h in e["hooks"]]
            self.assertEqual(cmds.count(ch.HOOK_COMMAND), 1)
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                ch.install_hook(settings_path=sp, remove=True)
            data = json.loads(sp.read_text(encoding="utf-8"))
            self.assertNotIn("SessionEnd", data.get("hooks", {}))

    def test_malformed_settings_never_clobbered(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "settings.json"
            sp.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                ch.install_hook(settings_path=sp)
            self.assertEqual(sp.read_text(encoding="utf-8"), "{not json")


class OllamaTests(unittest.TestCase):
    def test_call_hits_local_endpoint(self):
        captured = {}

        def fake_http(url, payload, headers):
            captured["url"] = url
            captured["payload"] = payload
            return {"choices": [{"message": {"content": "local summary"}}]}

        import contextlib
        import io
        with unittest.mock.patch.object(ch, "http_json", fake_http), \
                contextlib.redirect_stderr(io.StringIO()):
            out = ch.llm_summarize("ollama", None, "tiny transcript")
        self.assertEqual(out, "local summary")
        self.assertIn("localhost:11434", captured["url"])
        self.assertEqual(captured["payload"]["model"],
                         ch.PROVIDERS["ollama"]["default_model"])

    def test_connection_error_mentions_ollama_serve(self):
        def fake_http(url, payload, headers):
            raise SystemExit("LLM API unreachable: connection refused")

        import contextlib
        import io
        with unittest.mock.patch.object(ch, "http_json", fake_http), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                ch.llm_summarize("ollama", None, "x")
        self.assertIn("ollama serve", str(cm.exception))


class ZeroTrustTests(unittest.TestCase):
    """Secret redaction, chunk-note cache, and per-chunk retry."""

    def test_redact_secrets_hits_known_shapes(self):
        text = ("export ANTHROPIC_API_KEY=sk-ant-abc123def456ghi789 and "
                "ghp_abcdefghij1234567890KLMNOP plus AKIAIOSFODNN7EXAMPLE "
                "and password=hunter2secret ok")
        redacted, n = ch.redact_secrets(text)
        self.assertEqual(n, 4)
        self.assertNotIn("sk-ant-abc123def456ghi789", redacted)
        self.assertNotIn("ghp_abcdefghij", redacted)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", redacted)
        self.assertNotIn("hunter2secret", redacted)
        self.assertIn("password=[REDACTED]", redacted)

    def test_redact_leaves_normal_content_alone(self):
        text = ("git commit 1a2b3c4 fixed auth.py; see "
                "https://github.com/Vasilispapg/claude-handoff and run "
                "python -m pytest tests/ -q")
        redacted, n = ch.redact_secrets(text)
        self.assertEqual(n, 0)
        self.assertEqual(redacted, text)

    def test_chunk_cache_resumes_without_new_calls(self):
        import contextlib
        import io
        import tempfile
        calls = []

        def fake_call(key, model, prompt):
            calls.append(prompt)
            return f"NOTES{len(calls)}"

        big = "\n\n".join(f"### 🧑 User\n\nmessage {i} " + "x" * 300
                          for i in range(10))
        with tempfile.TemporaryDirectory() as td, \
                unittest.mock.patch.dict(ch.PROVIDERS["claude-cli"],
                                         {"call": fake_call}), \
                unittest.mock.patch.object(ch, "CACHE_DIR", Path(td)), \
                unittest.mock.patch.object(ch, "LLM_INPUT_CAP", 1000), \
                unittest.mock.patch.object(ch, "CHUNK_CAP", 800), \
                contextlib.redirect_stderr(io.StringIO()):
            ch.llm_summarize("claude-cli", None, big)
            first_run = len(calls)
            ch.llm_summarize("claude-cli", None, big)
            second_run = len(calls) - first_run
        self.assertGreater(first_run, 2)
        self.assertEqual(second_run, 1)   # only the reduce call repeats

    def test_call_with_retry_recovers_once(self):
        import contextlib
        import io
        attempts = []

        def flaky(key, model, prompt):
            attempts.append(1)
            if len(attempts) == 1:
                raise SystemExit("LLM API error 429: rate limited")
            return "ok"

        with unittest.mock.patch.object(ch.time, "sleep", lambda _: None), \
                contextlib.redirect_stderr(io.StringIO()):
            out = ch._call_with_retry(flaky, None, None, "p")
        self.assertEqual(out, "ok")
        self.assertEqual(len(attempts), 2)

    def test_call_with_retry_final_error_mentions_resume(self):
        def always_fails(key, model, prompt):
            raise SystemExit("LLM API error 500: boom")

        import contextlib
        import io
        with unittest.mock.patch.object(ch.time, "sleep", lambda _: None), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                ch._call_with_retry(always_fails, None, None, "p")
        self.assertIn("resume", str(cm.exception))


class HelperTests(unittest.TestCase):
    def test_truncate_short_passthrough(self):
        self.assertEqual(ch.truncate("abc", 10), "abc")

    def test_truncate_long_keeps_ends(self):
        text = "A" * 500 + "MID" + "Z" * 500
        out = ch.truncate(text, 100)
        self.assertLess(len(out), len(text))
        self.assertTrue(out.startswith("A"))
        self.assertTrue(out.endswith("Z"))
        self.assertIn("omitted", out)

    def test_clean_text(self):
        raw = "<system-reminder>noise</system-reminder>hello"
        self.assertEqual(ch.clean_text(raw), "hello")

    def test_tool_result_text_variants(self):
        self.assertEqual(ch.tool_result_text({"content": " x "}), "x")
        self.assertEqual(
            ch.tool_result_text(
                {"content": [{"type": "text", "text": "a"},
                             {"type": "text", "text": "b"}]}),
            "a\nb")
        self.assertEqual(ch.tool_result_text({"content": None}), "")


if __name__ == "__main__":
    unittest.main()
