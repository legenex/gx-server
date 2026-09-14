"""Tests for `gx status` (gx_orchestrator.status_cli).

Pure-function pieces (meminfo parsing, alias-table derivation, rendering) are
tested directly. Network/host-dependent pieces (docker, ping, the live
orchestrator) are exercised through fakes so these tests never touch the
real cluster.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator import status_cli  # noqa: E402
from gx_orchestrator.tiers import Tier  # noqa: E402


class TestReadMeminfo(unittest.TestCase):
    def test_parses_real_looking_meminfo(self):
        sample = (
            "MemTotal:       126934440 kB\n"
            "MemFree:         5000000 kB\n"
            "MemAvailable:   117440512 kB\n"
            "SwapTotal:       66060288 kB\n"
            "SwapFree:        66060288 kB\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo", delete=False) as fh:
            fh.write(sample)
            path = fh.name
        try:
            info = status_cli.read_meminfo(path)
            self.assertEqual(info["MemTotal"], 126934440)
            self.assertEqual(info["MemAvailable"], 117440512)
            self.assertEqual(info["SwapFree"], 66060288)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_missing_file_returns_none_not_raise(self):
        self.assertIsNone(status_cli.read_meminfo("/nonexistent/meminfo"))

    def test_real_proc_meminfo_is_readable_and_has_expected_keys(self):
        """This runs on a real Linux host in CI/dev, so /proc/meminfo really
        exists -- assert the parser survives the real file, not just a fixture.
        """
        info = status_cli.read_meminfo()
        self.assertIsNotNone(info)
        self.assertIn("MemTotal", info)
        self.assertIn("MemAvailable", info)
        self.assertGreater(info["MemTotal"], 0)


class TestBuildAliasTable(unittest.TestCase):
    def test_orchestrator_unreachable_marks_everything_unavailable_not_guessed(self):
        snap = status_cli.OrchestratorSnapshot(reachable=False, error="Connection refused")
        aliases = status_cli.build_alias_table(snap, node2_online=True)
        for tier in (Tier.MINI, Tier.FAST, Tier.REASON, Tier.MAX, Tier.AUTO):
            self.assertEqual(aliases[tier.value]["state"], "unavailable")
            self.assertFalse(aliases[tier.value]["usable"])
            self.assertIn("orchestrator_unreachable", aliases[tier.value]["reason"])

    def test_reachable_orchestrator_passes_tier_data_through_unchanged(self):
        tiers = {
            "gx-mini": {"state": "ready", "usable": True, "reason": "loaded"},
            "gx-fast": {"state": "stopped", "usable": True, "reason": "unloaded"},
            "gx-reason": {"state": "unavailable", "usable": False, "reason": "node2_offline"},
            "gx-max": {"state": "stopped", "usable": True, "reason": "node2_unavailable"},
        }
        snap = status_cli.OrchestratorSnapshot(reachable=True, tiers=tiers)
        aliases = status_cli.build_alias_table(snap, node2_online=False)
        self.assertEqual(aliases["gx-mini"], tiers["gx-mini"])
        self.assertEqual(aliases["gx-reason"], tiers["gx-reason"])
        # gx-auto is synthesised locally: reachable orchestrator -> ready.
        self.assertEqual(aliases["gx-auto"]["state"], "ready")
        self.assertTrue(aliases["gx-auto"]["usable"])

    def test_image_video_report_node2_offline_when_node2_is_down(self):
        snap = status_cli.OrchestratorSnapshot(reachable=True, tiers={})
        aliases = status_cli.build_alias_table(snap, node2_online=False)
        self.assertEqual(aliases["gx-image"]["reason"], "node2_offline")
        self.assertEqual(aliases["gx-video"]["reason"], "node2_offline")
        self.assertFalse(aliases["gx-image"]["usable"])

    def test_survives_an_old_orchestrator_still_returning_bare_booleans(self):
        """Regression test for a real incident hit while writing this script:
        the orchestrator process running live had not yet reloaded this
        session's fix and still answered `/health/detailed` with the OLD
        `{"gx-mini": true, ...}` shape. build_report() must degrade cleanly
        instead of raising AttributeError on `True.get(...)`.
        """
        snap = status_cli.OrchestratorSnapshot(
            reachable=True,
            tiers={"gx-mini": True, "gx-fast": True, "gx-reason": True, "gx-max": True},
        )
        aliases = status_cli.build_alias_table(snap, node2_online=True)
        for tier in ("gx-mini", "gx-fast", "gx-reason", "gx-max"):
            self.assertEqual(aliases[tier]["state"], "unavailable")
            self.assertFalse(aliases[tier]["usable"])
            self.assertIn("unrecognised_orchestrator_response", aliases[tier]["reason"])

    def test_image_video_never_claim_ready_even_when_node2_is_up(self):
        """The orchestrator does not own the media router: gx-status must not
        fake success for it just because node 2's kernel answers ICMP.
        """
        snap = status_cli.OrchestratorSnapshot(reachable=True, tiers={})
        aliases = status_cli.build_alias_table(snap, node2_online=True)
        self.assertEqual(aliases["gx-image"]["state"], "unavailable")
        self.assertNotEqual(aliases["gx-image"]["reason"], "node2_offline")
        self.assertFalse(aliases["gx-image"]["usable"])


class TestRendering(unittest.TestCase):
    """Rendering must not crash on a well-formed report, and JSON must
    round-trip through the standard library `json` module.
    """

    def _sample_report(self) -> dict:
        return {
            "generated_at": "2026-09-14T23:00:00+0200",
            "node1": {
                "online": True,
                "kernel": "6.17.0-1032-nvidia",
                "kernel_pinned": True,
                "expected_kernel": "6.17.0-1032-nvidia",
                "ram_total_gib": 121.0,
                "ram_available_gib": 105.0,
                "swap_total_gib": 63.0,
                "swap_free_gib": 63.0,
                "workload": ["gx-mini"],
                "containers": [{"name": "gx-mini", "status": "Up 1 minute"}],
                "gateway": {"litellm": True, "llama_swap": True},
            },
            "node2": {
                "online": False,
                "note": "no ICMP reply within 2s (single probe, not retried)",
                "llama_swap": {"state": "unavailable", "usable": False, "reason": "node2_offline"},
                "workload": None,
            },
            "aliases": {
                "gx-mini": {"state": "ready", "usable": True, "reason": "loaded"},
                "gx-fast": {"state": "stopped", "usable": True, "reason": "unloaded"},
                "gx-reason": {"state": "unavailable", "usable": False, "reason": "node2_offline"},
                "gx-max": {"state": "stopped", "usable": True, "reason": "node2_unavailable"},
                "gx-auto": {"state": "ready", "usable": True, "reason": "orchestrator routing"},
                "gx-image": {"state": "unavailable", "usable": False, "reason": "node2_offline"},
                "gx-video": {"state": "unavailable", "usable": False, "reason": "node2_offline"},
            },
            "orchestrator": {"reachable": True, "base": "http://127.0.0.1:18900", "error": ""},
        }

    def test_render_json_round_trips(self):
        import json

        report = self._sample_report()
        text = status_cli.render_json(report)
        self.assertEqual(json.loads(text), report)

    def test_render_human_contains_every_alias_and_flags_unpinned_kernel(self):
        report = self._sample_report()
        report["node1"]["kernel_pinned"] = False
        report["node1"]["kernel"] = "7.0.0-1019-nvidia"
        text = status_cli.render_human(report)
        for alias in report["aliases"]:
            self.assertIn(alias, text)
        self.assertIn("NOT the pinned kernel", text)
        self.assertIn("B-001", text)


class TestNode2KernelReachable(unittest.TestCase):
    def test_unroutable_address_is_reported_offline_quickly(self):
        """TEST-NET-1 (192.0.2.0/24, RFC 5737) is reserved for documentation
        and never routes anywhere -- a safe stand-in for "node 2 is down"
        that does not touch the real cluster.
        """
        import time

        from gx_orchestrator.config import Config

        cfg = Config(node2_swap_base="http://192.0.2.1:28080")
        start = time.time()
        reachable = status_cli.node2_kernel_reachable(cfg)
        elapsed = time.time() - start
        self.assertFalse(reachable)
        # Single probe, not retried: must not take much longer than the
        # configured ping deadline.
        self.assertLess(elapsed, status_cli._NODE2_PING_DEADLINE_S + 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
