"""Sandbox enforcement tests for the coding bench (hermetic, no network).

Every dangerous-command class must be REFUSED: rm -rf, curl, sudo, cd
escape, `..` traversal, semicolon chains, pipes, redirects, command
substitution, absolute paths, disallowed programs, disallowed git
subcommands, disallowed python modules. Allowed commands must pass and run
inside the pinned cwd.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_workflow as wf  # noqa: E402


class TestValidateCommandRefusals(unittest.TestCase):
    """Every one of these MUST be refused."""

    REFUSALS = [
        "rm -rf /",                       # destructive
        "rm -rf .",                       # destructive in-repo
        "rm notes.txt",                   # rm is never allowed
        "curl http://evil.example/x",     # network tool
        "wget http://evil.example/x",     # network tool
        "sudo python3 app.py",            # privilege escalation
        "sudo -l",                        # privilege probing
        "cd /",                           # cd escape
        "cd ../..",                       # cd escape via traversal
        "ls ..",                          # out-of-sandbox listing (ls also not allowlisted)
        "cat ../../etc/passwd",           # .. traversal + not allowlisted
        "python3 ../../etc/evil.py",      # .. traversal inside python3
        "pytest ../outside",              # .. traversal inside pytest
        "git status ../../x",             # .. traversal inside git
        "python3 /etc/evil.py",           # absolute path
        "pytest /tmp/x",                  # absolute path
        "git log --output=/tmp/x",        # absolute path arg
        "echo hi; rm -rf x",              # semicolon chain
        "echo hi && rm x",                # AND chain
        "echo hi || rm x",                # OR chain
        "cat x | sh",                     # pipe
        "python3 x.py > out",            # redirect
        "python3 x.py < in",             # redirect
        "echo `rm -rf x`",               # backtick substitution
        "echo $(rm -rf x)",              # $() substitution
        "echo $HOME",                    # variable expansion
        "echo a\\b",                     # backslash escape
        "bash -c 'rm x'",                # nested shell
        "sh scripts/x",                   # other interpreter
        "pip install requests",           # package installs
        "git push origin main",           # network git
        "git checkout --force x",         # destructive git
        "python3 -m http.server",        # server module
        "python3 -m os",                  # non-test module
        "",                               # empty
        "   ",                            # whitespace only
    ]

    def test_dangerous_commands_refused(self):
        for command in self.REFUSALS:
            with self.subTest(command=command):
                with self.assertRaises(wf.SandboxRefusal):
                    wf.validate_command(command)

    def test_multiline_command_refused(self):
        with self.assertRaises(wf.SandboxRefusal):
            wf.validate_command("pytest tests\necho done")


class TestValidateCommandAllowed(unittest.TestCase):
    ALLOWED = {
        "pytest": "pytest tests",
        "python3 script": "python3 app.py",
        "python3 -m unittest": "python3 -m unittest discover -s tests",
        "python3 -m pytest": "python3 -m pytest -q tests",
        "python3 -c": "python3 -c print(1)",
        "git status": "git status",
        "git diff": "git diff --stat",
        "git log": "git log --oneline -5",
        "git add": "git add tests",
        "git commit": "git commit -m fix-seeded-bug",
    }

    def test_allowed_commands_pass(self):
        for desc, command in self.ALLOWED.items():
            with self.subTest(command=command):
                argv = wf.validate_command(command)
                self.assertTrue(len(argv) >= 1)

    def test_python_resolved_to_interpreter(self):
        argv = wf.validate_command("python3 app.py")
        self.assertEqual(argv[0], sys.executable)


class TestSandboxRuns(unittest.TestCase):
    def setUp(self):
        import subprocess

        self.tmp = Path(tempfile.mkdtemp(prefix="gx-sbx-"))
        subprocess.run(["bash", str(wf.SEED_SCRIPT), str(self.tmp)],
                       capture_output=True, text=True, timeout=60, check=True)
        self.sandbox = wf.Sandbox(self.tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_allowed_command_runs_pinned_to_sandbox(self):
        out = self.sandbox.run_command("python3 tracker.py")
        self.assertTrue(out["ok"], out["output"])
        self.assertIn("remaining", out["output"])

    def test_refused_command_reports_refusal(self):
        out = self.sandbox.run_command("rm -rf /")
        self.assertFalse(out["ok"])
        self.assertTrue(out["refused"])
        self.assertIn("REFUSED", out["output"])

    def test_git_add_commit_allowed_in_sandbox(self):
        (self.tmp / "notes.txt").write_text("hello\n")
        out = self.sandbox.run_command("git add notes.txt")
        self.assertTrue(out["ok"], out["output"])
        out = self.sandbox.run_command("git commit -m test")
        self.assertTrue(out["ok"], out["output"])

    def test_write_file_inside_sandbox(self):
        out = self.sandbox.write_file("tests/x.py", "VALUE = 1\n")
        self.assertTrue(out["ok"])
        self.assertEqual((self.tmp / "tests" / "x.py").read_text(), "VALUE = 1\n")

    def test_write_file_traversal_refused(self):
        for path in ("../outside.txt", "tests/../../outside.txt", "/tmp/abs.py", "tests/../../abs.py"):
            with self.subTest(path=path):
                with self.assertRaises(wf.SandboxRefusal):
                    self.sandbox.write_file(path, "x")

    def test_write_symlink_escape_refused(self):
        (self.tmp / "tests").mkdir(exist_ok=True)
        (self.tmp / "tests" / "link").symlink_to(Path(tempfile.gettempdir()), target_is_directory=True)
        with self.assertRaises(wf.SandboxRefusal):
            self.sandbox.write_file("tests/link/pwn.py", "x")


class TestExtractToolBlocks(unittest.TestCase):
    def test_bash_and_write_blocks_parsed(self):
        reply = (
            "Here is the fix.\n"
            "```write tracker.py\nNEW = 1\n```\n"
            "```bash\npython3 -m unittest discover -s tests\n```\n"
            "Done."
        )
        commands, writes, prose = wf.extract_tool_blocks(reply)
        self.assertEqual(commands, ["python3 -m unittest discover -s tests"])
        self.assertEqual(writes, [("tracker.py", "NEW = 1\n")])
        self.assertNotIn("```", prose)

    def test_no_blocks(self):
        commands, writes, prose = wf.extract_tool_blocks("just text")
        self.assertEqual((commands, writes, prose), ([], [], "just text"))

    def test_multiline_bash_block_splits_into_commands(self):
        reply = "```bash\npytest tests\ngit status\n```"
        commands, _, _ = wf.extract_tool_blocks(reply)
        self.assertEqual(commands, ["pytest tests", "git status"])


class TestVerdictParsing(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(wf.parse_verdict("work ok\nVERDICT: PASS"), "PASS")
        self.assertEqual(wf.parse_verdict("VERDICT: FAIL (bug)"), "FAIL")
        self.assertEqual(wf.parse_verdict("FINAL: PASS tests green"), "PASS")
        self.assertEqual(wf.parse_verdict("final: fail"), "FAIL")
        self.assertIsNone(wf.parse_verdict("no verdict here"))

    def test_test_summary(self):
        summary = wf.parse_test_summary(["Ran 12 tests in 0.1s", "OK", "12 passed, 1 failed"])
        self.assertEqual(summary["passed"], 12)
        self.assertEqual(summary["failed"], 1)
        self.assertFalse(summary["green"])
        green = wf.parse_test_summary(["3 passed"])
        self.assertTrue(green["green"])


class TestTemplateRendering(unittest.TestCase):
    def test_templates_render_with_protocol(self):
        rendered = wf.render_template("orchestrator", repo_path="/tmp/x")
        self.assertIn("/tmp/x", rendered)
        self.assertIn("PROTOCOL", rendered)
        self.assertNotIn("<<", rendered)

    def test_all_role_templates_render(self):
        vars_by_role = {
            "inspector": {"repo_path": "/r"},
            "architect": {"repo_path": "/r", "inspector_findings": "f"},
            "implementer": {"repo_path": "/r", "architect_plan": "p"},
            "tester": {"repo_path": "/r", "implemented_files": "f"},
            "reviewer": {"repo_path": "/r"},
            "repair": {"repo_path": "/r", "review_defects": "d"},
            "validator": {"repo_path": "/r"},
        }
        for role, vars in vars_by_role.items():
            with self.subTest(role=role):
                rendered = wf.render_template(role, **vars)
                self.assertIn("PROTOCOL", rendered)
                self.assertNotIn("<<", rendered)


if __name__ == "__main__":
    unittest.main()
