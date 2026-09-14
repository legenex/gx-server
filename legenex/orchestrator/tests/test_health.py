"""Tests for per-tier availability probing (gx_orchestrator.health).

These cover the bug this module fixes: a tier's availability must reflect
its REAL upstream (each node's own llama-swap), not just whether the LiteLLM
gateway process answers -- see CURRENT_STATE.md / the gx-reason incident.

A stub HTTP server stands in for llama-swap's `/v1/models` control endpoint,
so the real cluster is never touched.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator.config import Config  # noqa: E402
from gx_orchestrator.health import (  # noqa: E402
    AliasState,
    TierHealth,
    TierStatus,
    _swap_model_status,
    _tier_status_from_swap,
)
from gx_orchestrator.tiers import Tier  # noqa: E402

API_KEY = "test-swap-key"


def _models_body(*entries: tuple[str, str]) -> bytes:
    """Build a llama-swap `/v1/models` body: (model_id, status_value) pairs."""
    return json.dumps(
        {
            "data": [
                {"id": model_id, "name": model_id, "status": {"value": status}}
                for model_id, status in entries
            ],
            "object": "list",
        }
    ).encode()


class _StubSwap(http.server.BaseHTTPRequestHandler):
    """Stands in for one node's llama-swap `/v1/models` endpoint."""

    body: bytes = b"{}"
    #: None disables the auth check entirely (server never answers 401).
    required_key: str | None = API_KEY

    def do_GET(self):  # noqa: N802
        if self.path != "/v1/models":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if type(self).required_key is not None:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {type(self).required_key}":
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        body = type(self).body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: A003
        pass


class HealthTestBase(unittest.TestCase):
    """Runs two stub swap servers standing in for node 1 and node 2."""

    def setUp(self) -> None:
        self.node1_handler = type("Node1Handler", (_StubSwap,), {})
        self.node2_handler = type("Node2Handler", (_StubSwap,), {})
        self.node1_handler.body = _models_body(("gx-mini", "loaded"), ("gx-fast", "unloaded"))
        self.node2_handler.body = _models_body(("gx-reason", "loaded"))

        self.node1_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.node1_handler)
        self.node2_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.node2_handler)
        threading.Thread(target=self.node1_srv.serve_forever, daemon=True).start()
        threading.Thread(target=self.node2_srv.serve_forever, daemon=True).start()
        self.node1_port = self.node1_srv.server_address[1]
        self.node2_port = self.node2_srv.server_address[1]

    def tearDown(self) -> None:
        self.node1_srv.shutdown()
        self.node2_srv.shutdown()

    def _cfg(self, *, node2_base: str | None = None, api_key: str | None = API_KEY) -> Config:
        import os

        if api_key is not None:
            os.environ["GX_SWAP_API_KEY"] = api_key
        else:
            os.environ.pop("GX_SWAP_API_KEY", None)
        return Config(
            node1_swap_base=f"http://127.0.0.1:{self.node1_port}",
            node2_swap_base=node2_base or f"http://127.0.0.1:{self.node2_port}",
            node1_probe_timeout=2.0,
            node2_probe_timeout=1.0,
        )


class TestSwapStatusParsing(unittest.TestCase):
    """Pure-function tests: no network at all."""

    def test_loaded_is_ready_and_usable(self):
        body = json.loads(_models_body(("gx-mini", "loaded")))
        raw, found = _swap_model_status(body, "gx-mini")
        status = _tier_status_from_swap(raw, found)
        self.assertEqual(status.state, AliasState.READY)
        self.assertTrue(status.usable)

    def test_unloaded_is_stopped_but_still_usable(self):
        """llama-swap starts models on-demand: unloaded != unavailable."""
        body = json.loads(_models_body(("gx-fast", "unloaded")))
        raw, found = _swap_model_status(body, "gx-fast")
        status = _tier_status_from_swap(raw, found)
        self.assertEqual(status.state, AliasState.STOPPED)
        self.assertTrue(status.usable)

    def test_loading_is_usable(self):
        body = json.loads(_models_body(("gx-reason", "loading")))
        raw, found = _swap_model_status(body, "gx-reason")
        status = _tier_status_from_swap(raw, found)
        self.assertEqual(status.state, AliasState.LOADING)
        self.assertTrue(status.usable)

    def test_failed_status_is_not_usable(self):
        body = json.loads(_models_body(("gx-reason", "failed")))
        raw, found = _swap_model_status(body, "gx-reason")
        status = _tier_status_from_swap(raw, found)
        self.assertEqual(status.state, AliasState.FAILED)
        self.assertFalse(status.usable)

    def test_model_missing_from_list_is_failed_not_available(self):
        """A tier llama-swap has never heard of must never report healthy."""
        body = json.loads(_models_body(("gx-mini", "loaded")))
        raw, found = _swap_model_status(body, "gx-fast")
        self.assertFalse(found)
        status = _tier_status_from_swap(raw, found)
        self.assertEqual(status.state, AliasState.FAILED)
        self.assertFalse(status.usable)
        self.assertEqual(status.reason, "not_configured_in_llama_swap")

    def test_unrecognised_status_value_defaults_to_unavailable(self):
        """An unrecognised status must never be treated as a green light."""
        body = json.loads(_models_body(("gx-mini", "some-future-status")))
        raw, found = _swap_model_status(body, "gx-mini")
        status = _tier_status_from_swap(raw, found)
        self.assertEqual(status.state, AliasState.UNAVAILABLE)
        self.assertFalse(status.usable)


class TestTierHealthSnapshot(HealthTestBase):
    def test_node1_tiers_reflect_real_llama_swap_state(self):
        health = TierHealth(self._cfg(), ttl=0)
        snap = health.snapshot()
        self.assertEqual(snap[Tier.MINI].state, AliasState.READY)
        self.assertTrue(snap[Tier.MINI].usable)
        self.assertEqual(snap[Tier.FAST].state, AliasState.STOPPED)
        self.assertTrue(snap[Tier.FAST].usable)

    def test_node2_tier_reflects_its_own_llama_swap_not_the_gateway(self):
        health = TierHealth(self._cfg(), ttl=0)
        snap = health.snapshot()
        self.assertEqual(snap[Tier.REASON].state, AliasState.READY)
        self.assertTrue(snap[Tier.REASON].usable)

    def test_node2_unreachable_reports_node2_offline_not_available(self):
        """The bug this module fixes: gx-reason must NOT report available when
        node 2 -- its only upstream -- cannot be reached at all.
        """
        # Port 1 on loopback refuses connections immediately (fast, no hang).
        health = TierHealth(self._cfg(node2_base="http://127.0.0.1:1"), ttl=0)
        snap = health.snapshot()
        self.assertEqual(snap[Tier.REASON].state, AliasState.UNAVAILABLE)
        self.assertFalse(snap[Tier.REASON].usable)
        self.assertEqual(snap[Tier.REASON].reason, "node2_offline")
        # node 1's tiers must be unaffected by node 2 being down.
        self.assertTrue(snap[Tier.MINI].usable)
        self.assertTrue(snap[Tier.FAST].usable)

    def test_node1_unreachable_reports_node1_reason_and_does_not_affect_node2(self):
        health = TierHealth(self._cfg(), ttl=0)
        # Point node 1 at a refusing port after construction to simulate it
        # going away; TierHealth re-probes every call when ttl=0.
        health._cfg = Config(
            node1_swap_base="http://127.0.0.1:1",
            node2_swap_base=health._cfg.node2_swap_base,
            node1_probe_timeout=1.0,
            node2_probe_timeout=1.0,
        )
        snap = health.snapshot()
        self.assertEqual(snap[Tier.MINI].state, AliasState.UNAVAILABLE)
        self.assertEqual(snap[Tier.MINI].reason, "node1_llama_swap_unreachable")
        self.assertFalse(snap[Tier.MINI].usable)
        self.assertEqual(snap[Tier.FAST].reason, "node1_llama_swap_unreachable")
        # node 2 (gx-reason) must be unaffected.
        self.assertTrue(snap[Tier.REASON].usable)

    def test_bad_auth_is_failed_not_offline(self):
        """A 401 from a reachable llama-swap is a config problem, not a dead
        node -- must be distinguishable from `node2_offline`.
        """
        health = TierHealth(self._cfg(api_key="wrong-key"), ttl=0)
        snap = health.snapshot()
        self.assertEqual(snap[Tier.REASON].state, AliasState.FAILED)
        self.assertEqual(snap[Tier.REASON].reason, "llama_swap_error_401")
        self.assertFalse(snap[Tier.REASON].usable)

    def test_snapshot_is_cached_within_ttl(self):
        health = TierHealth(self._cfg(), ttl=30.0)
        first = health.snapshot()
        # Break node 2 without touching the cache: a cached snapshot must not
        # re-probe within its TTL.
        health._cfg = Config(
            node1_swap_base=health._cfg.node1_swap_base,
            node2_swap_base="http://127.0.0.1:1",
        )
        second = health.snapshot()
        self.assertEqual(first[Tier.REASON].state, second[Tier.REASON].state)
        self.assertEqual(second[Tier.REASON].state, AliasState.READY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
