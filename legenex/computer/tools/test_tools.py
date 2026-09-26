"""Offline guards for the Open WebUI <-> Computer tooling (python3 -m unittest, no services needed).

Run: cd legenex/computer/tools && python3 -m unittest -v test_tools
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

import owui_api

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


class Redaction(unittest.TestCase):
    def test_names_and_token_shapes(self):
        out = owui_api.redact({"CLIENT_SECRET": "abc", "OPENAI_API_KEY": "x", "LDAP_APP_PASSWORD": "p",
                               "headers": {"Authorization": "Bearer abc", "Cookie": "t=1", "X-OpenWebUI-Chat-Id": "{{CHAT_ID}}"},
                               "auth_type": "bearer", "ENABLE_API_KEYS": True,
                               "note": "use hf_" + "a" * 30 + " or ghp_" + "b" * 36 + " or xai-" + "c" * 30})
        for k in ("CLIENT_SECRET", "OPENAI_API_KEY", "LDAP_APP_PASSWORD"):
            self.assertEqual(out[k], "<redacted>")
        self.assertEqual(out["headers"]["Authorization"], "<redacted>")
        self.assertEqual(out["headers"]["Cookie"], "<redacted>")
        self.assertEqual(out["headers"]["X-OpenWebUI-Chat-Id"], "{{CHAT_ID}}")
        self.assertEqual((out["auth_type"], out["ENABLE_API_KEYS"]), ("bearer", True))
        self.assertNotIn("hf_", out["note"]); self.assertNotIn("ghp_", out["note"]); self.assertNotIn("xai-", out["note"])

    def test_keys_and_tokens_never_printed(self):
        out = owui_api.redact({"OPENAI_API_KEYS": ["sk-abc", "x"], "nested": [{"key": "sk-zzz"}, "eyJhbGciOiJIUzI1NiJ9.eyJpZCI6IjEifQ.c2lnbmF0dXJl"],
                               "OPENAI_API_BASE_URLS": ["http://127.0.0.1:4000/v1"]})
        self.assertEqual(out["OPENAI_API_KEYS"], "<redacted>")
        self.assertEqual(out["nested"][0]["key"], "<redacted>")
        self.assertTrue(out["nested"][1].startswith("<redacted"))
        self.assertEqual(out["OPENAI_API_BASE_URLS"], ["http://127.0.0.1:4000/v1"])


class OpenAIUpsert(unittest.TestCase):
    """Read-modify-write of Open WebUI's parallel url/key lists must keep indices aligned."""

    def run_upsert(self, cfg, spec):
        sent = {}
        orig_call, orig_cfg = owui_api.call, owui_api.openai_config
        owui_api.openai_config = lambda tok: cfg
        owui_api.call = lambda tok, m, p, b=None, timeout=600: (sent.update(body=b) or (200, b))
        try:
            owui_api.openai_upsert("t", spec)
        finally:
            owui_api.call, owui_api.openai_config = orig_call, orig_cfg
        return sent["body"]

    def test_add_with_stale_longer_key_list(self):
        cfg = {"ENABLE_OPENAI_API": True, "OPENAI_API_BASE_URLS": ["a", "b"], "OPENAI_API_KEYS": ["ka", "kb", "stale"],
               "OPENAI_API_CONFIGS": {"0": {"enable": False}, "1": {"enable": True}}}
        body = self.run_upsert(cfg, {"url": "c", "key": "kc", "config": {"enable": True, "tags": ["computer"]}})
        self.assertEqual(body["OPENAI_API_BASE_URLS"], ["a", "b", "c"])
        self.assertEqual(body["OPENAI_API_KEYS"], ["ka", "kb", "kc"])
        self.assertEqual(body["OPENAI_API_CONFIGS"]["2"]["tags"], ["computer"])
        self.assertEqual(body["OPENAI_API_CONFIGS"]["0"], {"enable": False})

    def test_update_keeps_other_connections_and_settings(self):
        cfg = {"ENABLE_OPENAI_API": True, "OPENAI_API_BASE_URLS": ["a", "b"], "OPENAI_API_KEYS": ["ka"],
               "OPENAI_API_CONFIGS": {"0": {"enable": False}, "1": {"enable": True, "tags": ["gx"]}}}
        body = self.run_upsert(cfg, {"url": "b", "key": "kb2", "config": None})
        self.assertEqual(body["OPENAI_API_KEYS"], ["ka", "kb2"])
        self.assertEqual(body["OPENAI_API_CONFIGS"]["1"], {"enable": True, "tags": ["gx"]})


class NoteSnapshot(unittest.TestCase):
    def test_note_is_the_gx_block_only(self):
        import provision
        md = provision.note_markdown()
        self.assertIn("LOCKED", md)
        self.assertNotIn("lead software architect", md)   # the generic community rules after '---'


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

    def test_autosync_refuses_computer_state_and_scanner_overrides(self):
        script = ('source "$0"; for p in "$@"; do gxs_forbidden_path "$p" && echo F || echo A; done')
        paths = [".cptr/chats/c.json", ".cptr/.gitignore", "x/.cptr/task_logs/t", "generated-image-1.png",
                 ".gitleaks.toml", "a/.gitleaksignore", ".cptr/system.md", ".cptr/model", "legenex/control-ui/x.py"]
        env = {"GX_SYNC_LOG_DIR": "/tmp/gx-test-sync-logs", "GX_SYNC_STATE_DIR": "/tmp/gx-test-sync-state", "PATH": "/usr/bin:/bin"}
        out = subprocess.run(["bash", "-c", script, str(REPO / "ops" / "git-sync" / "common.sh"), *paths],
                             capture_output=True, text=True, env=env).stdout.split()
        self.assertEqual(out, ["F"] * 6 + ["A"] * 3)

    def test_instruction_files_stay_tracked(self):
        for p in (".cptr/system.md", ".cptr/model"):
            self.assertFalse(self.ignored(p), p)


GITLEAKS = Path.home() / ".local" / "bin" / "gitleaks"


@unittest.skipUnless(GITLEAKS.exists(), "gitleaks not installed on this node")
class SecretGate(unittest.TestCase):
    """ops/git-sync gxs_scan_staged must not be switchable off from the working tree."""

    TOKEN = "ghp_" + "Zx8" * 12   # fake, secret-shaped

    def gate(self, setup):
        with tempfile.TemporaryDirectory() as t:
            git = lambda *a: subprocess.run(["git", "-C", t, *a], check=True, capture_output=True)
            git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
            setup(Path(t), git)
            env = {"GX_SYNC_REPO": t, "GX_SYNC_LOG_DIR": f"{t}/.l", "GX_SYNC_STATE_DIR": f"{t}/.s",
                   "PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
            return subprocess.run(["bash", "-c", 'source "$0"; cd "$GX_SYNC_REPO"; gxs_scan_staged >/dev/null; echo $?',
                                   str(REPO / "ops" / "git-sync" / "common.sh")],
                                  capture_output=True, text=True, env=env).stdout.strip()

    def leak(self, t, git, suffix=""):
        (t / "a.txt").write_text(f'x = "{self.TOKEN}"{suffix}\n'); git("add", "a.txt")

    def test_blocks_plain_leak(self):
        self.assertEqual(self.gate(lambda t, g: self.leak(t, g)), "1")

    def test_planted_overrides_do_not_disable_it(self):
        def ignore_file(t, g):
            self.leak(t, g); (t / ".gitleaksignore").write_text("*\n")
        def config_file(t, g):
            (t / ".gitleaks.toml").write_text('[allowlist]\npaths = [".*"]\n'); self.leak(t, g)
        self.assertEqual(self.gate(ignore_file), "1")
        self.assertEqual(self.gate(config_file), "1")
        self.assertEqual(self.gate(lambda t, g: self.leak(t, g, "  # gitleaks:allow")), "1")

    def test_clean_change_passes(self):
        def clean(t, g):
            (t / "a.txt").write_text("hello\n"); g("add", "a.txt")
        self.assertEqual(self.gate(clean), "0")


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
