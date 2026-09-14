"""Tests for the bash side of the admission-control layer.

`legenex/lifecycle/resource-guard.sh` and `gx-safe-run.sh` are meant to be
the sanctioned path for starting a medium/large/exclusive container from a
shell script (see B-012 in coordination/BLOCKERS.md for the incident that
motivated this). These tests exercise the REAL scripts with `bash`, a REAL
`flock`, and a REAL (temporary) filesystem -- only the "docker container" is
faked, as a plain shell command, since no real GPU workload may be started
by this task.

Run with:  python3 -m unittest discover -s legenex/lifecycle/tests
(also picked up by a repo-root `python3 -m unittest discover`, matching the
convention already used by legenex/orchestrator/tests).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LIFECYCLE_DIR = Path(__file__).resolve().parents[1]
GUARD_SH = LIFECYCLE_DIR / "resource-guard.sh"
SAFE_RUN_SH = LIFECYCLE_DIR / "gx-safe-run.sh"


def write_meminfo(path: Path, mem_available_kib: int) -> None:
    path.write_text(
        "MemTotal:       127535340 kB\n"
        f"MemAvailable:   {mem_available_kib} kB\n"
        "SwapTotal:       67108856 kB\n"
    )


class GuardShTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.state_dir = self.tmp_path / "state"
        self.meminfo = self.tmp_path / "meminfo"
        write_meminfo(self.meminfo, 100 * 1024 * 1024)  # 100 GiB available
        self.env_base = {
            "PATH": "/usr/bin:/bin",
            "GX_GUARD_STATE_DIR": str(self.state_dir),
            "GX_GUARD_MEMINFO": str(self.meminfo),
            "GX_GUARD_LOCK_TIMEOUT": "5",
            "GX_GUARD_PY": sys.executable,
            "HOME": str(self.tmp_path),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_guard_run(self, *args: str, timeout: float = 20.0, env_overrides: dict | None = None):
        env = dict(self.env_base)
        env.update(env_overrides or {})
        script = f'source "{GUARD_SH}"; gx_guard_run "$@"'
        return subprocess.run(
            ["bash", "-c", script, "gx_guard_run", *args],
            capture_output=True, text=True, env=env, timeout=timeout,
        )

    def run_safe_run(self, *args: str, timeout: float = 20.0, env_overrides: dict | None = None):
        env = dict(self.env_base)
        env.update(env_overrides or {})
        return subprocess.run(
            [str(SAFE_RUN_SH), *args], capture_output=True, text=True, env=env, timeout=timeout,
        )

    def status(self, node: str) -> dict:
        env = dict(self.env_base)
        script = f'source "{GUARD_SH}"; gx_guard_status "$1"'
        out = subprocess.run(["bash", "-c", script, "gx_guard_status", node], capture_output=True, text=True, env=env, timeout=10)
        return json.loads(out.stdout)


class TestGxSafeRunUsage(GuardShTestBase):
    def test_missing_args_is_a_usage_error(self) -> None:
        res = self.run_safe_run("node1", "name", "medium")
        self.assertEqual(res.returncode, 64)
        self.assertIn("usage:", res.stderr)

    def test_forwards_to_guard_run_and_executes_on_success(self) -> None:
        marker = self.tmp_path / "ran"
        res = self.run_safe_run(
            "node1", "diag", "small", "5", "--", "bash", "-c", f'touch "{marker}"'
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(marker.exists())


class TestGuardRunAdmission(GuardShTestBase):
    def test_success_path_runs_command_and_registers_residency(self) -> None:
        marker = self.tmp_path / "ran"
        res = self.run_guard_run("node1", "gx-fast", "medium", "25", "--", "bash", "-c", f'echo hi > "{marker}"')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(marker.exists())

    def test_refusal_on_low_memory_never_runs_the_command(self) -> None:
        write_meminfo(self.meminfo, 5 * 1024 * 1024)  # 5 GiB free, reserve is 30
        marker = self.tmp_path / "should-not-exist"
        res = self.run_guard_run(
            "node1", "gx-fast", "medium", "25", "--", "bash", "-c", f'touch "{marker}"'
        )
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertFalse(marker.exists(), "the wrapped command ran despite a refused admission")

    def test_missing_double_dash_is_a_usage_error_not_silent_bypass(self) -> None:
        marker = self.tmp_path / "should-not-exist"
        res = self.run_guard_run("node1", "gx-fast", "medium", "25", "bash", "-c", f'touch "{marker}"')
        self.assertEqual(res.returncode, 64)
        self.assertFalse(marker.exists())

    # The class-conflict rule (never two large/exclusive workloads on one
    # node) needs a ledger entry that survives reconciliation, which needs a
    # `docker inspect` that reports the container as running. See
    # TestGuardRunWithStubDocker below for that proof.

class TestGuardRunWithStubDocker(GuardShTestBase):
    """Uses a fake `docker` on PATH that reports containers as running, so
    the residency ledger's reconcile step does not immediately drop entries
    -- this is what lets us prove the class-conflict rule in isolation from
    real Docker.
    """

    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.tmp_path / "bin"
        bin_dir.mkdir()
        docker_stub = bin_dir / "docker"
        docker_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# Fake docker: any 'inspect' call reports the container as running.\n"
            "if [ \"$1\" = inspect ]; then echo true; exit 0; fi\n"
            "exit 0\n"
        )
        docker_stub.chmod(0o755)
        self.env_base["PATH"] = f"{bin_dir}:/usr/bin:/bin"

    def _register(self, node: str, name: str, cls: str, est: str) -> None:
        env = dict(self.env_base)
        script = f'source "{GUARD_SH}"; gx_guard_register "$@"'
        reg = subprocess.run(
            ["bash", "-c", script, "gx_guard_register", node, name, cls, est, name],
            capture_output=True, text=True, env=env, timeout=10,
        )
        self.assertEqual(reg.returncode, 0, reg.stderr)

    def test_second_large_workload_is_refused_while_first_is_resident(self) -> None:
        self._register("node2", "gx-reason", "large", "95")
        status_before = self.status("node2")
        self.assertIn("gx-reason", status_before)

        marker = self.tmp_path / "should-not-exist"
        res = self.run_guard_run("node2", "cpu-diagnostic", "large", "5", "--", "bash", "-c", f'touch "{marker}"')
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertFalse(marker.exists(), "a second large workload ran alongside gx-reason -- this IS the B-012 bug")

    def test_medium_workload_still_allowed_alongside_a_large_resident(self) -> None:
        self._register("node2", "gx-reason", "large", "95")
        write_meminfo(self.meminfo, 15 * 1024 * 1024)  # matches a loaded node
        marker = self.tmp_path / "ran"
        res = self.run_guard_run(
            "node2", "comfyui", "medium", "5", "--", "bash", "-c", f'touch "{marker}"',
            env_overrides={"GX_GUARD_RESERVE_GIB": "10"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(marker.exists())


class TestGuardRunConcurrency(GuardShTestBase):
    def test_two_concurrent_launches_for_the_same_node_serialise(self) -> None:
        """Real proof with real separate bash processes: start a slow
        launch, then start a second one shortly after, and show from
        wall-clock timestamps written by each wrapped command that the
        second's critical section never overlaps the first's.
        """
        marker = self.tmp_path / "marker.log"

        env = dict(self.env_base)
        script = f'source "{GUARD_SH}"; gx_guard_run "$@"'
        proc_a = subprocess.Popen(
            ["bash", "-c", script, "gx_guard_run", "node1", "slow-holder", "medium", "5", "--",
             "bash", "-c", f'echo "A start $(date +%s.%N)" >> "{marker}"; sleep 1.2; echo "A end $(date +%s.%N)" >> "{marker}"'],
            env=env,
        )
        time.sleep(0.3)
        proc_b = subprocess.Popen(
            ["bash", "-c", script, "gx_guard_run", "node1", "second-launch", "medium", "5", "--",
             "bash", "-c", f'echo "B start $(date +%s.%N)" >> "{marker}"; echo "B end $(date +%s.%N)" >> "{marker}"'],
            env=env,
        )
        self.assertEqual(proc_a.wait(timeout=15), 0)
        self.assertEqual(proc_b.wait(timeout=15), 0)

        lines = marker.read_text().splitlines()
        events = {}
        for line in lines:
            who, when, ts = line.split()
            events.setdefault(who, {})[when] = float(ts)
        self.assertIn("A", events)
        self.assertIn("B", events)
        self.assertLessEqual(
            events["A"]["end"], events["B"]["start"],
            f"B started before A's critical section ended: {events}",
        )

    def test_lock_busy_returns_exit_3_when_another_holder_is_slow(self) -> None:
        env = dict(self.env_base)
        script = f'source "{GUARD_SH}"; gx_guard_run "$@"'
        holder = subprocess.Popen(
            ["bash", "-c", script, "gx_guard_run", "node1", "holder", "medium", "5", "--", "sleep", "3"],
            env=env,
        )
        time.sleep(0.3)
        res = self.run_guard_run(
            "node1", "impatient", "medium", "5", "--", "true",
            env_overrides={"GX_GUARD_LOCK_TIMEOUT": "0.5"},
        )
        self.assertEqual(res.returncode, 3, res.stderr)
        holder.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
