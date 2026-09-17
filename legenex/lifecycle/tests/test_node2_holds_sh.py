"""Tests for legenex/lifecycle/node2-holds.sh (D-036).

The gx-max drain must hold node 2, wait for gx-music's supervisor to unload
its engine, fall back to stopping only the engine container, and refuse to
report success while a container, a ledger entry or an ACE-Step process is
left. These tests run the real script with bash against a temporary guard
directory and fake `docker` / `pgrep` binaries; "node 2" is a local shell.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

HOLDS = Path(__file__).resolve().parents[1] / "node2-holds.sh"


class HoldsHarness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.guard = tmp / "guard"
        self.guard.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.state = tmp / "docker-state"
        self.state.mkdir()
        # fake docker: a container "exists" while <state>/<name> exists. A
        # counter file lets a test make the container vanish after N inspects,
        # which is what the supervisor's own unload looks like from outside.
        (self.bin / "docker").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {tmp}/docker.calls
            case "$1" in
              inspect)
                name="${{@: -1}}"
                f={self.state}/"$name"
                [ -e "$f" ] || exit 1
                if [ -f "$f.vanish" ]; then
                  n=$(cat "$f.vanish"); n=$((n-1)); echo $n > "$f.vanish"
                  if [ "$n" -le 0 ]; then rm -f "$f" "$f.vanish"; exit 1; fi
                fi
                echo true; exit 0 ;;
              stop) exit 0 ;;
              rm) name="${{@: -1}}"; [ -f {self.state}/"$name.sticky" ] || rm -f {self.state}/"$name"; exit 0 ;;
            esac
            exit 0
            """))
        (self.bin / "pgrep").write_text(f"#!/bin/bash\ncat {tmp}/procs 2>/dev/null || echo 0\n")
        for f in ("docker", "pgrep"):
            (self.bin / f).chmod(0o755)

    def container(self, name: str, vanish_after: int | None = None, sticky: bool = False) -> None:
        (self.state / name).write_text("")
        if vanish_after is not None:
            (self.state / f"{name}.vanish").write_text(str(vanish_after))
        if sticky:
            (self.state / f"{name}.sticky").write_text("")

    def ledger(self, data: dict) -> None:
        (self.guard / "node2-residency.json").write_text(json.dumps(data))

    def run(self, body: str, **env: str) -> subprocess.CompletedProcess:
        e = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "GX_N2_GUARD_DIR": str(self.guard),
            "GX_N2_REPO": str(self.tmp / "no-repo"),
            "GXH_N2_CMD": "bash -c",
            "GX_MUSIC_DRAIN_TIMEOUT": "4",
            **env,
        }
        script = f"set -u\nsource {HOLDS}\n{body}\n"
        return subprocess.run(["bash", "-c", script], env=e, capture_output=True, text=True, timeout=60)


class Node2HoldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = HoldsHarness(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hold_set_and_clear(self):
        r = self.h.run("gx_n2_hold_set gxmax")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.h.guard / "node2.gxmax-hold").is_file())
        r = self.h.run("gx_n2_hold_clear gxmax")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.h.guard / "node2.gxmax-hold").exists())

    def test_clear_is_idempotent(self):
        self.assertEqual(self.h.run("gx_n2_hold_clear gxmax").returncode, 0)

    def test_maintenance_hold_is_a_separate_file(self):
        self.h.run("gx_n2_hold_set maintenance")
        self.assertTrue((self.h.guard / "node2.maintenance-hold").is_file())
        self.assertFalse((self.h.guard / "node2.gxmax-hold").exists())

    def test_nothing_loaded_is_verified_immediately(self):
        self.h.ledger({})
        r = self.h.run("gx_music_drain_node2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("how=none", r.stdout)
        self.assertIn("container=absent", r.stdout)
        self.assertIn("ledger=clean", r.stdout)

    def test_supervisor_unload_is_waited_for_not_forced(self):
        # The engine disappears on its own after a few inspects (reaper tick).
        self.h.container("gx-music", vanish_after=3)
        self.h.ledger({})
        r = self.h.run("gx_music_drain_node2", GX_MUSIC_DRAIN_TIMEOUT="60")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("how=supervisor", r.stdout)
        calls = (self.h.tmp / "docker.calls").read_text()
        self.assertNotIn("stop", calls)

    def test_fallback_stops_only_the_engine(self):
        self.h.container("gx-music")
        self.h.ledger({})
        r = self.h.run("gx_music_drain_node2", GX_MUSIC_DRAIN_TIMEOUT="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("how=fallback-stop", r.stdout)
        calls = (self.h.tmp / "docker.calls").read_text()
        self.assertIn("stop -t 30 gx-music", calls)
        self.assertNotIn("gx-llama-swap", calls)
        self.assertNotIn("systemctl", calls)

    def test_container_that_will_not_go_fails_verification(self):
        self.h.container("gx-music", sticky=True)
        self.h.ledger({})
        r = self.h.run("gx_music_drain_node2", GX_MUSIC_DRAIN_TIMEOUT="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("container=present", r.stdout)

    def test_stale_ledger_entry_fails_verification(self):
        # No orchestrator checkout in the fake repo, so the release cannot run.
        self.h.ledger({"gx-music": {"node": "node2", "class": "medium", "estimated_gib": 32}})
        r = self.h.run("gx_music_drain_node2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ledger=dirty", r.stdout)

    def test_stale_ledger_entry_is_released_through_the_guard(self):
        repo = self.h.tmp / "repo"
        orch = repo / "legenex" / "orchestrator"
        orch.parent.mkdir(parents=True)
        os.symlink(Path(__file__).resolve().parents[2] / "orchestrator", orch)
        self.h.ledger({"gx-music": {"node": "node2", "class": "medium", "estimated_gib": 32,
                                    "container": "gx-music"}})
        r = self.h.run("gx_music_drain_node2", GX_N2_REPO=str(repo))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ledger=clean", r.stdout)
        self.assertNotIn("gx-music", (self.h.guard / "node2-residency.json").read_text())

    def test_leftover_engine_process_fails_verification(self):
        (self.h.tmp / "procs").write_text("1\n")
        self.h.ledger({})
        r = self.h.run("gx_music_drain_node2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("engine_procs=1", r.stdout)

    def test_unreachable_node2_is_reported(self):
        r = self.h.run("gx_music_drain_node2", GXH_N2_CMD="false")
        self.assertEqual(r.returncode, 5)
        r = self.h.run("gx_n2_hold_set gxmax", GXH_N2_CMD="false")
        self.assertEqual(r.returncode, 5)


class LifecycleWiringTests(unittest.TestCase):
    """The scripts must call the helpers in the right place (static check)."""

    here = Path(__file__).resolve().parents[1]

    def test_start_holds_and_drains_music_before_other_node2_conflicts(self):
        text = (self.here / "gx-max-start.sh").read_text()
        body = text[text.index("drain_node2() {"):]
        body = body[:body.index("\n}\n")]
        self.assertLess(body.index("gx_n2_hold_set gxmax"), body.index("gx_music_drain_node2"))
        self.assertLess(body.index("gx_music_drain_node2"), body.index('for c in "${CONFLICTS_N2[@]}"'))
        self.assertIn("CONFLICTS_N2=(gx-music ", text)
        # The drain happens before the rank1 launch.
        self.assertLess(text.index("drain_node2\n"), text.index("=== starting rank1 on node2 ==="))

    def test_every_release_path_clears_the_hold(self):
        for name in ("gx-max-stop.sh", "gx-max-unwind.sh", "restore-normal.sh"):
            self.assertIn("gx_n2_hold_clear gxmax", (self.here / name).read_text(), name)

    def test_restore_starts_the_music_supervisor_only(self):
        text = (self.here / "restore-normal.sh").read_text()
        self.assertIn("gx_music_supervisor_ensure", text)
        helper = (self.here / "node2-holds.sh").read_text()
        self.assertNotIn("/v1/music/load", helper)


if __name__ == "__main__":
    unittest.main()
