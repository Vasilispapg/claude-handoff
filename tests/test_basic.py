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


class WebExportTests(unittest.TestCase):
    """claude.ai data-export (conversations.json) as input."""

    def test_detection(self):
        self.assertTrue(ch.is_web_export(WEB_EXPORT))
        self.assertFalse(ch.is_web_export(FIXTURE))

    def test_parse_picks_newest_by_default(self):
        parsed = ch.parse_claude_export(WEB_EXPORT)
        self.assertEqual(parsed["meta"]["session_id"], "web-conv-002")
        self.assertEqual(parsed["meta"]["summaries"], ["Trip planning notes"])

    def test_parse_by_name(self):
        parsed = ch.parse_claude_export(WEB_EXPORT, name_filter="webhook")
        self.assertEqual(parsed["meta"]["session_id"], "web-conv-001")
        self.assertEqual(parsed["meta"]["n_user"], 2)
        self.assertEqual(parsed["meta"]["n_assistant"], 2)
        text = str(parsed["turns"])
        self.assertIn("signature validation", text)
        self.assertIn("[attachment]", text)

    def test_renders_without_project_line(self):
        parsed = ch.parse_claude_export(WEB_EXPORT, name_filter="webhook")
        doc = ch.build_deterministic(parsed, WEB_EXPORT,
                                     include_tools=False, max_chars=80_000)
        self.assertNotIn("**Project:**", doc)
        self.assertIn("Debug payment webhook", doc)
        self.assertIn("🧑 User", doc)

    def test_no_match_errors_with_hint(self):
        with self.assertRaises(SystemExit) as cm:
            ch.parse_claude_export(WEB_EXPORT, name_filter="zzz")
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
