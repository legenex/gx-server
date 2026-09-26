"""Offline guards for the Open WebUI <-> Computer tooling (python3 -m unittest, no services needed).

Run: cd legenex/computer/tools && python3 -m unittest -v test_tools
"""
import subprocess
import unittest
from pathlib import Path

import owui_api

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


class Redaction(unittest.TestCase):
    def test_keys_and_tokens_never_printed(self):
        out = owui_api.redact({"OPENAI_API_KEYS": ["sk-abc", "x"], "nested": [{"key": "sk-zzz"}, "eyJhbGciOi.x.y"],
                               "OPENAI_API_BASE_URLS": ["http://127.0.0.1:4000/v1"]})
        self.assertEqual(out["OPENAI_API_KEYS"], "<redacted>")
        self.assertEqual(out["nested"][0]["key"], "<redacted>")
        self.assertTrue(out["nested"][1].startswith("<redacted"))
        self.assertEqual(out["OPENAI_API_BASE_URLS"], ["http://127.0.0.1:4000/v1"])


class PublicRepoGuards(unittest.TestCase):
    """The repo is public and autosynced: Computer's workspace state must never be committable."""

    def ignored(self, path):
        return subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", path]).returncode == 0

    def test_computer_state_is_ignored(self):
        for p in (".cptr/chats/c.json", ".cptr/task_logs/t.jsonl", ".cptr/attachments/c/m/f.bin",
                  ".cptr/memory/users/u/m.md", ".cptr/artifacts/a.md", ".cptr/screenshots/s.png",
                  ".cptr/usage.json", ".cptr/cache/audio/x.wav", "legenex/sub/.cptr/chats/c.json",
                  "generated-image-20260101-000000.png", "edited-image-20260101-000000.png"):
            self.assertTrue(self.ignored(p), p)

    def test_instruction_files_stay_tracked(self):
        for p in (".cptr/system.md", ".cptr/model"):
            self.assertFalse(self.ignored(p), p)


class Templates(unittest.TestCase):
    def test_workspace_prompt_loads_project_instructions(self):
        text = (REPO / ".cptr" / "system.md").read_text()
        for var in ("{{INSTRUCTIONS}}", "{{CPTR_CONTEXT}}", "{{MEMORY}}", "{{SKILLS}}", "{{FILE_TREE}}"):
            self.assertIn(var, text)

    def test_compaction_summary_prompt_is_bounded_and_injection_resistant(self):
        text = (HERE / "compaction_prompt.md").read_text()
        self.assertIn("{{COMPACTED_MESSAGES}}", text)
        self.assertIn("{{PREVIOUS_SUMMARY}}", text)
        # the kept messages are what triggered the call; feeding them back re-inflates the
        # summary request and lets their instructions hijack the summary
        self.assertNotIn("{{RECENT_MESSAGES", text)
        self.assertNotIn("{{MESSAGES", text)
        self.assertIn("DATA", text)

    def test_default_model_is_a_public_alias(self):
        self.assertEqual((REPO / ".cptr" / "model").read_text().strip(), "gx-auto")


if __name__ == "__main__":
    unittest.main()
