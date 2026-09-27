"""Tests for gx_orchestrator.health (V4.1: ONE model, three separate facts).

Head health means /health AND /v1/models advertising the RIGHT served id (a
vLLM proxy answering with the wrong model is a config fault, not health).
Worker health means management-SSH + the worker container + at least one
fabric rail answering ping (B-012: kernel-ICMP-up while userspace is wedged
is a real failure mode on this cluster). All probes are pluggable; these
tests are hermetic.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator.config import Config  # noqa: E402
from gx_orchestrator.health import (  # noqa: E402
    AliasState,
    ClusterHealth,
    _ssh_mem_gib,
)
from tests.registry_fixtures import load_fixture_registry  # noqa: E402

MODEL_ID = "DeepSeek-v4.1-Flash-EXL3"


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _cfg() -> Config:
    return Config(
        gxmax_base="http://127.0.0.1:8888/v1",
        gxmax_model_id=MODEL_ID,
        node2_ssh="legenex-02@10.60.21.41",
        worker_container="dsv41-exl3-worker",
    )


class HealthHarness:
    """Pluggable host facts; every probe records what it was asked."""

    def __init__(self, *, models=None, healthy=True, ssh_ok=True, container=True,
                 fabric=True, mem1=100.0, mem2=100.0):
        self.models = models if models is not None else {"data": [{"id": MODEL_ID}]}
        self.healthy = healthy
        self.ssh_ok = ssh_ok
        self.container = container
        self.fabric = fabric
        self.mem1, self.mem2 = mem1, mem2
        self.http_calls: list[str] = []
        self.ssh_calls: list[tuple[str, str]] = []
        self.ping_calls: list[str] = []

    def http_get(self, url, timeout=0, headers=None):
        self.http_calls.append(url)
        if url.endswith("/health"):
            if not self.healthy:
                raise ConnectionError("connection refused")
            return _Resp({})
        if url.endswith("/models"):
            return _Resp(self.models)
        raise AssertionError(f"unexpected probe {url}")

    def ssh_run(self, target, cmd, timeout):
        self.ssh_calls.append((target, cmd))
        if not self.ssh_ok:
            return None
        if "MemAvailable" in cmd:
            return str(self.mem2)
        return "dsv41-exl3-worker" if self.container else "some-other-container"

    def ping(self, host, timeout):
        self.ping_calls.append(host)
        return self.fabric

    def local_mem(self):
        return self.mem1

    def health(self, cfg=None, ttl=0.0):
        return ClusterHealth(
            cfg or _cfg(), load_fixture_registry(),
            ttl=ttl, ssh_run=self.ssh_run, ping=self.ping,
            local_mem_gib=self.local_mem, http_get=self.http_get,
        )


class TestHeadProbe(unittest.TestCase):
    def test_healthy_with_verified_model_id(self):
        h = HealthHarness()
        st = h.health().head_status()
        self.assertIs(st.state, AliasState.READY)
        self.assertTrue(st.usable)
        self.assertEqual(
            h.http_calls,
            ["http://127.0.0.1:8888/health", "http://127.0.0.1:8888/v1/models"],
        )

    def test_health_ok_but_wrong_model_id_is_failed_not_ready(self):
        h = HealthHarness(models={"data": [{"id": "SomeOtherModel"}]})
        st = h.health().head_status()
        self.assertIs(st.state, AliasState.FAILED)
        self.assertFalse(st.usable)
        self.assertIn("expected", st.reason)

    def test_unreachable_head_is_unavailable(self):
        h = HealthHarness(healthy=False)
        st = h.health().head_status()
        self.assertIs(st.state, AliasState.UNAVAILABLE)
        self.assertFalse(st.usable)
        self.assertIn("health probe failed", st.reason)


class TestWorkerProbe(unittest.TestCase):
    def test_worker_ok_needs_ssh_container_and_a_fabric_rail(self):
        h = HealthHarness()
        self.assertTrue(h.health().worker_ok())
        # fabric IPs come from the registry's worker node, not from code
        self.assertEqual(set(h.ping_calls), {"192.168.100.11", "192.168.101.11"})
        self.assertTrue(all(target == "legenex-02@10.60.21.41" for target, _ in h.ssh_calls))

    def test_ssh_down(self):
        self.assertFalse(HealthHarness(ssh_ok=False).health().worker_ok())

    def test_worker_container_missing(self):
        self.assertFalse(HealthHarness(container=False).health().worker_ok())

    def test_all_fabric_rails_down_kills_serving(self):
        self.assertFalse(HealthHarness(fabric=False).health().worker_ok())

    def test_one_fabric_rail_up_is_enough(self):
        h = HealthHarness()
        h.fabric = False
        h.ping = lambda host, timeout: host == "192.168.100.11"
        self.assertTrue(h.health().worker_ok())


class TestMemory(unittest.TestCase):
    def test_snapshot_reports_both_nodes(self):
        h = HealthHarness(mem1=64.2, mem2=31.0)
        snap = h.health().snapshot()
        self.assertEqual(snap["mem"], {"node1_gib": 64.2, "node2_gib": 31.0})

    def test_ssh_mem_gib_parses_and_degrades(self):
        self.assertEqual(_ssh_mem_gib("t", lambda t, c, to: "96.15", 1), 96.2)
        self.assertIsNone(_ssh_mem_gib("t", lambda t, c, to: "", 1))
        self.assertIsNone(_ssh_mem_gib("t", lambda t, c, to: "garbage", 1))
        self.assertIsNone(_ssh_mem_gib("t", lambda t, c, to: None, 1))


class TestCache(unittest.TestCase):
    def test_snapshot_is_cached_on_ttl(self):
        h = HealthHarness()
        ch = h.health(ttl=60.0)
        first = ch.snapshot()
        h.healthy = False  # the world changes; the cache must not notice
        self.assertEqual(ch.snapshot(), first)
        self.assertEqual(len(h.http_calls), 2)  # only the first refresh probed

    def test_cache_expires(self):
        h = HealthHarness()
        ch = h.health(ttl=0.0)
        ch.snapshot()
        h.container = False
        snap = ch.snapshot()
        self.assertFalse(snap["worker"]["container_running"])

    def test_checked_timestamp_present(self):
        snap = HealthHarness().health().snapshot()
        self.assertIsInstance(snap["checked"], float)
        self.assertLessEqual(snap["checked"], time.time() + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
