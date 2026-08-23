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

    def test_global_truncation_keeps_head_and_tail(self):
        doc = ch.render_transcript(self.parsed, include_tools=False,
                                   max_chars=150)
        self.assertIn("omitted", doc)
        self.assertIn("το login σπάει"[:10], doc)   # opening survives


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
