"""D-044: every Control Center call to the gx-orchestrator carries its bearer key."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_control_ui.services import Cluster  # noqa: E402
from support import StubUpstream, TempEnv  # noqa: E402

KEY = "test-orchestrator-key"


class TestOrchestratorAuth(unittest.TestCase):
    def setUp(self):
        self.stub = StubUpstream({
            ("GET", "/lifecycle/gx-max/status"): (200, {"state": "down"}),
            ("GET", "/lifecycle/gx-max/events"): (200, {"seq": 0, "events": [], "history": [], "active_job": None}),
        })
        self.env = TempEnv(orchestrator_base=self.stub.url, offline=False)
        self.addCleanup(self.stub.close)
        self.addCleanup(self.env.cleanup)

    def test_lifecycle_probes_send_the_orchestrator_key(self):
        with mock.patch.dict(os.environ, {"GX_ORCHESTRATOR_API_KEY": KEY}):
            out = Cluster(self.env.cfg)._lifecycle()
        self.assertTrue(out["status"]["ok"] and out["events"]["ok"])
        self.assertEqual(len(self.stub.calls), 2)
        for _method, _path, headers, _body in self.stub.calls:
            self.assertEqual(headers.get("Authorization"), f"Bearer {KEY}")

    def test_no_key_configured_sends_no_authorization_header(self):
        env = {k: v for k, v in os.environ.items() if k != "GX_ORCHESTRATOR_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(Cluster(self.env.cfg).orch_headers(), {})


if __name__ == "__main__":
    unittest.main()
