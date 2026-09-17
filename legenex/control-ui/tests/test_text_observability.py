"""Text-alias latency / budget / routing facts on the model cards (D-039)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import support  # noqa: F401  (sets sys.path)
from gx_control_ui.models import text_live
from gx_control_ui.services import gateway_text_metrics


def _svc(text_body=None, gateway=None, ok=True):
    return {
        "text_status": {"ok": ok, "body": text_body or {}},
        "gateway_text": gateway or {"by_alias": {}, "recent_failures": {}},
    }


class TestGatewayMetricsReader(unittest.TestCase):
    def test_newest_record_per_alias_and_recent_failures(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway-text.jsonl"
            rows = [
                {"ts": now - 50, "alias": "gx-fast", "outcome": "ok", "ttft_ms": 70},
                {"ts": now - 40, "alias": "gx-reason", "outcome": "APIConnectionError", "status": None},
                {"ts": now - 30, "alias": "gx-fast", "outcome": "ok", "ttft_ms": 65},
                {"ts": now - 5000, "alias": "gx-mini", "outcome": "Timeout"},
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
            out = gateway_text_metrics(path)
        self.assertEqual(out["by_alias"]["gx-fast"]["ttft_ms"], 65)
        self.assertEqual(out["recent_failures"], {"gx-reason": 1})

    def test_missing_file_is_empty(self):
        out = gateway_text_metrics(Path("/nonexistent/gx-text.jsonl"))
        self.assertEqual(out["by_alias"], {})


class TestTextLive(unittest.TestCase):
    def test_newest_of_orchestrator_and_gateway_wins(self):
        now = time.time()
        body = {"aliases": {"gx-fast": {"context_limit": 131072, "last_request": {"ts": now - 100, "outcome": "ok", "via": "gx-auto"}}}}
        gw = {"by_alias": {"gx-fast": {"ts": now - 10, "outcome": "ok", "ttft_ms": 60}}, "recent_failures": {}}
        t = text_live("gx-fast", _svc(body, gw), now)
        self.assertEqual(t["context_limit"], 131072)
        self.assertEqual(t["last_request"]["ttft_ms"], 60)
        self.assertFalse(t["degraded"])

    def test_recent_server_failure_is_degraded(self):
        now = time.time()
        gw = {"by_alias": {"gx-reason": {"ts": now - 30, "outcome": "InternalServerError", "status": "500"}},
              "recent_failures": {"gx-reason": 1}}
        self.assertTrue(text_live("gx-reason", _svc({}, gw), now)["degraded"])

    def test_client_errors_and_old_failures_are_not_degraded(self):
        now = time.time()
        for rec in (
            {"ts": now - 30, "outcome": "context_length_exceeded", "status": 400},
            {"ts": now - 30, "outcome": "HTTPException", "status": "400"},
            {"ts": now - 30, "outcome": "weird", "status": 404},
            {"ts": now - 5000, "outcome": "InternalServerError", "status": 500},
        ):
            gw = {"by_alias": {"gx-fast": rec}, "recent_failures": {}}
            self.assertFalse(text_live("gx-fast", _svc({}, gw), now)["degraded"], rec)

    def test_orchestrator_unreachable_is_reported(self):
        t = text_live("gx-auto", _svc(ok=False))
        self.assertFalse(t["available"])
        self.assertIsNone(t["last_request"])


if __name__ == "__main__":
    unittest.main()
