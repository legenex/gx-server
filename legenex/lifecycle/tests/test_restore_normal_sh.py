"""Tests for legenex/lifecycle/restore-normal.sh.

Regression (2026-09-17): the orchestrator started before gateway/.env got the
real media key, kept the old placeholder in its environment, and a gx-max
release run from it recreated gx-litellm with that placeholder, because a
caller's variable beats `docker compose --env-file`. The script must let the
ignored .env win. Runs the real script against a temporary tree with fake
`docker`, `curl` and `ssh` binaries (node 2 unreachable, so it is skipped).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


class RestoreNormalEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.tmp = tmp
        lifecycle = tmp / "repo" / "legenex" / "lifecycle"
        lifecycle.mkdir(parents=True)
        for name in ("restore-normal.sh", "node2-holds.sh"):
            shutil.copy(HERE / name, lifecycle / name)
        gateway = tmp / "repo" / "legenex" / "gateway"
        gateway.mkdir()
        (gateway / "docker-compose.gateway.yml").write_text("services: {}\n")
        (gateway / ".env").write_text("# test\nGX_MEDIA_API_KEY=from-dot-env-0123456789\nexport LITELLM_MASTER_KEY=sk-dot-env\n")
        self.script = lifecycle / "restore-normal.sh"
        bindir = tmp / "bin"
        bindir.mkdir()
        # fake docker records what compose would see for the gateway variables
        (bindir / "docker").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            if [ "$1" = compose ]; then
              echo "GX_MEDIA_API_KEY=${{GX_MEDIA_API_KEY-<unset>}} LITELLM_MASTER_KEY=${{LITELLM_MASTER_KEY-<unset>}} $*" >> {tmp}/compose.calls
            fi
            exit 0
            """))
        (bindir / "curl").write_text("#!/bin/bash\nexit 0\n")
        (bindir / "ssh").write_text("#!/bin/bash\nexit 255\n")
        for f in bindir.iterdir():
            f.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
                    "GXH_N2_CMD": "false", "GX_GUARD_ROOT": str(tmp / "guard")}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_script(self, **extra: str) -> str:
        subprocess.run(["bash", str(self.script)], env={**self.env, **extra}, capture_output=True,
                       text=True, timeout=120, check=False)
        return (self.tmp / "compose.calls").read_text()

    def test_stale_caller_values_do_not_reach_compose(self) -> None:
        calls = self.run_script(GX_MEDIA_API_KEY="not-required", LITELLM_MASTER_KEY="stale")
        self.assertIn("GX_MEDIA_API_KEY=<unset>", calls)
        self.assertIn("LITELLM_MASTER_KEY=<unset>", calls)
        self.assertIn("--env-file .env", calls)
        self.assertNotIn("not-required", calls)

    def test_unrelated_variables_are_kept(self) -> None:
        # Only names defined in .env are dropped; the script still runs compose once.
        calls = self.run_script()
        self.assertEqual(len(calls.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
