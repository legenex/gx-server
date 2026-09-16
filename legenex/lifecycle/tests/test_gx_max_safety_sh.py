"""Unit tests for legenex/lifecycle/gx-max-safety.sh (D-025).

The rules are exercised against fake /proc files and a fake clock, so each
case is deterministic and needs no real memory pressure. What matters most is
the negative space: a healthy load that dips hard and spills into swap must
NOT be aborted -- that is exactly what the old 2 GiB tripwire got wrong.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SAFETY = Path(__file__).resolve().parents[1] / "gx-max-safety.sh"


class SafetyHarness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.set(avail_mib=110_000, swap_free_mib=64_000, swap_total_mib=64_000,
                 pswpin=0, pswpout=0, oom=0, psi_full=0)
        (tmp / "now").write_text("1000")
        (tmp / "fork").write_text("1")
        (tmp / "klog").write_text("")
        stub = tmp / "klog.sh"
        stub.write_text(f"#!/bin/sh\ncat {tmp}/klog\n")
        stub.chmod(0o755)

    def set(self, *, avail_mib, swap_free_mib, swap_total_mib=64_000, pswpin=0, pswpout=0, oom=0, psi_full=0):
        (self.tmp / "meminfo").write_text(
            f"MemAvailable: {avail_mib * 1024} kB\n"
            f"SwapTotal: {swap_total_mib * 1024} kB\nSwapFree: {swap_free_mib * 1024} kB\n"
        )
        (self.tmp / "vmstat").write_text(f"pswpin {pswpin}\npswpout {pswpout}\noom_kill {oom}\n")
        (self.tmp / "psi").write_text(
            f"some avg10={psi_full}.00 avg60=0.00 avg300=0.00 total=0\n"
            f"full avg10={psi_full}.00 avg60=0.00 avg300=0.00 total=0\n"
        )

    def run(self, steps: str, **env: str) -> list[str]:
        """Run a bash script; `steps` may call `at <t>`, `tick <phase>`, `setmem ...`."""
        exports = "".join(f"export {k}={v}\n" for k, v in env.items())
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            export GXS_MEMINFO={self.tmp}/meminfo GXS_VMSTAT={self.tmp}/vmstat
            export GXS_PSI={self.tmp}/psi GXS_KLOG_CMD={self.tmp}/klog.sh
            {exports}
            source {SAFETY}
            _gxs_now() {{ cat {self.tmp}/now; }}
            _gxs_fork_ms() {{ cat {self.tmp}/fork; }}
            at() {{ echo "$1" > {self.tmp}/now; }}
            tick() {{ gxs_tick "$1"; echo "$(cat {self.tmp}/now) $GXS_VERDICT $GXS_REASON"; }}
            gxs_arm
            """
        ) + textwrap.dedent(steps)
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise AssertionError(f"harness failed rc={out.returncode}: {out.stderr}")
        return [line for line in out.stdout.splitlines() if line.strip()]


class GxMaxSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = SafetyHarness(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def verdicts(self, lines):
        return [line.split()[1] for line in lines]

    def test_healthy_load_dip_into_swap_is_not_aborted(self):
        # The 2026-09-14 verified shape: ~1 GiB available, swap filling fast,
        # one-way swap-out, for several minutes.
        self.h.set(avail_mib=900, swap_free_mib=20_000, pswpout=0)
        steps = ""
        for i, t in enumerate(range(1005, 1300, 5)):
            steps += f"at {t}\n"
            steps += (
                f"printf 'pswpin 10\\npswpout {(i + 1) * 50000}\\noom_kill 0\\n' > {self.h.tmp}/vmstat\n"
                "tick load\n"
            )
        self.assertTrue(all(v == "ok" for v in self.verdicts(self.h.run(steps))))

    def test_single_instant_below_old_2gib_floor_is_not_an_abort(self):
        self.h.set(avail_mib=100, swap_free_mib=30_000)
        lines = self.h.run("at 1005\ntick load\n")
        self.assertEqual(self.verdicts(lines), ["ok"])

    def test_exhaustion_must_be_sustained(self):
        self.h.set(avail_mib=100, swap_free_mib=500)
        lines = self.h.run("at 1005\ntick load\nat 1020\ntick load\nat 1036\ntick load\n")
        self.assertEqual(self.verdicts(lines), ["ok", "ok", "abort"])
        self.assertIn("exhausted", lines[-1])

    def test_exhaustion_timer_resets_when_pressure_clears(self):
        lines = self.h.run(
            f"""
            printf 'MemAvailable: 102400 kB\\nSwapTotal: 65536000 kB\\nSwapFree: 512000 kB\\n' > {self.h.tmp}/meminfo
            at 1005; tick load
            at 1020; tick load
            printf 'MemAvailable: 8000000 kB\\nSwapTotal: 65536000 kB\\nSwapFree: 512000 kB\\n' > {self.h.tmp}/meminfo
            at 1025; tick load
            printf 'MemAvailable: 102400 kB\\nSwapTotal: 65536000 kB\\nSwapFree: 512000 kB\\n' > {self.h.tmp}/meminfo
            at 1040; tick load
            at 1050; tick load
            """
        )
        self.assertEqual(self.verdicts(lines), ["ok"] * 5)

    def test_kernel_oom_kill_is_immediate(self):
        lines = self.h.run(
            f"printf 'pswpin 0\\npswpout 0\\noom_kill 1\\n' > {self.h.tmp}/vmstat\nat 1005\ntick load\n"
        )
        self.assertEqual(self.verdicts(lines), ["abort"])
        self.assertIn("OOM", lines[0])

    def test_preexisting_oom_count_is_not_an_abort(self):
        self.h.set(avail_mib=110_000, swap_free_mib=64_000, oom=4)
        self.assertEqual(self.verdicts(self.h.run("at 1005\ntick load\n")), ["ok"])

    def test_hard_nvidia_no_memory_is_immediate(self):
        (self.h.tmp / "klog").write_text("NVRM: alloc failed: Out of memory [NV_ERR_NO_MEMORY]\n")
        lines = self.h.run("at 1005\ntick load\n")
        self.assertEqual(self.verdicts(lines), ["abort"])
        self.assertIn("NV_ERR_NO_MEMORY", lines[0])

    def test_soft_nolog_nvidia_no_memory_is_counted_not_fatal(self):
        # Verbatim shape of the lines both nodes emit at "Load weight begin".
        line = ("NVRM: nvCheckOkFailedNoLog: Check failed: Out of memory [NV_ERR_NO_MEMORY] "
                "(0x00000051) returned from _memdescAllocInternal(pMemDesc) @ mem_desc.c:1359\n")
        (self.h.tmp / "klog").write_text(line * 10)
        lines = self.h.run("at 1005\ntick load\necho soft=$GXS_NV_SOFT hard=$GXS_NV_HARD\n")
        self.assertEqual(lines[0].split()[1], "ok")
        self.assertEqual(lines[1], "soft=10 hard=0")

    def test_thrashing_needs_pressure_and_swapin_sustained(self):
        steps = ""
        for i, t in enumerate(range(1010, 1200, 10)):
            steps += (
                f"printf 'pswpin {(i + 1) * 100000}\\npswpout {(i + 1) * 100000}\\noom_kill 0\\n' > {self.h.tmp}/vmstat\n"
                f"at {t}\ntick load\n"
            )
        self.h.set(avail_mib=2000, swap_free_mib=30_000, psi_full=60)
        v = self.verdicts(self.h.run(steps))
        # 20 s of window before the first delta, then 120 s sustain.
        self.assertEqual(v[0], "ok")
        self.assertIn("abort", v)
        self.assertGreaterEqual(v.index("abort"), 12)

    def test_high_pressure_without_swapin_is_not_thrash(self):
        self.h.set(avail_mib=2000, swap_free_mib=30_000, psi_full=90)
        steps = "".join(f"at {t}\ntick load\n" for t in range(1010, 1300, 10))
        self.assertTrue(all(v == "ok" for v in self.verdicts(self.h.run(steps))))

    def test_management_plane_starvation_sustained(self):
        steps = f"echo 9000 > {self.h.tmp}/fork\n" + "".join(f"at {t}\ntick load\n" for t in (1005, 1030, 1066))
        v = self.verdicts(self.h.run(steps))
        self.assertEqual(v, ["ok", "ok", "abort"])

    def test_steady_floor_only_in_steady_phase(self):
        self.h.set(avail_mib=1000, swap_free_mib=30_000)
        lines = self.h.run("at 1005\ntick load\nat 1100\ntick load\nat 1105\ntick steady\nat 1170\ntick steady\n")
        self.assertEqual(self.verdicts(lines), ["ok", "ok", "ok", "abort"])
        self.assertIn("steady-state", lines[-1])

    def test_clean_start_facts_report_swapfile(self):
        swaps = self.h.tmp / "swaps"
        swaps.write_text("Filename Type Size Used Priority\n/swapfile-sglang file 1 0 -3\n")
        out = subprocess.run(
            ["bash", "-c", f"GXS_MEMINFO={self.h.tmp}/meminfo GXS_VMSTAT={self.h.tmp}/vmstat "
             f"GXS_PSI={self.h.tmp}/psi GXS_SWAPS={swaps} bash -c 'source {SAFETY}; gxs_clean_start_facts /swapfile-sglang'"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("swapfile_active=1", out.stdout)
        self.assertIn("avail_mib=110000", out.stdout)
        swaps.write_text("Filename Type Size Used Priority\n/swap.img file 1 0 -2\n")
        out = subprocess.run(
            ["bash", "-c", f"GXS_SWAPS={swaps} bash -c 'source {SAFETY}; gxs_clean_start_facts /swapfile-sglang'"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("swapfile_active=0", out.stdout)


if __name__ == "__main__":
    unittest.main()
