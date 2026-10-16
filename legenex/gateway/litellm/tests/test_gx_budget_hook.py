"""gx_budget_hook (D-053 review): signed identity, namespaced fallbacks, and a
switch-validation token that is forwarded but never logged.

    python3 -m unittest discover -s legenex/gateway/litellm/tests
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ORCH = ROOT / "legenex" / "orchestrator"
os.environ["GX_HOOK_LIB"] = str(ORCH)
os.environ["GX_TEXT_METRICS_LOG"] = "/tmp/gx-hook-test-metrics.jsonl"
# Stub the LiteLLM base class: the hook only subclasses it.
for name in ("litellm", "litellm.integrations", "litellm.integrations.custom_logger"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["litellm.integrations.custom_logger"].CustomLogger = type("CustomLogger", (), {})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ORCH))

import gx_hooks.gx_budget_hook as H  # noqa: E402
from gx_orchestrator.identity import identity_from_headers  # noqa: E402

KEY = "hook-test-identity-key-0123456789"


class Key:
    def __init__(self, **kw):
        self.key_alias = kw.get("key_alias")
        self.team_alias = kw.get("team_alias")
        self.user_id = kw.get("user_id")


class TestIdentity(unittest.TestCase):
    def test_fallbacks_are_namespaced(self):
        self.assertEqual(H._identity(Key(key_alias="odin")), "odin")
        self.assertEqual(H._identity(Key(team_alias="odin")), "team:odin")
        self.assertEqual(H._identity(Key(user_id="odin")), "user:odin")
        self.assertEqual(H._identity(Key()), "anonymous")

    def test_signature_verifies_at_the_orchestrator(self):
        os.environ["GX_IDENTITY_HMAC_KEY"] = KEY
        self.addCleanup(os.environ.pop, "GX_IDENTITY_HMAC_KEY", None)
        hdrs = H._signed_identity("open-webui")
        self.assertEqual(identity_from_headers(hdrs, KEY), "open-webui")
        self.assertEqual(identity_from_headers(hdrs, "x" * 32), "")

    def test_pre_call_forwards_identity_and_token_but_logs_no_token(self):
        os.environ["GX_IDENTITY_HMAC_KEY"] = KEY
        self.addCleanup(os.environ.pop, "GX_IDENTITY_HMAC_KEY", None)
        data = {"model": "gx-fast", "messages": [{"role": "user", "content": "hi"}],
                "extra_headers": {"X-GX-Identity": "odin", "Authorization": "Bearer stolen", "X-Evil": "1"},
                "headers": {"X-GX-Priority": "health"},
                "metadata": {"headers": {"X-GX-Project": "p", "X-GX-Switch-Validation": "secret-token",
                                         "X-GX-Identity": "odin"}}}
        out = asyncio.run(H.GxBudgetHook().async_pre_call_hook(Key(key_alias="hermes"), None, data, "acompletion"))
        fwd = out["extra_headers"]
        self.assertEqual(out["headers"], fwd)
        self.assertNotIn("forwarded_headers", out)
        self.assertEqual(fwd["X-GX-Identity"], "hermes")          # the client's "odin" claim is gone
        self.assertEqual(fwd["X-GX-Switch-Validation"], "secret-token")
        self.assertNotIn("X-GX-Switch-Validation", out["metadata"]["gx_attribution"])
        self.assertEqual(identity_from_headers(fwd, KEY), "hermes")
        self.assertNotIn("Authorization", fwd)
        self.assertNotIn("X-Evil", fwd)
        self.assertEqual(set(fwd) - set(H.ATTRIB_HEADERS),
                         {"X-GX-Identity", "X-GX-Identity-Ts", "X-GX-Identity-Nonce", "X-GX-Identity-Sig"})

    def test_hook_failure_drops_every_client_header_field(self):
        # Even a client dict that names X-GX-Identity exactly (the old check)
        # and carries Authorization must not survive a hook error.
        data = {"model": "gx-fast", "messages": [],
                "extra_headers": {"X-GX-Identity": "odin", "Authorization": "Bearer stolen"},
                "headers": {"X-GX-Identity": "odin"}}
        orig = H._capture_attribution
        H._capture_attribution = lambda d: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = asyncio.run(H.GxBudgetHook().async_pre_call_hook(Key(key_alias="hermes"), None, data, "acompletion"))
        finally:
            H._capture_attribution = orig
        self.assertNotIn("extra_headers", out)
        self.assertNotIn("headers", out)

    def test_non_ascii_or_control_values_are_dropped(self):
        data = {"metadata": {"headers": {"X-GX-Project": "projekt-ü", "X-GX-Agent": "ok-agent",
                                         "X-GX-Task": "a\r\nInjected: 1", "X-GX-Intent": "x" * 500}}}
        out = H._capture_attribution(data)
        self.assertNotIn("X-GX-Project", out)
        self.assertNotIn("X-GX-Task", out)
        self.assertEqual(out["X-GX-Agent"], "ok-agent")
        self.assertEqual(len(out["X-GX-Intent"]), 120)
        for v in out.values():
            v.encode("ascii")


if __name__ == "__main__":
    unittest.main()
