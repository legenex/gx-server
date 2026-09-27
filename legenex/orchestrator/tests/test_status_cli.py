"""Tests for `gx status` (gx_orchestrator.status_cli, V4.1 shape).

Pure functions (meminfo parsing, report assembly, rendering) are tested
directly; host and network pieces (uname, docker ps, the orchestrator's
/text/status) run through fakes so these tests never touch a real cluster.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator import status_cli  # noqa: E402
from gx_orchestrator.config import Config  # noqa: E402
from tests.registry_fixtures import write_fixture_registry  # noqa: E402


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


_MEMINFO = """MemTotal:       132_000_000 kB
MemFree:         10_000_000 kB
MemAvailable:    96_000_000 kB
SwapTotal:       50_331_648 kB
SwapFree:        50_331_000 kB
"""


class TestPurePieces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.reg_path = write_fixture_registry(self.dir / "registry.json")
        self.cfg = Config(
            registry_path=self.reg_path,
            head_container="dsv41-exl3-head",
            worker_container="dsv41-exl3-worker",
        )

    def test_read_meminfo(self):
        p = self.dir / "meminfo"
        p.write_text(_MEMINFO)
        info = status_cli.read_meminfo(str(p))
        self.assertEqual(info["MemAvailable"], 96_000_000)
        self.assertEqual(info["SwapTotal"], 50_331_648)
        self.assertIsNone(status_cli.read_meminfo(str(self.dir / "missing")))

    def test_local_node1_facts(self):
        status_cli._run = lambda cmd, timeout=5.0: {
            ("uname", "-r"): "6.17.0-1032-nvidia",
        }.get(tuple(cmd))
        status_cli.read_meminfo = lambda path="/proc/meminfo": {
            "MemTotal": 132_000_000, "MemAvailable": 96_000_000,
            "SwapTotal": 50_331_648, "SwapFree": 50_331_000,
        }
        status_cli._docker_ps = lambda: [{"name": "dsv41-exl3-head", "status": "Up 2 hours"}]
        try:
            facts = status_cli.local_node1_facts(self.cfg)
        finally:
            import importlib
            importlib.reload(status_cli)
        self.assertTrue(facts["online"])
        self.assertEqual(facts["kernel"], "6.17.0-1032-nvidia")
        self.assertTrue(facts["kernel_pinned"])
        self.assertEqual(facts["ram_available_gib"], 91.6)
        self.assertEqual(facts["workload"], "dsv41-exl3-head")
        self.assertEqual(facts["containers"], [{"name": "dsv41-exl3-head", "status": "Up 2 hours"}])

    def test_kernel_not_pinned_is_flagged(self):
        status_cli._run = lambda cmd, timeout=5.0: "7.0.0" if cmd[:1] == ["uname"] else None
        status_cli.read_meminfo = lambda path="/proc/meminfo": None
        status_cli._docker_ps = lambda: []
        try:
            facts = status_cli.local_node1_facts(self.cfg)
        finally:
            import importlib
            importlib.reload(status_cli)
        self.assertFalse(facts["kernel_pinned"])
        self.assertIn("NOT the pinned kernel", status_cli.render_human({
            "generated_at": "t", "model": {"model_id": "m", "uncensored": True, "quant": "q"},
            "orchestrator": {"reachable": False, "base": "b", "error": "x", "cluster": {}, "queue": {}},
            "node1": facts,
        }))

    def test_registry_model_facts(self):
        facts = status_cli.registry_model_facts(self.cfg)
        self.assertEqual(facts["model_id"], "DeepSeek-v4.1-Flash-EXL3")
        self.assertTrue(facts["uncensored"])
        self.assertEqual(facts["quant"], "exl3-2.9bpw-mul1")
        self.assertEqual(facts["max_context"], 262144)
        self.assertTrue(facts["vision"])
        self.assertTrue(facts["tools"])

    def test_registry_model_facts_error_is_explicit(self):
        facts = status_cli.registry_model_facts(Config(registry_path=self.dir / "nope.json"))
        self.assertIn("error", facts)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = Config(registry_path=write_fixture_registry(self.dir / "r.json"))
        self.snapshot = {
            "model": {
                "id": "DeepSeek-v4.1-Flash-EXL3", "uncensored": True,
                "profile": "balanced", "nodes": {
                    "head": {"healthy": True, "serves_model": True, "detail": ""},
                    "worker": {"ssh_reachable": True, "container_running": True,
                               "fabric": {"192.168.100.11": True, "192.168.101.11": False},
                               "detail": ""},
                    "mem": {"node1_gib": 25.0, "node2_gib": 30.0},
                },
                "lifecycle": {"state": "ready", "seconds_in_state": 120},
            },
            "queue": {"active": 1, "capacity": 2, "queued": 3, "oldest_wait_seconds": 4},
        }

    def test_build_report_unreachable_orchestrator(self):
        status_cli.fetch_orchestrator_snapshot = lambda base, timeout=5.0: {
            "reachable": False, "error": "connection refused",
        }
        try:
            report = status_cli.build_report(self.cfg, "http://127.0.0.1:18999")
        finally:
            import importlib
            importlib.reload(status_cli)
        self.assertFalse(report["orchestrator"]["reachable"])
        self.assertEqual(report["orchestrator"]["error"], "connection refused")
        text = status_cli.render_human(report)
        self.assertIn("UNREACHABLE", text)
        self.assertIn("unknown, not assumed", text)
        self.assertIn("MODEL", text)  # registry facts still render

    def test_build_report_and_human_rendering(self):
        status_cli.fetch_orchestrator_snapshot = lambda base, timeout=5.0: {
            "reachable": True, "error": "", **self.snapshot,
        }
        try:
            report = status_cli.build_report(self.cfg, "http://127.0.0.1:18900")
        finally:
            import importlib
            importlib.reload(status_cli)
        self.assertTrue(report["orchestrator"]["reachable"])
        self.assertEqual(report["orchestrator"]["cluster"]["profile"], "balanced")
        text = status_cli.render_human(report)
        self.assertIn("CLUSTER  ready  profile=balanced", text)
        self.assertIn("head  : healthy (model id verified)", text)
        self.assertIn("worker: container running (fabric 192.168.100.11:up, 192.168.101.11:DOWN)", text)
        self.assertIn("mem   : node1=25.0 GiB  node2=30.0 GiB", text)
        self.assertIn("QUEUE  active=1/2  queued=3  oldest_wait=4s", text)
        self.assertIn("DeepSeek-v4.1-Flash-EXL3", text)
        self.assertIn("uncensored", text)

    def test_worker_problem_line(self):
        self.snapshot["model"]["nodes"]["worker"]["ssh_reachable"] = False
        self.snapshot["model"]["nodes"]["worker"]["container_running"] = False
        lines = status_cli._one_line_nodes(self.snapshot["model"])
        self.assertIn("worker: PROBLEM (ssh/container unreachable)", lines)

    def test_json_rendering_round_trips(self):
        report = {
            "generated_at": "t", "node1": {}, "model": {"model_id": "m"},
            "orchestrator": {"reachable": False, "base": "b", "error": "e",
                             "cluster": {}, "queue": {}},
        }
        self.assertEqual(json.loads(status_cli.render_json(report)), report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
