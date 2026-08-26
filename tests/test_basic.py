"""Basic tests for claude_handoff. Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# hermetic: a real user config must never leak into test runs
os.environ.setdefault("CLAUDE_HANDOFF_CONFIG", str(ROOT / "no-such-config"))

import claude_handoff as ch  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "classic_session.jsonl"
TRIVIAL = ROOT / "tests" / "fixtures" / "trivial_session.jsonl"
AGENT_SESSION = ROOT / "tests" / "fixtures" / "agent_session.jsonl"
SECRET = ROOT / "tests" / "fixtures" / "secret_session.jsonl"


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
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
                                        lambda *a, **k: [FIXTURE]):
            self.assertEqual(ch.find_session_by_name("login BUG"), [FIXTURE])
            self.assertEqual(ch.find_session_by_name("unicode"), [FIXTURE])
            self.assertEqual(ch.find_session_by_name("classic_session"),
                             [FIXTURE])
            self.assertEqual(ch.find_session_by_name("zzz-no-match"), [])

    def test_resolve_source_positional_name(self):
        import contextlib
        import io
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
                                        lambda *a, **k: [FIXTURE]), \
                contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args(["login bug"])
            self.assertEqual(ch.resolve_source(args), FIXTURE)

    def test_resolve_source_name_flag_no_match_mentions_list(self):
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
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
        with unittest.mock.patch.object(ch.cli, "find_sessions",
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
        with unittest.mock.patch.object(ch.cli, "find_sessions",
                                        lambda *a, **k: [TRIVIAL]), \
                contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args([])
            self.assertEqual(ch.resolve_source(args), TRIVIAL)


class AgentSessionTests(unittest.TestCase):
    """Separate-file subagent transcripts (<session-id>/subagents/agent-*.jsonl)."""

    @classmethod
    def setUpClass(cls):
        cls.parsed = ch.parse_session(AGENT_SESSION)

    def test_agent_files_become_sidechain_groups_ordered_by_time(self):
        groups = self.parsed["sidechains"]
        self.assertEqual(len(groups), 2)
        # agent-bbb222 ran first: timestamp order, not filename order
        self.assertIn("Update the docs", groups[0]["prompt"])
        self.assertIn("encoding bug", groups[1]["prompt"])
        self.assertEqual(groups[0]["agent_id"], "bbb222")
        self.assertTrue(any("docs ενημερώθηκαν" in t
                            for t in groups[0]["texts"]))

    def test_agent_activity_merged_by_default(self):
        fw = self.parsed["files_written"]
        self.assertIn("/home/vspapg/myapp/docs/api.md", fw)
        self.assertIn("/home/vspapg/myapp/server.py", fw)
        self.assertTrue(any("pytest" in c for c in self.parsed["commands"]))
        self.assertTrue(any("git diff --stat docs/" in c
                            for c in self.parsed["commands"]))

    def test_meta_counts_agents(self):
        self.assertEqual(self.parsed["meta"]["n_agents"], 2)
        self.assertEqual(ch.parse_session(FIXTURE)["meta"]["n_agents"], 0)

    def test_agent_texts_stay_out_of_main_turns(self):
        text = str(self.parsed["turns"])
        self.assertNotIn("Ενημερώνω τα docs", text)       # agent chatter
        self.assertIn("Οι δύο agents τελείωσαν", text)    # main convo intact

    def test_lane_labels_from_agent_tool_calls(self):
        groups = self.parsed["sidechains"]
        self.assertEqual(groups[0]["label"], "Docs update lane")
        self.assertEqual(groups[1]["label"], "Backend fix lane")

    def test_label_used_as_heading_prompt_moves_to_body(self):
        doc = ch.build_deterministic(self.parsed, AGENT_SESSION,
                                     include_tools=False, max_chars=80_000,
                                     include_sidechains=True)
        self.assertIn("### Subagent: Docs update lane", doc)
        self.assertIn("Task: Update the docs for the new API", doc)

    def test_parent_steering_messages_interleaved_in_texts(self):
        joined = "\n".join(self.parsed["sidechains"][0]["texts"])
        self.assertIn("🧭 Parent: Και πρόσθεσε ένα παράδειγμα", joined)
        self.assertLess(joined.index("Ενημερώνω τα docs"),
                        joined.index("🧭 Parent:"))       # chronological
        self.assertLess(joined.index("🧭 Parent:"),
                        joined.index("docs ενημερώθηκαν"))
        self.assertNotIn("meta noise", joined)            # isMeta filtered

    def test_render_marker_by_default_full_texts_with_flag(self):
        doc = ch.build_deterministic(self.parsed, AGENT_SESSION,
                                     include_tools=False, max_chars=80_000)
        self.assertIn("2 subagent", doc)                  # marker line
        self.assertIn("--include-sidechains", doc)        # discovery hint
        self.assertNotIn("## Subagent work", doc)
        self.assertNotIn("docs ενημερώθηκαν", doc)
        doc2 = ch.build_deterministic(self.parsed, AGENT_SESSION,
                                      include_tools=False, max_chars=80_000,
                                      include_sidechains=True)
        self.assertIn("## Subagent work", doc2)
        self.assertIn("docs ενημερώθηκαν", doc2)


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

        with unittest.mock.patch.object(ch.cli, "find_sessions", fake_find), \
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
                unittest.mock.patch.object(ch.llm, "LLM_INPUT_CAP", 1000), \
                unittest.mock.patch.object(ch.llm, "CHUNK_CAP", 800), \
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


class ListJsonTests(unittest.TestCase):
    """--list --format json: machine-readable session listing."""

    def test_list_json_structure(self):
        import contextlib
        import io
        buf = io.StringIO()
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
                                        lambda *a, **k: [FIXTURE, TRIVIAL]), \
                contextlib.redirect_stdout(buf):
            ch.list_sessions(None, as_json=True)
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data), 2)
        for key in ("path", "session_id", "project", "mtime", "size_kb",
                    "title", "prompt"):
            self.assertIn(key, data[0])
        self.assertEqual(data[0]["title"], "Fix login bug in auth.py")

    def test_list_json_with_grep_includes_match(self):
        import contextlib
        import io
        buf = io.StringIO()
        with unittest.mock.patch.object(
                ch.discovery, "find_sessions",
                lambda *a, **k: [AGENT_SESSION, FIXTURE]), \
                contextlib.redirect_stdout(buf):
            ch.list_sessions(None, grep="unicode", as_json=True)
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data), 1)
        self.assertIn("unicode", data[0]["match"].lower())

    def test_cli_list_format_json(self):
        import contextlib
        import io
        buf = io.StringIO()
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
                                        lambda *a, **k: [FIXTURE]), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            ch.main(["--list", "--format", "json"])
        data = json.loads(buf.getvalue())
        self.assertEqual(data[0]["session_id"], "classic_session")


class FitTests(unittest.TestCase):
    """--fit: size the deterministic handoff to a token budget."""

    def test_parse_budget_forms(self):
        self.assertEqual(ch._parse_budget("32k"), 32_000)
        self.assertEqual(ch._parse_budget("1m"), 1_000_000)
        self.assertEqual(ch._parse_budget("128000"), 128_000)

    def test_parse_budget_rejects_garbage(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            ch.build_arg_parser().parse_args(["--fit", "banana"])

    def test_fit_caps_document_size(self):
        import contextlib
        import io
        parsed = ch.parse_session(FIXTURE)
        parsed["turns"].append({"role": "user",
                                "text_parts": ["x" * 50_000],
                                "tools": [], "ts": None})
        args = ch.build_arg_parser().parse_args([str(FIXTURE), "--fit", "2k"])
        with contextlib.redirect_stderr(io.StringIO()):
            doc = ch.build_document(parsed, FIXTURE, args)
        self.assertLess(len(doc), 12_000)         # ≈2k tokens, not 50k chars
        self.assertIn("omitted", doc)

    def test_fit_conflicts_with_llm_and_max_chars(self):
        for extra in (["--llm", "claude"], ["--max-chars", "9000"]):
            with self.assertRaises(SystemExit) as cm:
                ch.main([str(FIXTURE), "--fit", "8k", *extra])
            self.assertIn("--fit", str(cm.exception))

    def test_fit_reports_estimate_with_target(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "h.md"
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ch.main([str(FIXTURE), "--fit", "8k", "-o", str(out)])
            self.assertTrue(out.exists())
        self.assertIn("tokens", buf.getvalue())
        self.assertIn("target", buf.getvalue())

    def test_every_write_reports_token_estimate(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "h.md"
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ch.main([str(FIXTURE), "-o", str(out)])
        self.assertIn("tokens", buf.getvalue())


class MultiParamTests(unittest.TestCase):
    """0.15.0: multiple positional paths merge; --grep ANDs; --project ORs."""

    def test_multiple_paths_produce_merged_handoff(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "h.md"
            with contextlib.redirect_stderr(io.StringIO()):
                ch.main([str(COMPACTED), str(FIXTURE), "-o", str(out)])
            doc = out.read_text(encoding="utf-8")
        self.assertIn("Session 1", doc)
        self.assertIn("Session 2", doc)
        self.assertIn("rate limiting", doc)          # from COMPACTED
        self.assertIn("unicode", doc)                # from FIXTURE

    def test_multiple_paths_must_all_be_files(self):
        with self.assertRaises(SystemExit) as cm:
            ch.main(["nope-a.jsonl", "nope-b.jsonl"])
        self.assertIn("file", str(cm.exception).lower())

    def test_single_positional_name_still_works(self):
        import contextlib
        import io
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
                                        lambda *a, **k: [FIXTURE]), \
                contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args(["login bug"])
            self.assertEqual(ch.resolve_source(args), FIXTURE)

    def test_grep_and_semantics(self):
        with unittest.mock.patch.object(
                ch.discovery, "find_sessions",
                lambda *a, **k: [AGENT_SESSION, FIXTURE]):
            both = ch.grep_sessions(["unicode", "commit"])
            self.assertEqual([p for p, _ in both], [FIXTURE])
            self.assertEqual(ch.grep_sessions(["unicode", "zzz-nope"]), [])
            single = ch.grep_sessions("unicode")     # str still accepted
            self.assertEqual([p for p, _ in single], [FIXTURE])

    def test_cli_grep_appends(self):
        args = ch.build_arg_parser().parse_args(
            ["--grep", "unicode", "--grep", "commit"])
        self.assertEqual(args.grep, ["unicode", "commit"])

    def test_find_sessions_accepts_many_project_filters(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pd = Path(td)
            for name in ("-projA", "-projB", "-projC"):
                d = pd / name
                d.mkdir()
                (d / "s.jsonl").write_text('{"type":"summary"}\n',
                                           encoding="utf-8")
            hits = ch.find_sessions(["projA", "projC"], projects_dir=pd)
            self.assertEqual(sorted(h.parent.name for h in hits),
                             ["-projA", "-projC"])
            one = ch.find_sessions("projB", projects_dir=pd)
            self.assertEqual([h.parent.name for h in one], ["-projB"])

    def test_brief_refuses_multiple_projects(self):
        with self.assertRaises(SystemExit) as cm:
            ch.main(["--brief", "--project", "a", "--project", "b"])
        self.assertIn("single project", str(cm.exception))


class GrepTests(unittest.TestCase):
    """--grep: find sessions by conversation content, not just title."""

    def _patch(self):
        return unittest.mock.patch.object(
            ch.discovery, "find_sessions",
            lambda *a, **k: [AGENT_SESSION, FIXTURE])

    def test_grep_matches_conversation_text(self):
        with self._patch():
            hits = ch.grep_sessions("unicode")
        self.assertEqual([p for p, _ in hits], [FIXTURE])
        self.assertIn("unicode", hits[0][1].lower())    # preview has context

    def test_may_contain_spans_block_boundaries(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.jsonl"
            p.write_text('{"a": "hello GrepMe world"}', encoding="utf-8")
            self.assertTrue(
                ch.discovery._may_contain(p, "grepme", block_size=8))
            self.assertFalse(
                ch.discovery._may_contain(p, "absent!", block_size=8))

    def test_grep_matches_greek_text(self):
        with self._patch():                      # non-ASCII: full-parse path
            hits = ch.grep_sessions("σπάει")
        self.assertEqual([p for p, _ in hits], [FIXTURE])

    def test_grep_is_case_insensitive(self):
        with self._patch():
            self.assertEqual(len(ch.grep_sessions("UNICODE")), 1)

    def test_grep_ignores_tool_noise(self):
        with self._patch():
            self.assertEqual(ch.grep_sessions("3 passed"), [])  # tool result

    def test_resolve_source_grep_picks_newest_match(self):
        import contextlib
        import io
        with self._patch(), contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args(["--grep", "unicode"])
            self.assertEqual(ch.resolve_source(args), FIXTURE)

    def test_resolve_source_grep_no_match_hints(self):
        import contextlib
        import io
        with self._patch(), contextlib.redirect_stderr(io.StringIO()):
            args = ch.build_arg_parser().parse_args(["--grep", "zzz-nope"])
            with self.assertRaises(SystemExit) as cm:
                ch.resolve_source(args)
        self.assertIn("--list", str(cm.exception))

    def test_grep_refuses_name_combo(self):
        args = ch.build_arg_parser().parse_args(["--grep", "x", "--name", "y"])
        with self.assertRaises(SystemExit) as cm:
            ch.resolve_source(args)
        self.assertIn("--grep", str(cm.exception))

    def test_list_with_grep_shows_only_matches_with_preview(self):
        import contextlib
        import io
        buf = io.StringIO()
        with self._patch(), contextlib.redirect_stdout(buf):
            ch.list_sessions(None, grep="unicode")
        out = buf.getvalue()
        self.assertIn("🔍", out)
        self.assertIn("classic_", out)
        self.assertNotIn("agent_se", out)

    def test_interactive_pick_with_grep_filters_list(self):
        import contextlib
        import io
        with self._patch(), \
                unittest.mock.patch.object(ch.sys.stdin, "isatty",
                                           lambda: True), \
                unittest.mock.patch("builtins.input", side_effect=["1"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ch.interactive_pick(None, grep="unicode"),
                             [FIXTURE])


class PickerTests(unittest.TestCase):
    def test_interactive_pick_by_number(self):
        import contextlib
        import io
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
                                        lambda *a, **k: [TRIVIAL, FIXTURE]), \
                unittest.mock.patch.object(ch.sys.stdin, "isatty",
                                           lambda: True), \
                unittest.mock.patch.object(ch.sys.stderr, "isatty",
                                           lambda: True, create=True), \
                unittest.mock.patch("builtins.input",
                                    side_effect=["nope", "2"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ch.interactive_pick(None), [FIXTURE])

    def test_parse_pick_forms(self):
        self.assertEqual(ch.discovery._parse_pick("2", 5), [2])
        self.assertEqual(ch.discovery._parse_pick("1,3", 5), [1, 3])
        self.assertEqual(ch.discovery._parse_pick("2-4", 5), [2, 3, 4])
        self.assertEqual(ch.discovery._parse_pick("3,1-2,3", 5), [1, 2, 3])
        self.assertEqual(ch.discovery._parse_pick("0", 5), [])
        self.assertEqual(ch.discovery._parse_pick("6", 5), [])
        self.assertEqual(ch.discovery._parse_pick("x", 5), [])

    def test_interactive_pick_returns_multiple(self):
        import contextlib
        import io
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
                                        lambda *a, **k: [TRIVIAL, FIXTURE]), \
                unittest.mock.patch.object(ch.sys.stdin, "isatty",
                                           lambda: True), \
                unittest.mock.patch("builtins.input",
                                    side_effect=["1,2"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ch.interactive_pick(None), [TRIVIAL, FIXTURE])

    def test_main_interactive_multi_select_merges(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "h.md"
            with unittest.mock.patch.object(
                    ch.discovery, "find_sessions",
                    lambda *a, **k: [COMPACTED, FIXTURE]), \
                    unittest.mock.patch.object(ch.sys.stdin, "isatty",
                                               lambda: True), \
                    unittest.mock.patch("builtins.input",
                                        side_effect=["1,2"]), \
                    contextlib.redirect_stderr(io.StringIO()):
                ch.main(["-i", "-o", str(out)])
            doc = out.read_text(encoding="utf-8")
        self.assertIn("Session 1", doc)
        self.assertIn("Session 2", doc)
        self.assertIn("unicode", doc)              # from FIXTURE
        self.assertIn("rate limiting", doc)        # from COMPACTED

    def test_interactive_needs_terminal(self):
        with unittest.mock.patch.object(ch.discovery, "find_sessions",
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

    def test_completions_cover_chf_alias(self):
        import contextlib
        import io
        for shell in ("bash", "zsh"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ch.print_completions(shell)
            last = buf.getvalue().strip().splitlines()[-1]
            self.assertIn("claude-handoff", last)
            self.assertIn("chf", last.split())


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
        proc = sp.run(  # noqa: PLW1510 — returncode checked via replies
            [sys.executable, "-m", "claude_handoff", "--mcp"], cwd=str(ROOT),
            input=req, capture_output=True, text=True, encoding="utf-8",
            timeout=60, env={**os.environ, "PYTHONUTF8": "1"})
        replies = {m["id"]: m for m in
                   (json.loads(line) for line in
                    proc.stdout.splitlines() if line.strip())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"],
                         "claude-handoff")
        tools = {t["name"] for t in replies[2]["result"]["tools"]}
        self.assertEqual(tools, {"list_sessions", "handoff"})
        ok = replies[3]["result"]
        self.assertFalse(ok["isError"])
        self.assertIn("Conversation handoff", ok["content"][0]["text"])
        self.assertIn("last 1 of 2", ok["content"][0]["text"])
        self.assertTrue(replies[4]["result"]["isError"])


class McpLlmTests(unittest.TestCase):
    """--mcp --allow-llm: explicit opt-in for LLM summaries over MCP."""

    def _fake_provider(self, calls):
        def fake_call(key, model, prompt):
            calls.append(prompt)
            return "## Goal\nSummarized over MCP."
        return unittest.mock.patch.dict(ch.PROVIDERS["claude-cli"],
                                        {"call": fake_call})

    def test_llm_arg_refused_without_flag(self):
        with self.assertRaises(ValueError) as cm:
            ch._mcp_call("handoff", {"path": str(FIXTURE),
                                     "llm": "claude-cli"})
        self.assertIn("--allow-llm", str(cm.exception))

    def test_llm_summary_with_flag(self):
        import contextlib
        import io
        calls = []
        with self._fake_provider(calls), \
                contextlib.redirect_stderr(io.StringIO()):
            out = ch._mcp_call("handoff", {"path": str(FIXTURE),
                                           "llm": "claude-cli"},
                               allow_llm=True)
        self.assertIn("Summarized over MCP", out)
        self.assertTrue(calls)                     # provider really invoked

    def test_tools_schema_advertises_llm_only_with_flag(self):
        without = json.dumps(ch._mcp_tools())
        withit = json.dumps(ch._mcp_tools(allow_llm=True))
        self.assertNotIn('"llm"', without)
        self.assertIn('"llm"', withit)


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
                unittest.mock.patch.object(ch.llm, "LLM_INPUT_CAP", 1000), \
                unittest.mock.patch.object(ch.llm, "CHUNK_CAP", 800), \
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
        with unittest.mock.patch.object(ch.llm, "http_json", fake_http), \
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
        with unittest.mock.patch.object(ch.llm, "http_json", fake_http), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                ch.llm_summarize("ollama", None, "x")
        self.assertIn("ollama serve", str(cm.exception))


class RedactOutputTests(unittest.TestCase):
    """v0.10.0: the final document is egress too — redacted by default."""

    def _doc(self, *extra):
        import contextlib
        import io
        parsed = ch.parse_session(SECRET)
        args = ch.build_arg_parser().parse_args([str(SECRET), *extra])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            doc = ch.build_document(parsed, SECRET, args)
        return doc, buf.getvalue()

    def test_deterministic_doc_redacted_by_default(self):
        doc, err = self._doc()
        self.assertNotIn("sk-ant-abc123", doc)
        self.assertNotIn("ghp_abcdefghij", doc)
        self.assertIn("[REDACTED]", doc)
        self.assertIn("Redacted", err)
        self.assertIn("--no-redact", err)

    def test_no_redact_keeps_content(self):
        doc, err = self._doc("--no-redact")
        self.assertIn("sk-ant-abc123def456ghi789jkl", doc)
        self.assertNotIn("Redacted", err)

    def test_json_format_redacted_and_still_valid(self):
        doc, _ = self._doc("--format", "json")
        data = json.loads(doc)                    # redaction kept JSON valid
        self.assertNotIn("sk-ant-abc123", doc)
        self.assertIn("deploy", str(data["turns"]))

    def test_mcp_handoff_redacted(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            out = ch._mcp_call("handoff", {"path": str(SECRET)})
        self.assertNotIn("sk-ant-abc123", out)
        self.assertIn("[REDACTED]", out)

    def test_hook_output_redacted(self):
        import contextlib
        import io
        import tempfile
        payload = json.dumps({"transcript_path": str(SECRET)})
        with tempfile.TemporaryDirectory() as td:
            with unittest.mock.patch.object(ch.integrations, "HANDOFFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO(payload)), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                ch.run_hook_mode()
            files = list(Path(td).glob("*.md"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding="utf-8")
            self.assertNotIn("sk-ant-abc123", text)
            self.assertIn("[REDACTED]", text)


class BriefTests(unittest.TestCase):
    """--brief: distill a project's whole history into one memory doc."""

    def test_deterministic_brief_content(self):
        import contextlib
        import io
        parsed_list = [ch.parse_session(COMPACTED), ch.parse_session(FIXTURE)]
        with contextlib.redirect_stderr(io.StringIO()):
            doc = ch.build_brief_deterministic(parsed_list, "home/vspapg/myapp")
        self.assertIn("# Project brief: home/vspapg/myapp", doc)
        self.assertIn("2 sessions", doc)
        self.assertIn("Fix login bug in auth.py", doc)    # FIXTURE title
        self.assertIn("auth.py", doc)                     # activity rollup
        self.assertIn("abc123"[:8], doc)                  # session citation

    def test_timeline_sorted_by_start_time_not_input_order(self):
        import contextlib
        import io
        a, b = ch.parse_session(FIXTURE), ch.parse_session(COMPACTED)
        with contextlib.redirect_stderr(io.StringIO()):
            doc = ch.build_brief_deterministic([a, b], "x")
            doc2 = ch.build_brief_deterministic([b, a], "x")
        self.assertEqual(doc, doc2)                # input order irrelevant

    def test_brief_cli_writes_to_briefs_dir(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with unittest.mock.patch.object(
                    ch.brief, "find_sessions",
                    lambda *a, **k: [FIXTURE, AGENT_SESSION]), \
                    unittest.mock.patch.object(
                        ch.cli, "cwd_project_filter",
                        lambda *a, **k: "-home-vspapg-myapp"), \
                    unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                               Path(td)), \
                    contextlib.redirect_stderr(io.StringIO()):
                ch.main(["--brief"])
            out = Path(td) / "-home-vspapg-myapp.md"
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertIn("# Project brief", text)
            stamp = ch.brief.parse_stamp(text)      # stamped at write time
            self.assertIsNotNone(stamp)
            self.assertEqual(stamp["provider"], "none")

    def test_brief_requires_project_scope(self):
        with unittest.mock.patch.object(ch.cli, "cwd_project_filter",
                                        lambda *a, **k: None):
            with self.assertRaises(SystemExit) as cm:
                ch.main(["--brief"])
        self.assertIn("--project", str(cm.exception))

    def _fake_provider(self, calls):
        def fake_call(key, model, prompt):
            calls.append(prompt)
            return f"NOTE{len(calls)}: decisions here"
        return unittest.mock.patch.dict(ch.PROVIDERS["claude-cli"],
                                        {"call": fake_call})

    def test_llm_brief_maps_each_session_then_reduces(self):
        import contextlib
        import io
        calls = []
        parsed_list = [ch.parse_session(COMPACTED), ch.parse_session(FIXTURE)]
        with self._fake_provider(calls), \
                contextlib.redirect_stderr(io.StringIO()):
            doc = ch.build_brief_llm(parsed_list, "home/vspapg/myapp",
                                     "claude-cli", None, use_cache=False)
        self.assertEqual(len(calls), 3)            # 2 session notes + reduce
        self.assertIn("abc123", calls[1])          # note prompt cites its id
        self.assertIn("NOTE1", calls[-1])          # reduce sees the notes
        self.assertIn("# Project brief: home/vspapg/myapp", doc)
        self.assertIn("## Session timeline", doc)  # factual skeleton kept
        self.assertIn("NOTE3", doc)                # reduce output appended

    def test_llm_brief_notes_cached_incrementally(self):
        import contextlib
        import io
        import tempfile
        calls = []
        parsed_list = [ch.parse_session(COMPACTED), ch.parse_session(FIXTURE)]
        with tempfile.TemporaryDirectory() as td, \
                self._fake_provider(calls), \
                unittest.mock.patch.object(ch.llm, "CACHE_DIR", Path(td)), \
                contextlib.redirect_stderr(io.StringIO()):
            ch.build_brief_llm(parsed_list, "x", "claude-cli", None)
            first = len(calls)
            ch.build_brief_llm(parsed_list, "x", "claude-cli", None)
            second = len(calls) - first
        self.assertEqual(first, 3)
        self.assertEqual(second, 1)                # only the reduce re-runs

    def test_cli_brief_llm_writes_distillation(self):
        import contextlib
        import io
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as td, \
                self._fake_provider(calls), \
                unittest.mock.patch.object(
                    ch.brief, "find_sessions",
                    lambda *a, **k: [FIXTURE]), \
                unittest.mock.patch.object(
                    ch.cli, "cwd_project_filter",
                    lambda *a, **k: "-home-vspapg-myapp"), \
                unittest.mock.patch.object(ch.brief, "BRIEFS_DIR", Path(td)), \
                contextlib.redirect_stderr(io.StringIO()):
            ch.main(["--brief", "--llm", "claude-cli", "--no-cache"])
            text = (Path(td) / "-home-vspapg-myapp.md") \
                .read_text(encoding="utf-8")
        self.assertIn("decisions here", text)

    def test_llm_brief_demotes_distilled_headings(self):
        import contextlib
        import io

        def fake_call(key, model, prompt):
            return ("## Decisions\n- chose X [abc123]\n\n"
                    "## Open threads\n- pending Y [abc123]")

        parsed_list = [ch.parse_session(FIXTURE)]
        with unittest.mock.patch.dict(ch.PROVIDERS["claude-cli"],
                                      {"call": fake_call}), \
                contextlib.redirect_stderr(io.StringIO()):
            doc = ch.build_brief_llm(parsed_list, "home/vspapg/myapp",
                                     "claude-cli", None, use_cache=False)
        distilled = doc[doc.index("## Distilled memory"):]
        # LLM section headings nest UNDER the marker, never rival it
        self.assertIn("### Decisions", distilled)
        self.assertIn("### Open threads", distilled)
        self.assertNotIn("\n## Decisions", distilled)
        self.assertIn("## Session timeline", doc)   # skeleton stays H2

    def test_plain_brief_preserves_existing_distillation(self):
        import contextlib
        import io
        import tempfile
        old = ("<!-- claude-handoff-brief v=1 built=10 sessions=1 "
               "newest_mtime=10 distilled=1500 distilled_sessions=1 "
               "provider=claude-cli -->\n"
               "# Project brief: old\n\n## Session timeline\n\n- old\n\n"
               "## Distilled memory\n\n- Redis for drafts [abc123]\n")
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                old, encoding="utf-8")
            with unittest.mock.patch.object(
                    ch.brief, "find_sessions",
                    lambda *a, **k: [FIXTURE]), \
                    unittest.mock.patch.object(
                        ch.cli, "cwd_project_filter",
                        lambda *a, **k: "-home-vspapg-myapp"), \
                    unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                               Path(td)), \
                    contextlib.redirect_stderr(io.StringIO()):
                ch.main(["--brief"])
            text = (Path(td) / "-home-vspapg-myapp.md") \
                .read_text(encoding="utf-8")
        self.assertIn("Fix login bug in auth.py", text)     # fresh skeleton
        self.assertIn("- Redis for drafts [abc123]", text)  # distilled kept
        stamp = ch.brief.parse_stamp(text)
        self.assertEqual(stamp["distilled"], 1500)          # stamp carried
        self.assertEqual(stamp["provider"], "claude-cli")

    def test_plain_brief_to_explicit_output_stays_deterministic(self):
        import contextlib
        import io
        import tempfile
        old = ("<!-- claude-handoff-brief v=1 built=10 sessions=1 "
               "newest_mtime=10 distilled=1500 distilled_sessions=1 "
               "provider=claude-cli -->\n"
               "# Project brief: old\n\n"
               "## Distilled memory\n\n- Redis for drafts [abc123]\n")
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                old, encoding="utf-8")
            out = Path(td) / "elsewhere.md"
            with unittest.mock.patch.object(
                    ch.brief, "find_sessions",
                    lambda *a, **k: [FIXTURE]), \
                    unittest.mock.patch.object(
                        ch.cli, "cwd_project_filter",
                        lambda *a, **k: "-home-vspapg-myapp"), \
                    unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                               Path(td)), \
                    contextlib.redirect_stderr(io.StringIO()):
                ch.main(["--brief", "-o", str(out)])
            text = out.read_text(encoding="utf-8")
        self.assertIn("Fix login bug in auth.py", text)
        self.assertNotIn("Redis for drafts", text)   # -o: plain skeleton

    def test_monster_session_note_is_map_reduced(self):
        import contextlib
        import io
        calls = []
        parsed = ch.parse_session(FIXTURE)
        with self._fake_provider(calls), \
                unittest.mock.patch.object(ch.brief, "NOTE_INPUT_CAP", 400), \
                contextlib.redirect_stderr(io.StringIO()):
            note = ch.brief._session_note(parsed, "claude-cli", None,
                                          redact=True, use_cache=False)
        # transcript >> 400 chars → several chunk notes + one synthesis,
        # never a single truncated call
        self.assertGreater(len(calls), 2)
        self.assertIn("NOTE1", calls[-1])            # synthesis sees parts
        self.assertIn("abc123", calls[-1])           # citation survives
        self.assertTrue(note)

    def test_small_session_note_is_one_call(self):
        import contextlib
        import io
        calls = []
        parsed = ch.parse_session(FIXTURE)
        with self._fake_provider(calls), \
                contextlib.redirect_stderr(io.StringIO()):
            ch.brief._session_note(parsed, "claude-cli", None,
                                   redact=True, use_cache=False)
        self.assertEqual(len(calls), 1)

    def test_brief_to_stdout(self):
        import contextlib
        import io
        buf = io.StringIO()
        with unittest.mock.patch.object(
                ch.brief, "find_sessions",
                lambda *a, **k: [FIXTURE]), \
                unittest.mock.patch.object(
                    ch.cli, "cwd_project_filter",
                    lambda *a, **k: "-home-vspapg-myapp"), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            ch.main(["--brief", "-o", "-"])
        self.assertIn("# Project brief", buf.getvalue())


def _synth_parsed(i: int) -> dict:
    """Minimal parsed-session dict for volume tests."""
    p = ch._new_parse_state()
    p["meta"].update(session_id=f"synth{i:03d}",
                     first_ts=f"2026-07-{(i % 28) + 1:02d}T10:00:00Z",
                     last_ts=f"2026-07-{(i % 28) + 1:02d}T11:00:00Z",
                     n_user=1, summaries=[f"synthetic session {i}"])
    p["turns"] = [{"role": "user", "text_parts": [f"work {i}"],
                   "tools": [], "ts": None}]
    for k in [k for k in p if k.startswith("_")]:
        p.pop(k)
    return p


class BriefFreshnessTests(unittest.TestCase):
    """Stamps, timeline caps, and the SessionEnd auto-update policy."""

    def test_timeline_caps_at_20_sessions(self):
        import contextlib
        import io
        parsed_list = [_synth_parsed(i) for i in range(25)]
        with contextlib.redirect_stderr(io.StringIO()):
            doc = ch.build_brief_deterministic(parsed_list, "x")
        self.assertEqual(doc.count("`synth"), 20)         # capped bullets
        self.assertIn("5 earlier session(s) omitted", doc)
        self.assertIn("synth024", doc)                    # newest kept

    def test_stamp_roundtrip(self):
        stamp = ch.brief.make_stamp(sessions=5, newest_mtime=1000,
                                    distilled=900, distilled_sessions=4,
                                    provider="claude-cli", now=1234)
        meta = ch.brief.parse_stamp("x\n" + stamp + "\ny")
        self.assertEqual(meta, {"built": 1234, "sessions": 5,
                                "newest_mtime": 1000, "distilled": 900,
                                "distilled_sessions": 4,
                                "provider": "claude-cli"})
        self.assertIsNone(ch.brief.parse_stamp("no stamp here"))

    def test_update_refreshes_skeleton_keeps_distilled_with_note(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old = ("<!-- claude-handoff-brief v=1 built=10 sessions=1 "
                   "newest_mtime=10 distilled=10 distilled_sessions=1 "
                   "provider=claude-cli -->\n"
                   "# Project brief: old\n\n## Session timeline\n\n- old\n\n"
                   "## Distilled memory\n\n- Redis for drafts [abc123]\n")
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                old, encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(
                        ch.brief, "find_sessions",
                        lambda *a, **k: [FIXTURE, AGENT_SESSION]), \
                    contextlib.redirect_stderr(io.StringIO()):
                changed = ch.brief.update_brief_skeleton("-home-vspapg-myapp")
            text = (Path(td) / "-home-vspapg-myapp.md") \
                .read_text(encoding="utf-8")
        self.assertTrue(changed)
        self.assertIn("Fix login bug in auth.py", text)   # fresh timeline
        self.assertIn("- Redis for drafts [abc123]", text)  # distilled kept
        self.assertIn("newer session(s) since this distillation", text)
        self.assertIn("claude-handoff-brief v=1", text)   # re-stamped

    def _fake_repo(self, td, entries):
        """Write a synthetic .git/logs/HEAD reflog — (ts, sha, subject)."""
        logs = Path(td) / ".git" / "logs"
        logs.mkdir(parents=True)
        lines = [f"{'0' * 40} {sha} You <y@x> {ts} +0200\tcommit: {subject}"
                 for ts, sha, subject in entries]
        (logs / "HEAD").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_brief_shows_repo_head_from_reflog(self):
        import tempfile
        parsed = ch.parse_session(FIXTURE)
        with tempfile.TemporaryDirectory() as td:
            self._fake_repo(td, [(1000, "a" * 40, "init"),
                                 (2000, "b" * 40, "add rate limiter")])
            doc = ch.brief.build_brief_deterministic([parsed], td)
        self.assertIn("Repo HEAD `bbbbbbbb`", doc)     # newest entry wins
        self.assertIn("add rate limiter", doc)
        doc2 = ch.brief.build_brief_deterministic([parsed], "home/x/y")
        self.assertNotIn("Repo HEAD", doc2)            # no repo → no line

    def test_update_notes_commits_after_distillation(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
                tempfile.TemporaryDirectory() as repo:
            self._fake_repo(repo, [(1000, "a" * 40, "init"),
                                   (2000, "b" * 40, "fix parser")])
            old = ("<!-- claude-handoff-brief v=1 built=10 sessions=1 "
                   "newest_mtime=10 distilled=1500 distilled_sessions=1 "
                   "provider=claude-cli -->\n"
                   "# Project brief: old\n\n## Session timeline\n\n- old\n\n"
                   "## Distilled memory\n\n- Redis for drafts [abc123]\n")
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                old, encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(
                        ch.brief, "find_sessions",
                        lambda *a, **k: [FIXTURE]), \
                    unittest.mock.patch.object(
                        ch.brief, "brief_label",
                        lambda *a, **k: repo), \
                    contextlib.redirect_stderr(io.StringIO()):
                ch.brief.update_brief_skeleton("-home-vspapg-myapp")
                ch.brief.update_brief_skeleton("-home-vspapg-myapp")
            text = (Path(td) / "-home-vspapg-myapp.md") \
                .read_text(encoding="utf-8")
        # one commit (ts 2000) landed after the distillation (ts 1500)
        self.assertIn("1 commit(s) landed after this distillation", text)
        self.assertIn("fix parser", text)
        # refreshing twice never stacks the note
        self.assertEqual(text.count("landed after this distillation"), 1)
        self.assertIn("- Redis for drafts [abc123]", text)  # distilled kept

    def test_update_leaves_unknown_files_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "-p.md"
            target.write_text("hand-written notes, no stamp",
                              encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)):
                self.assertFalse(ch.brief.update_brief_skeleton("-p"))
            self.assertEqual(target.read_text(encoding="utf-8"),
                             "hand-written notes, no stamp")

    def test_update_without_existing_brief_is_noop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)):
                self.assertFalse(ch.brief.update_brief_skeleton("-none"))
            self.assertEqual(list(Path(td).iterdir()), [])


class VisibleFailureTests(unittest.TestCase):
    """Tolerant is not mute: swallowed errors report to stderr (never
    fatal, exit codes and stdout untouched); CLAUDE_HANDOFF_DEBUG=1
    surfaces the by-design-tolerant paths too."""

    def test_warn_helper_prefixes_stderr(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ch.textutil.warn("brief hook", ValueError("boom"))
        self.assertIn("[claude-handoff] brief hook: boom", buf.getvalue())

    def test_hooks_report_their_error_on_stderr(self):
        import contextlib
        import io
        for mode in (ch.run_hook_mode, ch.run_brief_hook_mode,
                     ch.run_brief_update_mode):
            out, err = io.StringIO(), io.StringIO()
            with unittest.mock.patch.object(sys, "stdin",
                                            io.StringIO("{broken")), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                mode()                              # must not raise
            self.assertEqual(out.getvalue(), "")    # stdout stays clean
            self.assertIn("[claude-handoff]", err.getvalue())

    def test_grep_reports_unreadable_files(self):
        import contextlib
        import io
        ghost = Path("/nonexistent/ghost-session.jsonl")
        err = io.StringIO()
        with unittest.mock.patch.object(
                ch.discovery, "find_sessions",
                lambda *a, **k: [ghost, FIXTURE]), \
                contextlib.redirect_stderr(err):
            hits = ch.grep_sessions("unicode")
        self.assertEqual([p for p, _ in hits], [FIXTURE])
        self.assertIn("1 unreadable", err.getvalue())

    def test_debug_surfaces_corrupt_lines(self):
        import contextlib
        import io
        err = io.StringIO()
        with unittest.mock.patch.dict(ch.os.environ,
                                      {"CLAUDE_HANDOFF_DEBUG": "1"}), \
                contextlib.redirect_stderr(err):
            ch.parse_session(AGENT_SESSION)     # aaa111 has a corrupt line
        self.assertIn("corrupt line", err.getvalue())

    def test_debug_flag_enables_the_channel(self):
        import contextlib
        import io
        import tempfile
        err = io.StringIO()
        env = dict(ch.os.environ)
        env.pop("CLAUDE_HANDOFF_DEBUG", None)
        with tempfile.TemporaryDirectory() as td, \
                unittest.mock.patch.dict(ch.os.environ, env, clear=True), \
                contextlib.redirect_stderr(err):
            ch.main([str(AGENT_SESSION), "--debug",
                     "-o", str(Path(td) / "h.md")])
        self.assertIn("corrupt line", err.getvalue())

    def test_silent_by_default_for_tolerant_paths(self):
        import contextlib
        import io
        err = io.StringIO()
        env = dict(ch.os.environ)
        env.pop("CLAUDE_HANDOFF_DEBUG", None)
        with unittest.mock.patch.dict(ch.os.environ, env, clear=True), \
                contextlib.redirect_stderr(err):
            ch.parse_session(AGENT_SESSION)
        self.assertNotIn("corrupt", err.getvalue())


class InjectionDefenseTests(unittest.TestCase):
    """Untrusted transcript content must be framed as data, never as
    instructions — at every LLM consumption point and in every output a
    downstream model will read."""

    GUARD = "data"

    def test_all_llm_prompts_carry_data_not_instructions_guard(self):
        for name, prompt in (("SUMMARY_PROMPT", ch.SUMMARY_PROMPT),
                             ("CHUNK_PROMPT", ch.CHUNK_PROMPT),
                             ("SESSION_NOTE_PROMPT",
                              ch.brief.SESSION_NOTE_PROMPT),
                             ("SESSION_CHUNK_PROMPT",
                              ch.brief.SESSION_CHUNK_PROMPT),
                             ("SESSION_NOTE_REDUCE_PROMPT",
                              ch.brief.SESSION_NOTE_REDUCE_PROMPT),
                             ("BRIEF_PROMPT", ch.brief.BRIEF_PROMPT)):
            self.assertIn("not instructions", prompt,
                          f"{name} lacks the injection guard")

    def test_handoff_preamble_warns_receiving_model(self):
        parsed = ch.parse_session(FIXTURE)
        doc = ch.build_deterministic(parsed, FIXTURE,
                                     include_tools=False, max_chars=80_000)
        self.assertIn("data, not instructions", doc)

    def test_brief_injection_wrapper_warns_model(self):
        import contextlib
        import io
        import tempfile
        payload = json.dumps({"cwd": "/home/vspapg/myapp"})
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                "# Project brief: x\n", encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(
                        ch.integrations, "cwd_project_filter",
                        lambda cwd=None, *a, **k: "-home-vspapg-myapp"), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO(payload)), \
                    contextlib.redirect_stdout(buf):
                ch.run_brief_hook_mode()
        self.assertIn("data, not instructions", buf.getvalue())


class PreCompactHookTests(unittest.TestCase):
    """Both hooks also fire on PreCompact — snapshot before detail is
    compacted away, and keep the brief skeleton fresh mid-session."""

    def _cmds(self, data, event):
        return [h["command"] for e in data.get("hooks", {}).get(event, [])
                for h in e.get("hooks", [])]

    def test_handoff_hook_registers_precompact(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "settings.json"
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                ch.install_hook(settings_path=sp)
            data = json.loads(sp.read_text(encoding="utf-8"))
            self.assertIn(ch.HOOK_COMMAND, self._cmds(data, "SessionEnd"))
            self.assertIn(ch.HOOK_COMMAND, self._cmds(data, "PreCompact"))
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                ch.install_hook(settings_path=sp, remove=True)
            data = json.loads(sp.read_text(encoding="utf-8"))
            self.assertNotIn("PreCompact", data.get("hooks", {}))

    def test_brief_hook_registers_precompact_update(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "settings.json"
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                ch.install_brief_hook(settings_path=sp)
            data = json.loads(sp.read_text(encoding="utf-8"))
            self.assertIn(ch.BRIEF_UPDATE_COMMAND,
                          self._cmds(data, "PreCompact"))


class BriefHookTests(unittest.TestCase):
    """SessionStart hook: inject the project brief as session context."""

    def test_install_is_idempotent_and_removable(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "settings.json"
            sp.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                ch.install_brief_hook(settings_path=sp)
                ch.install_brief_hook(settings_path=sp)      # idempotent
            data = json.loads(sp.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "opus")          # untouched
            entries = data["hooks"]["SessionStart"]
            cmds = [h["command"] for e in entries for h in e["hooks"]]
            self.assertEqual(cmds.count(ch.BRIEF_HOOK_COMMAND), 1)
            self.assertIn("startup", entries[0]["matcher"])
            end_cmds = [h["command"]
                        for e in data["hooks"]["SessionEnd"]
                        for h in e["hooks"]]
            self.assertEqual(end_cmds.count(ch.BRIEF_UPDATE_COMMAND), 1)
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                ch.install_brief_hook(settings_path=sp, remove=True)
            data = json.loads(sp.read_text(encoding="utf-8"))
            self.assertNotIn("SessionStart", data.get("hooks", {}))
            self.assertNotIn("SessionEnd", data.get("hooks", {}))

    def test_update_mode_calls_skeleton_refresh(self):
        import contextlib
        import io
        seen = []
        payload = json.dumps({"cwd": "/home/vspapg/myapp"})
        with unittest.mock.patch.object(
                ch.integrations, "cwd_project_filter",
                lambda cwd=None, *a, **k: "-home-vspapg-myapp"), \
                unittest.mock.patch.object(ch.integrations,
                                           "update_brief_skeleton",
                                           seen.append), \
                unittest.mock.patch.object(sys, "stdin",
                                           io.StringIO(payload)), \
                contextlib.redirect_stdout(io.StringIO()):
            ch.run_brief_update_mode()
        self.assertEqual(seen, ["-home-vspapg-myapp"])

    def test_update_mode_swallows_errors(self):
        import contextlib
        import io
        with unittest.mock.patch.object(sys, "stdin",
                                        io.StringIO("{broken")), \
                contextlib.redirect_stdout(io.StringIO()):
            ch.run_brief_update_mode()             # must not raise

    def test_injection_warns_when_brief_is_stale(self):
        import contextlib
        import io
        import tempfile
        payload = json.dumps({"cwd": "/home/vspapg/myapp"})
        stale = (ch.brief.make_stamp(1, newest_mtime=10, distilled=0,
                                     distilled_sessions=0, provider="none")
                 + "\n# Project brief: x\n")
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                stale, encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(
                        ch.integrations, "cwd_project_filter",
                        lambda cwd=None, *a, **k: "-home-vspapg-myapp"), \
                    unittest.mock.patch.object(
                        ch.integrations, "find_sessions",
                        lambda *a, **k: [FIXTURE]), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO(payload)):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ch.run_brief_hook_mode()
        self.assertIn("newer than this brief", buf.getvalue())

    def test_injection_ignores_the_session_that_just_started(self):
        import contextlib
        import io
        import tempfile
        # the only "newer" session is the one this very hook fired for
        payload = json.dumps({"cwd": "/home/vspapg/myapp",
                              "transcript_path": str(FIXTURE)})
        stale = (ch.brief.make_stamp(1, newest_mtime=10, distilled=0,
                                     distilled_sessions=0, provider="none")
                 + "\n# Project brief: x\n")
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                stale, encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(
                        ch.integrations, "cwd_project_filter",
                        lambda cwd=None, *a, **k: "-home-vspapg-myapp"), \
                    unittest.mock.patch.object(
                        ch.integrations, "find_sessions",
                        lambda *a, **k: [FIXTURE]), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO(payload)):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ch.run_brief_hook_mode()
        self.assertIn("project-memory", buf.getvalue())
        self.assertNotIn("newer than this brief", buf.getvalue())

    def test_injection_warns_when_repo_moved_after_distillation(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
                tempfile.TemporaryDirectory() as repo:
            logs = Path(repo) / ".git" / "logs"
            logs.mkdir(parents=True)
            (logs / "HEAD").write_text(
                f"{'0' * 40} {'b' * 40} You <y@x> 2000 +0200"
                "\tcommit: fix parser\n", encoding="utf-8")
            payload = json.dumps({"cwd": repo})
            stale = (ch.brief.make_stamp(1, newest_mtime=9999999999,
                                         distilled=1500,
                                         distilled_sessions=1,
                                         provider="claude-cli")
                     + "\n# Project brief: x\n")
            (Path(td) / "-r.md").write_text(stale, encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(
                        ch.integrations, "cwd_project_filter",
                        lambda cwd=None, *a, **k: "-r"), \
                    unittest.mock.patch.object(
                        ch.integrations, "find_sessions",
                        lambda *a, **k: [FIXTURE]), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO(payload)):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ch.run_brief_hook_mode()
        out = buf.getvalue()
        # the commit at ts 2000 postdates the distillation at ts 1500
        self.assertIn("commit(s) newer than this memory", out)
        self.assertNotIn("newer than this brief", out)   # sessions are fine

    def test_hook_stdin_emits_brief_as_context(self):
        import contextlib
        import io
        import tempfile
        payload = json.dumps({"cwd": "/home/vspapg/myapp",
                              "hook_event_name": "SessionStart",
                              "session_id": "s1"})
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "-home-vspapg-myapp.md").write_text(
                "# Project brief: myapp\n- Redis for drafts [abc123]\n",
                encoding="utf-8")
            with unittest.mock.patch.object(ch.brief, "BRIEFS_DIR",
                                            Path(td)), \
                    unittest.mock.patch.object(
                        ch.integrations, "cwd_project_filter",
                        lambda cwd=None, *a, **k: "-home-vspapg-myapp"), \
                    unittest.mock.patch.object(sys, "stdin",
                                               io.StringIO(payload)), \
                    contextlib.redirect_stdout(buf):
                ch.run_brief_hook_mode()
        out = buf.getvalue()
        self.assertIn("Redis for drafts", out)
        self.assertIn("chf --brief", out)          # refresh hint in banner

    def test_hook_swallows_all_errors(self):
        import contextlib
        import io
        buf = io.StringIO()
        with unittest.mock.patch.object(sys, "stdin",
                                        io.StringIO("{not json")), \
                contextlib.redirect_stdout(buf):
            ch.run_brief_hook_mode()               # must not raise
        self.assertEqual(buf.getvalue(), "")


class ConfigTests(unittest.TestCase):
    """~/.config/claude-handoff/config.json supplies defaults; flags win."""

    def _with_cfg(self, content):
        import tempfile
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "config.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        env = unittest.mock.patch.dict(
            ch.os.environ, {"CLAUDE_HANDOFF_CONFIG": str(path)})
        return td, env

    def test_config_supplies_defaults(self):
        td, env = self._with_cfg(
            '{"include_tools": true, "fit": "8k", "output": "-"}')
        with td, env:
            cfg = ch.cli._load_config()
        self.assertEqual(cfg, {"include_tools": True, "fit": 8000,
                               "output": "-"})

    def test_cli_flag_beats_config(self):
        td, env = self._with_cfg('{"output": "-"}')
        with td, env:
            p = ch.build_arg_parser()
            p.set_defaults(**ch.cli._load_config())
            self.assertEqual(p.parse_args(["-o", "x.md"]).output, "x.md")
            self.assertEqual(p.parse_args([]).output, "-")

    def test_disallowed_and_unknown_keys_dropped_with_warning(self):
        import contextlib
        import io
        td, env = self._with_cfg(
            '{"no_redact": true, "bogus": 1, "llm": "ollama"}')
        buf = io.StringIO()
        with td, env, contextlib.redirect_stderr(buf):
            cfg = ch.cli._load_config()
        self.assertEqual(cfg, {"llm": "ollama"})
        self.assertIn("no_redact", buf.getvalue())
        self.assertIn("bogus", buf.getvalue())

    def test_malformed_config_never_fatal(self):
        import contextlib
        import io
        td, env = self._with_cfg("{not json")
        with td, env, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ch.cli._load_config(), {})

    def test_missing_config_is_empty(self):
        td, env = self._with_cfg(None)
        with td, env:
            self.assertEqual(ch.cli._load_config(), {})

    def test_main_applies_config(self):
        import contextlib
        import io
        import tempfile
        td, env = self._with_cfg('{"include_tools": true}')
        with td, env, tempfile.TemporaryDirectory() as out_td, \
                contextlib.redirect_stderr(io.StringIO()):
            out = Path(out_td) / "h.md"
            ch.main([str(FIXTURE), "-o", str(out)])
            doc = out.read_text(encoding="utf-8")
        self.assertIn("<details>", doc)        # include_tools via config


class AnonymizeTests(unittest.TestCase):
    """--anonymize: strip identity (paths, emails, IPs) for public sharing."""

    def test_anonymize_text_patterns(self):
        text = ("see /Users/devuser/Code/app/main.py and mail me at "
                "v.dev@example.com from 192.168.1.77; devuser wrote it")
        out, n = ch.anonymize_text(text, home=Path("/Users/devuser"))
        self.assertIn("~/Code/app/main.py", out)
        self.assertNotIn("/Users/devuser", out)
        self.assertIn("[EMAIL]", out)
        self.assertNotIn("v.dev@example.com", out)
        self.assertIn("[IP]", out)
        self.assertNotIn("192.168.1.77", out)
        self.assertNotIn("devuser", out)              # bare username too
        self.assertGreaterEqual(n, 4)

    def test_short_usernames_left_alone(self):
        out, _ = ch.anonymize_text("vi is an editor", home=Path("/home/vi"))
        self.assertIn("vi is an editor", out)          # too short to scrub

    def test_version_strings_not_ips(self):
        out, _ = ch.anonymize_text("claude-handoff 0.11.0 on 2.1.241",
                                   home=Path("/home/nobody"))
        self.assertNotIn("[IP]", out)

    def test_cli_flag_anonymizes_document(self):
        import contextlib
        import io
        parsed = ch.parse_session(SECRET)
        args = ch.build_arg_parser().parse_args([str(SECRET), "--anonymize"])
        with contextlib.redirect_stderr(io.StringIO()), \
                unittest.mock.patch.object(ch.redact, "_home",
                                           lambda: Path("/home/vspapg")):
            doc = ch.build_document(parsed, SECRET, args)
        self.assertIn("~/deploy", doc)
        self.assertNotIn("/home/vspapg", doc)
        self.assertNotIn("sk-ant-abc123", doc)         # redaction still on

    def test_off_by_default(self):
        import contextlib
        import io
        parsed = ch.parse_session(SECRET)
        args = ch.build_arg_parser().parse_args([str(SECRET)])
        with contextlib.redirect_stderr(io.StringIO()):
            doc = ch.build_document(parsed, SECRET, args)
        self.assertIn("/home/vspapg/deploy", doc)      # real paths kept

    def test_mcp_handoff_accepts_anonymize(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), \
                unittest.mock.patch.object(ch.redact, "_home",
                                           lambda: Path("/home/vspapg")):
            out = ch._mcp_call("handoff", {"path": str(SECRET),
                                           "anonymize": True})
        self.assertIn("~/deploy", out)
        self.assertNotIn("/home/vspapg", out)


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
                unittest.mock.patch.object(ch.llm, "CACHE_DIR", Path(td)), \
                unittest.mock.patch.object(ch.llm, "LLM_INPUT_CAP", 1000), \
                unittest.mock.patch.object(ch.llm, "CHUNK_CAP", 800), \
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
    def test_tilde_collapses_home(self):
        home = Path.home()
        self.assertEqual(ch.textutil.tilde(home / "x" / "y.jsonl"),
                         str(Path("~") / "x" / "y.jsonl"))  # native seps
        self.assertEqual(ch.textutil.tilde(Path("/opt/z")),
                         str(Path("/opt/z")))


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
