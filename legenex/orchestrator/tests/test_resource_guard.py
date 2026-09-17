"""Tests for the node-level admission-control / resource-ownership guard.

These are the tests that back the 2026-09-14 admission-control task, born
from BLOCKERS.md B-012 (node 2 wedged by two ~77-95GiB models at once).
Nothing here touches a real node: /proc/meminfo is faked via a temp file,
`docker inspect` is faked via an injectable `is_running` callable, and all
state lives in a TemporaryDirectory.

Where the task specifically asked for proof rather than assertion of intent
(a *real* concurrency test), these tests spawn actual separate OS processes
-- not just Python threads -- racing for the same `NodeLock`, and separately
prove interop with the plain `flock` shell command so a bash caller and this
Python module genuinely contend for the identical kernel-level lock.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
from pathlib import Path

_ORCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ORCH_DIR))

from gx_orchestrator.resource_guard import (  # noqa: E402
    AdmissionRefused,
    NodeLock,
    NodeLockBusy,
    ResidencyLedger,
    WorkloadClass,
    check_admission,
    compute_admission,
    guard_launch,
    read_mem_available_gib,
    release_workload,
)

HAVE_FLOCK = shutil.which("flock") is not None


def write_meminfo(path: Path, mem_available_kib: int) -> None:
    path.write_text(
        "MemTotal:       127535340 kB\n"
        f"MemAvailable:   {mem_available_kib} kB\n"
        "SwapTotal:       67108856 kB\n"
    )


class TestReadMemAvailable(unittest.TestCase):
    def test_reads_real_looking_meminfo(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "meminfo"
            write_meminfo(p, 40 * 1024 * 1024)  # 40 GiB
            self.assertAlmostEqual(read_mem_available_gib(p), 40.0, places=3)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            read_mem_available_gib("/no/such/path/meminfo")

    def test_missing_field_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "meminfo"
            p.write_text("MemTotal: 1000 kB\n")
            with self.assertRaises(RuntimeError):
                read_mem_available_gib(p)


class TestComputeAdmission(unittest.TestCase):
    def test_allows_when_comfortably_within_budget(self) -> None:
        r = compute_admission(
            "node1", 25.0, current_residency_gib=10.0, mem_available_gib=100.0,
            reserve_gib=30.0, node_total_gib=121.0,
        )
        self.assertTrue(r.allowed, r.reason)

    def test_refuses_on_ledger_intent_math_even_if_memory_looks_free(self) -> None:
        # This is the B-012 shape from the OTHER side: the ledger already
        # accounts for a resident 95GiB workload (gx-reason); admitting a
        # second 77GiB one must be refused by the intent check even before
        # touching /proc/meminfo semantics.
        r = compute_admission(
            "node2", 77.0, current_residency_gib=95.0, mem_available_gib=118.0,
            reserve_gib=30.0, node_total_gib=121.0,
        )
        self.assertFalse(r.allowed)
        self.assertIn("exceeds node total", r.reason)

    def test_refuses_on_live_measurement_even_if_ledger_is_clean(self) -> None:
        # Ledger says nothing is resident (e.g. it was started outside this
        # module -- the exact B-012 bypass), but real MemAvailable says the
        # node cannot actually absorb this workload without eating the
        # reserve.
        r = compute_admission(
            "node2", 77.0, current_residency_gib=0.0, mem_available_gib=95.0,
            reserve_gib=30.0, node_total_gib=121.0,
        )
        self.assertFalse(r.allowed)
        self.assertIn("reserve floor", r.reason)

    def test_refusal_is_exact_boundary_not_fuzzy(self) -> None:
        # 121 total - 30 reserve = 91 exactly admissible with 0 residency.
        ok = compute_admission("node1", 91.0, current_residency_gib=0.0, mem_available_gib=121.0,
                                reserve_gib=30.0, node_total_gib=121.0)
        refused = compute_admission("node1", 91.1, current_residency_gib=0.0, mem_available_gib=121.0,
                                     reserve_gib=30.0, node_total_gib=121.0)
        self.assertTrue(ok.allowed)
        self.assertFalse(refused.allowed)


class TestResidencyLedger(unittest.TestCase):
    def test_add_and_total(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = ResidencyLedger(Path(d) / "node2-residency.json", is_running=lambda c: True)
            ledger.add("gx-reason", node="node2", workload_class=WorkloadClass.LARGE, estimated_gib=95.0)
            self.assertAlmostEqual(ledger.total_gib("node2"), 95.0)
            self.assertEqual(ledger.exclusive_residents("node2"), ["gx-reason"])

    def test_reconcile_drops_dead_containers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            alive = {"gx-reason", "stray-diagnostic"}
            ledger = ResidencyLedger(Path(d) / "node2-residency.json", is_running=lambda c: c in alive)
            ledger.add("gx-reason", node="node2", workload_class=WorkloadClass.LARGE, estimated_gib=95.0)
            ledger.add("stray-diagnostic", node="node2", workload_class=WorkloadClass.LARGE, estimated_gib=77.0)
            self.assertAlmostEqual(ledger.total_gib("node2"), 172.0)

            alive.clear()
            alive.add("gx-reason")
            # stray-diagnostic's container died (crashed, was killed, node
            # rebooted...) -- reconcile must drop it without any manual
            # intervention.
            self.assertAlmostEqual(ledger.total_gib("node2"), 95.0)
            data = json.loads((Path(d) / "node2-residency.json").read_text())
            self.assertEqual(set(data), {"gx-reason"})

    def test_remove(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = ResidencyLedger(Path(d) / "node2-residency.json", is_running=lambda c: True)
            ledger.add("gx-reason", node="node2", workload_class=WorkloadClass.LARGE, estimated_gib=95.0)
            ledger.remove("gx-reason")
            self.assertAlmostEqual(ledger.total_gib("node2"), 0.0)

    def test_corrupt_ledger_file_treated_as_empty_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "node2-residency.json"
            p.write_text("{not valid json")
            ledger = ResidencyLedger(p, is_running=lambda c: True)
            self.assertEqual(ledger.reconcile(), {})


class TestCheckAdmission(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.meminfo = self.state_dir / "meminfo"
        write_meminfo(self.meminfo, 100 * 1024 * 1024)  # 100 GiB available

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_refuses_two_large_workloads_on_one_node_regardless_of_arithmetic(self) -> None:
        # This is the literal B-012 incident: gx-reason (~95GiB) already
        # resident, plenty of *headroom by the numbers* (100GiB available),
        # but a second LARGE workload must still be refused outright.
        ledger = ResidencyLedger(self.state_dir / "node2-residency.json", is_running=lambda c: True)
        ledger.add("gx-reason", node="node2", workload_class=WorkloadClass.LARGE, estimated_gib=95.0)

        result = check_admission(
            "node2", "cpu-diagnostic", WorkloadClass.LARGE, 5.0,
            state_dir=self.state_dir, meminfo_path=self.meminfo,
            is_running=lambda c: True,
        )
        self.assertFalse(result.allowed)
        self.assertIn("gx-reason", result.reason)

    def test_allows_medium_alongside_a_large_resident(self) -> None:
        ledger = ResidencyLedger(self.state_dir / "node2-residency.json", is_running=lambda c: True)
        ledger.add("gx-reason", node="node2", workload_class=WorkloadClass.LARGE, estimated_gib=95.0)
        write_meminfo(self.meminfo, 20 * 1024 * 1024)  # only 20 GiB free, matches a loaded node

        result = check_admission(
            "node2", "comfyui", WorkloadClass.MEDIUM, 5.0,
            state_dir=self.state_dir, meminfo_path=self.meminfo,
            reserve_gib=10.0, is_running=lambda c: True,
        )
        self.assertTrue(result.allowed, result.reason)

    def test_refuses_when_it_would_eat_the_reserve(self) -> None:
        write_meminfo(self.meminfo, 35 * 1024 * 1024)  # 35 GiB free
        result = check_admission(
            "node1", "gx-fast-2", WorkloadClass.MEDIUM, 25.0,
            state_dir=self.state_dir, meminfo_path=self.meminfo,
            reserve_gib=30.0, is_running=lambda c: True,
        )
        self.assertFalse(result.allowed)


class TestGuardLaunch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.meminfo = self.state_dir / "meminfo"
        write_meminfo(self.meminfo, 100 * 1024 * 1024)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_refused_launch_never_runs_caller_body_and_registers_nothing(self) -> None:
        write_meminfo(self.meminfo, 5 * 1024 * 1024)  # 5 GiB free -- must refuse
        body_ran = False
        with self.assertRaises(AdmissionRefused):
            with guard_launch(
                "node1", "gx-fast", WorkloadClass.MEDIUM, 25.0,
                state_dir=self.state_dir, meminfo_path=self.meminfo, reserve_gib=30.0,
            ) as (_ctx, _ledger):
                body_ran = True  # pragma: no cover - must never execute
        self.assertFalse(body_ran, "caller body executed despite a refused admission")
        ledger = ResidencyLedger(self.state_dir / "node1-residency.json", is_running=lambda c: True)
        self.assertEqual(ledger.reconcile(), {})

    def test_successful_launch_registers_residency_and_releases_lock(self) -> None:
        with guard_launch(
            "node1", "gx-fast", WorkloadClass.MEDIUM, 25.0,
            state_dir=self.state_dir, meminfo_path=self.meminfo, reserve_gib=30.0,
            is_running=lambda c: True,
        ) as (ctx, ledger):
            self.assertTrue(ctx.admission.allowed)
            ledger.add("gx-fast", node="node1", workload_class=WorkloadClass.MEDIUM, estimated_gib=25.0, container="gx-fast")

        ledger2 = ResidencyLedger(self.state_dir / "node1-residency.json", is_running=lambda c: True)
        self.assertAlmostEqual(ledger2.total_gib("node1"), 25.0)

        # The lock must be free again -- a second guard_launch call for a
        # DIFFERENT node-exclusive workload should immediately see the new
        # residency and be refused (never silently proceed).
        with self.assertRaises(AdmissionRefused):
            with guard_launch(
                "node1", "gx-max-rank0", WorkloadClass.EXCLUSIVE, 90.0,
                state_dir=self.state_dir, meminfo_path=self.meminfo, reserve_gib=30.0,
                is_running=lambda c: True, lock_timeout=5.0,
            ):
                pass

    def test_release_workload_removes_ledger_entry(self) -> None:
        with guard_launch(
            "node2", "gx-reason", WorkloadClass.LARGE, 95.0,
            state_dir=self.state_dir, meminfo_path=self.meminfo, reserve_gib=3.0,
            is_running=lambda c: True,
        ) as (_ctx, ledger):
            ledger.add("gx-reason", node="node2", workload_class=WorkloadClass.LARGE, estimated_gib=95.0)

        release_workload(self.state_dir, "node2", "gx-reason")
        ledger2 = ResidencyLedger(self.state_dir / "node2-residency.json", is_running=lambda c: True)
        self.assertEqual(ledger2.reconcile(), {})


class TestNodeLockRealConcurrency(unittest.TestCase):
    """Proof, not assertion: races the lock with real separate processes."""

    _WORKER = textwrap.dedent(
        """
        import sys, time, json, os
        sys.path.insert(0, {orch_dir!r})
        from gx_orchestrator.resource_guard import NodeLock
        lock = NodeLock({lockfile!r})
        marker = {marker!r}
        hold = float(sys.argv[1])
        acquired_marker = sys.argv[2] if len(sys.argv) > 2 else None
        with lock.acquire(timeout=20, workload=f"pid-{{os.getpid()}}"):
            if acquired_marker:
                open(acquired_marker, "w").write("acquired")
            with open(marker, "a") as f:
                f.write(json.dumps({{"pid": os.getpid(), "event": "start", "t": time.time()}}) + "\\n")
            time.sleep(hold)
            with open(marker, "a") as f:
                f.write(json.dumps({{"pid": os.getpid(), "event": "end", "t": time.time()}}) + "\\n")
        """
    )

    def _worker_script(self, tmp: Path, lockfile: Path, marker: Path) -> Path:
        script = tmp / "worker.py"
        script.write_text(
            self._WORKER.format(orch_dir=str(_ORCH_DIR), lockfile=str(lockfile), marker=str(marker))
        )
        return script

    def test_five_concurrent_processes_never_overlap_in_critical_section(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            lockfile = tmp / "node1.lock"
            marker = tmp / "marker.jsonl"
            script = self._worker_script(tmp, lockfile, marker)

            procs = [
                subprocess.Popen([sys.executable, str(script), "0.3"])
                for _ in range(5)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=30), 0)

            events = [json.loads(line) for line in marker.read_text().splitlines() if line.strip()]
            self.assertEqual(len(events), 10, "expected a start+end pair per process")

            # Reconstruct intervals and assert NO two intervals from
            # different pids overlap -- true mutual exclusion, not merely
            # "it happened to run in order this time".
            intervals: dict[int, list[float]] = {}
            for e in events:
                intervals.setdefault(e["pid"], [None, None])
                idx = 0 if e["event"] == "start" else 1
                intervals[e["pid"]][idx] = e["t"]
            spans = sorted(intervals.values(), key=lambda s: s[0])
            for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
                self.assertLessEqual(e1, s2, f"critical sections overlapped: {(s1, e1)} vs {(s2, e2)}")

    def test_two_concurrent_acquire_calls_for_same_node_cannot_both_succeed_at_once(self) -> None:
        """Directly targets the deliverable's proof requirement (#7)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            lockfile = tmp / "node2.lock"
            marker = tmp / "marker.jsonl"
            script = self._worker_script(tmp, lockfile, marker)

            a_marker = tmp / "a-acquired"
            b_marker = tmp / "b-acquired"
            proc_a = subprocess.Popen([sys.executable, str(script), "1.0", str(a_marker)])
            time.sleep(0.15)  # let A get in first
            proc_b = subprocess.Popen([sys.executable, str(script), "0.1", str(b_marker)])

            # While A is inside, B must NOT yet have acquired.
            time.sleep(0.2)
            self.assertTrue(a_marker.exists(), "process A never acquired the lock")
            self.assertFalse(b_marker.exists(), "process B acquired the lock WHILE A still held it")

            self.assertEqual(proc_a.wait(timeout=20), 0)
            self.assertEqual(proc_b.wait(timeout=20), 0)
            self.assertTrue(b_marker.exists(), "process B never acquired the lock at all")

    def test_lock_released_when_holder_is_sigkilled(self) -> None:
        """Stale-lock recovery for 'process died': flock is released by the
        kernel even on SIGKILL, so a fresh acquirer is never stuck.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            lockfile = tmp / "node1.lock"
            marker = tmp / "marker.jsonl"
            acquired = tmp / "acquired"
            script = tmp / "holder.py"
            script.write_text(
                textwrap.dedent(
                    f"""
                    import sys, time
                    sys.path.insert(0, {str(_ORCH_DIR)!r})
                    from gx_orchestrator.resource_guard import NodeLock
                    lock = NodeLock({str(lockfile)!r})
                    with lock.acquire(timeout=20, workload="doomed"):
                        open({str(acquired)!r}, "w").write("x")
                        time.sleep(30)
                    """
                )
            )
            proc = subprocess.Popen([sys.executable, str(script)])
            deadline = time.time() + 10
            while not acquired.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(acquired.exists(), "holder never acquired the lock")

            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)

            lock = NodeLock(lockfile)
            start = time.monotonic()
            with lock.acquire(timeout=5, workload="recovery"):
                pass
            self.assertLess(
                time.monotonic() - start, 4.0,
                "a fresh acquire should succeed almost immediately after the holder was killed",
            )

    @unittest.skipUnless(HAVE_FLOCK, "flock(1) not available on this system")
    def test_bash_flock_and_python_nodelock_contend_for_the_identical_lock(self) -> None:
        """Proves cross-language interop, not just cross-process: a plain
        `flock` shell invocation and `NodeLock.acquire` must be mutually
        exclusive on the same path. This is what lets gx-max-start.sh /
        gx-safe-run.sh (bash) and gx_orchestrator (Python) share one lock.
        """
        with tempfile.TemporaryDirectory() as d:
            lockfile = Path(d) / "node1.lock"
            lock = NodeLock(lockfile)
            with lock.acquire(timeout=5, workload="python-holds"):
                busy = subprocess.run(["flock", "-n", str(lockfile), "-c", "true"])
                self.assertNotEqual(busy.returncode, 0, "bash flock acquired a lock Python was holding")

            free = subprocess.run(["flock", "-n", str(lockfile), "-c", "true"])
            self.assertEqual(free.returncode, 0, "bash flock could not acquire after Python released")

            # And the reverse direction: bash holds, Python must be refused.
            hold_proc = subprocess.Popen(["flock", str(lockfile), "-c", "sleep 1"])
            time.sleep(0.15)
            with self.assertRaises(NodeLockBusy):
                with lock.acquire(timeout=0.3, workload="python-tries"):
                    pass  # pragma: no cover
            hold_proc.wait(timeout=5)


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.meminfo = self.state_dir / "meminfo"
        write_meminfo(self.meminfo, 100 * 1024 * 1024)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(_ORCH_DIR)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "gx_orchestrator.resource_guard", "--state-dir", str(self.state_dir), *args],
            capture_output=True, text=True, env=self.env, timeout=15,
        )

    #: A container name that cannot collide with anything real on these hosts.
    #: The previous version of this test used "gx-fast", which is a REAL
    #: production container -- so the test passed only while gx-fast happened
    #: to be unloaded, and failed the moment anything (an acceptance run, a
    #: user request) had it resident. Found 2026-09-16. Tests must not depend
    #: on which models are currently loaded on the machine running them.
    ABSENT_CONTAINER = "gx-test-absent-container-do-not-create"

    def test_check_register_status_release_roundtrip(self) -> None:
        name = self.ABSENT_CONTAINER
        chk = self._run(
            "check", "--node", "node1", "--name", name, "--class", "medium",
            "--estimated-gib", "25", "--meminfo-path", str(self.meminfo),
        )
        self.assertEqual(chk.returncode, 0, chk.stderr)
        self.assertTrue(json.loads(chk.stdout)["allowed"])

        reg = self._run("register", "--node", "node1", "--name", name, "--class", "medium", "--estimated-gib", "25")
        self.assertEqual(reg.returncode, 0, reg.stderr)

        status = self._run("status", "--node", "node1")
        self.assertEqual(status.returncode, 0, status.stderr)
        data = json.loads(status.stdout)
        # is_running defaults to a real `docker inspect`, which will report
        # "not running" for a container that was never started, so the
        # freshly-registered entry reconciles away immediately. That is
        # correct behaviour, not a test bug: it proves the CLI's default
        # reconciliation really shells out rather than trusting the file.
        self.assertEqual(data, {})

        rel = self._run("release", "--node", "node1", "--name", name)
        self.assertEqual(rel.returncode, 0, rel.stderr)

    def test_check_exit_code_2_on_refusal(self) -> None:
        write_meminfo(self.meminfo, 1 * 1024 * 1024)  # 1 GiB free
        chk = self._run(
            "check", "--node", "node1", "--name", "gx-fast", "--class", "medium",
            "--estimated-gib", "25", "--meminfo-path", str(self.meminfo), "--reserve-gib", "30",
        )
        self.assertEqual(chk.returncode, 2)
        self.assertFalse(json.loads(chk.stdout)["allowed"])


class TakeoverAdmissionTests(unittest.TestCase):
    """gx-max cluster-takeover policy (D-025)."""

    CLEAN = "avail_mib=113000 swap_free_mib=60000 swap_total_mib=65535 swapfile_active=1 psi_full10=0 oom_kill=4"

    def setUp(self) -> None:
        from gx_orchestrator.resource_guard import NodeFacts, TakeoverPolicy, compute_takeover_admission
        self.NodeFacts, self.Policy, self.decide = NodeFacts, TakeoverPolicy, compute_takeover_admission
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _facts(self, **over):
        kv = dict(p.split("=") for p in self.CLEAN.split())
        kv.update({k: str(v) for k, v in over.items()})
        return self.NodeFacts.parse(" ".join(f"{k}={v}" for k, v in kv.items()))

    def test_clean_node_is_admitted_even_though_peak_plus_reserve_exceeds_node(self):
        # 117 + 30 > 121.63: the old formula refused this forever (B-022).
        r = self.decide("node1", self._facts(), other_exclusive_residents=[])
        self.assertTrue(r.allowed, r.reason)
        self.assertEqual(r.numbers["startup_transient_gib"], 117.0)
        old = compute_admission("node1", 117.0, current_residency_gib=0, mem_available_gib=113.0)
        self.assertFalse(old.allowed)

    def test_refuses_when_swapfile_inactive(self):
        r = self.decide("node2", self._facts(swapfile_active=0), other_exclusive_residents=[])
        self.assertFalse(r.allowed)
        self.assertIn("swapfile-sglang", r.reason)

    def test_refuses_without_swap_headroom(self):
        r = self.decide("node1", self._facts(swap_free_mib=10000), other_exclusive_residents=[])
        self.assertFalse(r.allowed)
        self.assertIn("swap free", r.reason)

    def test_refuses_when_not_drained(self):
        r = self.decide("node2", self._facts(avail_mib=70000), other_exclusive_residents=[])
        self.assertFalse(r.allowed)
        self.assertIn("clean-start minimum", r.reason)

    def test_refuses_with_other_exclusive_resident(self):
        r = self.decide("node2", self._facts(), other_exclusive_residents=["gx-reason"])
        self.assertFalse(r.allowed)
        self.assertIn("gx-reason", r.reason)

    def test_refuses_under_existing_pressure(self):
        r = self.decide("node1", self._facts(psi_full10=30), other_exclusive_residents=[])
        self.assertFalse(r.allowed)
        self.assertIn("pressure", r.reason)

    def test_malformed_facts_rejected(self):
        with self.assertRaises(ValueError):
            self.NodeFacts.parse("avail_mib=abc")

    def test_ordinary_tiers_keep_the_30gib_reserve(self):
        r = compute_admission("node2", 45.0, current_residency_gib=0, mem_available_gib=70.0)
        self.assertFalse(r.allowed)
        self.assertEqual(r.numbers["reserve_gib"], 30.0)

    def test_cli_takeover_check_exit_codes(self):
        base = [sys.executable, "-m", "gx_orchestrator.resource_guard", "--state-dir", self.tmp.name,
                "takeover-check", "--node", "node1", "--name", "gx-max-rank0"]
        ok = subprocess.run(base + ["--facts", self.CLEAN], cwd=_ORCH_DIR, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(json.loads(ok.stdout)["allowed"])
        bad = subprocess.run(base + ["--facts", "garbage"], cwd=_ORCH_DIR, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        low = subprocess.run(base + ["--facts", self.CLEAN, "--min-avail-gib", "200"], cwd=_ORCH_DIR,
                             capture_output=True, text=True)
        self.assertEqual(low.returncode, 2)

    def test_read_node_facts_parses_proc_files(self):
        from gx_orchestrator.resource_guard import read_node_facts
        d = Path(self.tmp.name)
        (d / "meminfo").write_text("MemTotal: 1 kB\nMemAvailable: 2097152 kB\nSwapTotal: 4194304 kB\nSwapFree: 3145728 kB\n")
        (d / "swaps").write_text("Filename Type Size Used Priority\n/swapfile-sglang file 1 0 -3\n")
        (d / "psi").write_text("some avg10=1.00 avg60=0 avg300=0 total=0\nfull avg10=2.50 avg60=0 avg300=0 total=0\n")
        f = read_node_facts(meminfo_path=d / "meminfo", swaps_path=d / "swaps", psi_path=d / "psi")
        self.assertEqual((f.avail_mib, f.swap_free_mib, f.swap_total_mib, f.swapfile_active, f.psi_full10),
                         (2048, 3072, 4096, True, 2.5))

    def test_health_probe_ignores_undrained_memory_but_not_missing_swapfile(self):
        from gx_orchestrator import server
        from gx_orchestrator import resource_guard as rg
        busy = self._facts(avail_mib=40000)  # gx-mini/gx-fast loaded, not drained
        with unittest.mock.patch.object(rg, "read_node_facts", return_value=busy):
            self.assertEqual(server._gx_max_admission_blocked(), "")
        noswap = self._facts(swapfile_active=0)
        with unittest.mock.patch.object(rg, "read_node_facts", return_value=noswap):
            self.assertIn("swapfile-sglang", server._gx_max_admission_blocked())

    def test_ledger_exclusive_resident_blocks_cli(self):
        ResidencyLedger(Path(self.tmp.name) / "node1-residency.json", is_running=lambda c: True).add(
            "gx-fast-big", node="node1", workload_class=WorkloadClass.LARGE, estimated_gib=60)
        from gx_orchestrator.resource_guard import check_takeover_admission
        r = check_takeover_admission("node1", "gx-max-rank0", self._facts(), state_dir=Path(self.tmp.name),
                                     is_running=lambda c: True)
        self.assertFalse(r.allowed)
        self.assertIn("gx-fast-big", r.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class MaintenanceHoldTests(unittest.TestCase):
    """D-037: a node in Maintenance refuses every sanctioned launch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hold_refuses_ordinary_admission(self):
        from gx_orchestrator.resource_guard import WorkloadClass, check_admission

        ok = check_admission("node2", "gx-music", WorkloadClass.MEDIUM, 32, state_dir=self.state,
                             mem_available_gib=110, is_running=lambda c: False)
        self.assertTrue(ok.allowed)
        (self.state / "node2.maintenance-hold").write_text("x")
        refused = check_admission("node2", "gx-music", WorkloadClass.MEDIUM, 32, state_dir=self.state,
                                  mem_available_gib=110, is_running=lambda c: False)
        self.assertFalse(refused.allowed)
        self.assertIn("maintenance", refused.reason)
        # a hold on the other node does not matter
        self.assertTrue(check_admission("node1", "x", WorkloadClass.SMALL, 1, state_dir=self.state,
                                        mem_available_gib=110, is_running=lambda c: False).allowed)

    def test_hold_refuses_guard_launch_without_running_the_body(self):
        from gx_orchestrator.resource_guard import AdmissionRefused, WorkloadClass, guard_launch

        (self.state / "node2.maintenance-hold").write_text("x")
        ran = []
        with self.assertRaises(AdmissionRefused):
            with guard_launch("node2", "gx-music", WorkloadClass.MEDIUM, 32, state_dir=self.state,
                              mem_available_gib=110, is_running=lambda c: False):
                ran.append(1)
        self.assertEqual(ran, [])

    def test_hold_refuses_gxmax_takeover(self):
        from gx_orchestrator.resource_guard import NodeFacts, check_takeover_admission

        facts = NodeFacts(avail_mib=115_000, swap_free_mib=60_000, swap_total_mib=64_000,
                          swapfile_active=True, psi_full10=0.0)
        self.assertTrue(check_takeover_admission("node1", "gx-max-rank0", facts, state_dir=self.state,
                                                 is_running=lambda c: False).allowed)
        (self.state / "node1.maintenance-hold").write_text("x")
        self.assertFalse(check_takeover_admission("node1", "gx-max-rank0", facts, state_dir=self.state,
                                                  is_running=lambda c: False).allowed)
