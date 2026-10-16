"""identity-hmac.sh: provision / verify / rollback against a fake docker (D-054).

    python3 -m unittest discover -s legenex/gateway/tests
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "identity-hmac.sh"

# A fake `docker`: `compose ... --env-file F up ... litellm` copies the key
# from F into the "running container" state; `exec` prints it back.
FAKE_DOCKER = r'''#!/usr/bin/env bash
state="$FAKE_STATE"
echo "$*" >> "$state.calls"
if [ "$1" = compose ]; then
  envf=""; prev=""
  for a in "$@"; do [ "$prev" = "--env-file" ] && envf="$a"; prev="$a"; done
  sed -n 's/^GX_IDENTITY_HMAC_KEY=//p' "$envf" | tail -1 > "$state"
  exit 0
fi
if [ "$1" = exec ]; then cat "$state" 2>/dev/null | tr -d '\n'; exit 0; fi
exit 1
'''


class TestIdentityHmacScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.key = d / "secrets" / "orchestrator" / "identity-hmac"
        self.envf = d / "gateway.env"
        self.envf.write_text("LITELLM_MASTER_KEY=sk-test\nGX_ORCHESTRATOR_API_KEY=orch\n")
        self.envf.chmod(0o600)
        fake = d / "docker"
        fake.write_text(FAKE_DOCKER)
        fake.chmod(0o755)
        self.state = d / "container.env"
        self.env = dict(os.environ, GX_HMAC_FILE=str(self.key), GX_GATEWAY_ENV=str(self.envf),
                        GX_DOCKER=str(fake), FAKE_STATE=str(self.state), GX_SKIP_REQUEST="1",
                        GX_GATEWAY_URL="http://127.0.0.1:9/v1")

    def run_script(self, *args, **extra):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                              env={**self.env, **extra}, timeout=60)

    def test_provision_verify_rollback_cycle(self):
        # Liveliness polling would hit :9; skip the recreate wait but still call compose.
        p = self.run_script("provision", GX_SKIP_RECREATE="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)
        key = self.key.read_text().strip()
        self.assertEqual(len(key), 64)
        self.assertNotIn(key, p.stdout + p.stderr, "the key must never be printed")
        self.assertIn(f"GX_IDENTITY_HMAC_KEY={key}", self.envf.read_text())
        self.assertEqual(stat.S_IMODE(self.envf.stat().st_mode), 0o600)
        # The container has not been recreated yet -> verify must fail.
        v = self.run_script("verify")
        self.assertNotEqual(v.returncode, 0)
        self.assertIn("different (or no) key", v.stderr)
        # Simulate the recreate (what provision does without GX_SKIP_RECREATE).
        self.state.write_text(key)
        v = self.run_script("verify")
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertIn("same key on both sides", v.stdout)
        # Provision again keeps the key (idempotent).
        self.run_script("provision", GX_SKIP_RECREATE="1")
        self.assertEqual(self.key.read_text().strip(), key)
        # Rollback: key file moved aside, .env line gone, verify fails closed.
        r = self.run_script("rollback", GX_SKIP_RECREATE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.key.exists())
        self.assertTrue(list(self.key.parent.glob("identity-hmac.disabled-*")))
        self.assertNotIn("GX_IDENTITY_HMAC_KEY=", self.envf.read_text())
        self.assertIn("LITELLM_MASTER_KEY=sk-test", self.envf.read_text())
        self.assertTrue(list(self.envf.parent.glob("gateway.env.bak-identity-hmac-*")))
        self.assertNotEqual(self.run_script("verify").returncode, 0)

    def test_mismatched_env_is_refused(self):
        self.run_script("provision", GX_SKIP_RECREATE="1")
        self.state.write_text(self.key.read_text().strip())
        self.envf.write_text(self.envf.read_text().replace("GX_IDENTITY_HMAC_KEY=", "GX_IDENTITY_HMAC_KEY=x"))
        v = self.run_script("verify")
        self.assertNotEqual(v.returncode, 0)
        self.assertIn(".env key differs", v.stderr)

    def test_recreate_targets_only_the_litellm_service(self):
        p = self.run_script("provision", GX_SKIP_RECREATE="0", GX_LIVE_WAIT="2")   # nothing live on :9
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rollback", p.stderr)
        calls = Path(str(self.state) + ".calls").read_text()
        self.assertIn("compose -p gx-gateway", calls)
        self.assertIn("up -d --no-deps --force-recreate litellm", calls)

    def test_symlinked_env_stays_a_symlink_and_secrets_stay_out_of_the_tree(self):
        # Live layout (D-043): legenex/gateway/.env -> /srv/projects/gx-cluster/secrets/gateway.env
        d = Path(self.tmp.name)
        tree, secrets = d / "tree", d / "secrets-store"
        tree.mkdir()
        secrets.mkdir()
        target = secrets / "gateway.env"
        target.write_text(self.envf.read_text())
        target.chmod(0o600)
        link = tree / ".env"
        link.symlink_to(target)
        env = {"GX_GATEWAY_ENV": str(link), "GX_SKIP_RECREATE": "1"}
        for _ in range(5):                      # reruns: backups must be capped
            self.assertEqual(self.run_script("provision", **env).returncode, 0)
        self.assertTrue(link.is_symlink(), "the .env symlink must never be replaced")
        self.assertEqual(Path(os.readlink(link)), target)
        key = self.key.read_text().strip()
        self.assertIn(f"GX_IDENTITY_HMAC_KEY={key}", target.read_text())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in tree.iterdir()), [".env"], "nothing secret lands in the tree")
        backups = list(secrets.glob("gateway.env.bak-identity-hmac-*"))
        self.assertTrue(1 <= len(backups) <= 3, backups)
        self.assertTrue(all(stat.S_IMODE(b.stat().st_mode) == 0o600 for b in backups))
        self.assertFalse(list(secrets.glob(".gateway.env.identity-hmac.*")), "no temp file left behind")
        r = self.run_script("rollback", **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(link.is_symlink())
        self.assertNotIn("GX_IDENTITY_HMAC_KEY=", target.read_text())
        self.assertEqual(sorted(p.name for p in tree.iterdir()), [".env"])


if __name__ == "__main__":
    unittest.main()
