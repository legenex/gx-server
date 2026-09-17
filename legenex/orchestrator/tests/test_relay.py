"""Relay behaviour (D-039): fail fast, correct once, never resend unchanged.

The 2026-09-17 hang: for a STREAMED request the orchestrator sent `200` and
chunked headers before contacting the gateway, then wrote the gateway's 400
into the open stream. The client saw a stalled stream, timed out after about
five minutes and resent the identical request. These tests drive the real
Handler against a scripted fake upstream.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator import server as srv  # noqa: E402
from gx_orchestrator.health import AliasState, TierStatus  # noqa: E402
from gx_orchestrator.lifecycle import LifecycleStatus, State  # noqa: E402
from gx_orchestrator.tiers import Tier  # noqa: E402
from kilo_fixtures import claude_code_continuation  # noqa: E402

CONTEXT_400 = {
    "error": {
        "message": (
            "litellm.ContextWindowExceededError: ContextWindowExceededError: OpenAIException - This "
            "model's maximum context length is 131072 tokens. However, you requested {out} output tokens "
            "and your prompt contains at least {inp} input tokens, for a total of at least {tot} tokens."
        ),
        "type": None,
        "param": None,
        "code": "400",
    }
}


class ScriptedUpstream:
    """Answers each POST with the next scripted reply."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append(body)
                kind, value = outer.replies.pop(0) if outer.replies else ("ok", None)
                if kind == "error":
                    status, payload = value
                    data = json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for piece in ("Hel", "lo"):
                        ev = {"choices": [{"delta": {"content": piece}}]}
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.02)
                    usage = {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}}
                    self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
                    return
                data = json.dumps({"choices": [{"message": {"content": "ok"}}],
                                   "usage": {"prompt_tokens": 5, "completion_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


class FakeLifecycle:
    def __init__(self, state):
        self.state = state
        self.acquired = 0
        self.in_use = 0
        self.max_in_use = 0

    def status(self):
        return LifecycleStatus(state=self.state, since=time.time(), last_used=None, waiters=0)

    def acquire(self, timeout=None):
        self.acquired += 1
        self.state = State.READY

    def mark_used(self):
        pass

    def begin_use(self):
        self.in_use += 1
        self.max_in_use = max(self.max_in_use, self.in_use)

    def end_use(self):
        self.in_use -= 1


class FakeHealth:
    def snapshot(self):
        return {t: TierStatus(AliasState.READY, "ok", usable=True) for t in (Tier.MINI, Tier.FAST, Tier.REASON)}


class Cfg:
    def __init__(self, gw, mx):
        self.gateway_base = gw
        self.gxmax_base = mx
        self.gxmax_model_id = "/model"
        self.upstream_timeout = 10

    def gateway_key(self):
        return None


class RelayCase(unittest.TestCase):
    def serve(self, gw_replies=(), mx_replies=(), state=State.DOWN):
        self.gw = ScriptedUpstream(gw_replies)
        self.mx = ScriptedUpstream(mx_replies)
        self.addCleanup(self.gw.httpd.shutdown)
        self.addCleanup(self.mx.httpd.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lc = FakeLifecycle(state)
        self.journal = srv.RoutingJournal(Path(tmp.name) / "routing.jsonl")
        self.metrics = srv.TextMetrics()
        handler = type("H", (srv.Handler,), {
            "cfg": Cfg(self.gw.base, self.mx.base), "lifecycle": self.lc,
            "health": FakeHealth(), "journal": self.journal, "metrics": self.metrics,
        })
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(self, payload, path="/v1/chat/completions", timeout=10):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "X-GX-Request-Id": "rid-1"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read(), time.monotonic() - t0
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read(), time.monotonic() - t0

    def completed(self):
        return [r for r in self.journal.find(request_id="rid-1") if r["event"] == "completed"][0]


def context_error(out, inp, limit=131072):
    body = json.loads(json.dumps(CONTEXT_400))
    body["error"]["message"] = body["error"]["message"].format(out=out, inp=inp, tot=out + inp).replace(
        "131072", str(limit))
    return ("error", (400, body))


class TestStreamingErrors(RelayCase):
    def test_streamed_upstream_400_is_a_real_400_and_fast(self):
        bad = {"error": {"message": "All non-assistant messages must contain 'content'", "code": 400}}
        self.serve(gw_replies=[("error", (400, bad))])
        status, headers, body, secs = self.post(
            {"model": "gx-auto", "stream": True, "messages": [{"role": "user", "content": "fix app.py"}]})
        self.assertEqual(status, 400)
        self.assertLess(secs, 3)
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertIn("must contain", json.loads(body)["error"]["message"])
        self.assertEqual(len(self.gw.calls), 1, "a deterministic 4xx is never retried")
        rec = self.completed()
        self.assertEqual(rec["status"], 400)
        self.assertEqual(rec["attempts"], 1)

    def test_upstream_500_is_relayed_as_retryable(self):
        self.serve(gw_replies=[("error", (500, {"error": {"message": "engine crashed", "code": 500}}))])
        status, headers, body, _ = self.post({"model": "gx-auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 500)
        self.assertNotIn("x-should-retry", {k.lower() for k in headers})
        self.assertTrue(json.loads(body)["error"]["retryable"])

    def test_successful_stream_records_ttft_and_usage(self):
        self.serve()
        status, headers, body, _ = self.post(
            {"model": "gx-auto", "stream": True, "messages": [{"role": "user", "content": "fix app.py"}]})
        self.assertEqual(status, 200)
        self.assertIn(b"Hel", body)
        self.assertEqual(headers.get("X-GX-Routed-To"), "gx-fast")
        rec = self.completed()
        self.assertEqual(rec["outcome"], "ok")
        self.assertIsNotNone(rec["ttft_ms"])
        self.assertEqual(rec["completion_tokens"], 2)
        self.assertEqual(rec["prompt_tokens"], 11)
        self.assertIsNotNone(rec["tokens_per_s"])
        self.assertEqual(self.metrics.snapshot()["gx-fast"]["outcome"], "ok")

    def test_unreachable_upstream_is_502_quickly(self):
        self.serve()
        self.gw.httpd.shutdown()
        self.gw.httpd.server_close()
        status, _, body, secs = self.post({"model": "gx-auto", "stream": True,
                                           "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 502)
        self.assertLess(secs, 5)


class TestContextCorrection(RelayCase):
    def test_observed_request_is_forwarded_to_fast_with_budget_headers(self):
        self.serve()
        payload = claude_code_continuation()
        status, headers, _, _ = self.post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.gw.calls[0]["model"], "gx-fast")
        self.assertEqual(self.gw.calls[0]["max_tokens"], 32_000)
        self.assertEqual(headers.get("X-GX-Context-Limit"), "131072")
        self.assertEqual(headers.get("X-GX-Output-Clamped"), "false")

    def test_forced_reason_is_clamped_before_sending(self):
        self.serve()
        payload = claude_code_continuation()
        payload["gx_tier"] = "reason"
        status, headers, _, _ = self.post(payload)
        self.assertEqual(status, 200)
        sent = self.gw.calls[0]
        self.assertEqual(sent["model"], "gx-reason")
        self.assertLess(sent["max_tokens"], 32_000)
        self.assertLessEqual(33_537 + sent["max_tokens"], 65_536)
        self.assertEqual(headers.get("X-GX-Output-Clamped"), "true")

    def test_engine_count_corrects_once_and_immediately(self):
        # Estimate says 32000 fits gx-fast; the engine counts more and says so.
        self.serve(gw_replies=[context_error(32_000, 110_000), ("ok", None)])
        payload = claude_code_continuation()
        status, headers, _, secs = self.post(payload)
        self.assertEqual(status, 200)
        self.assertLess(secs, 3)
        self.assertEqual(len(self.gw.calls), 2)
        self.assertEqual(self.gw.calls[1]["max_tokens"], 131_072 - 110_000 - 32)
        self.assertNotEqual(self.gw.calls[0], self.gw.calls[1], "never resend the same payload")
        self.assertEqual(headers.get("X-GX-Retry-Reason"), "engine_context_count")
        rec = self.completed()
        self.assertEqual(rec["attempts"], 2)
        self.assertEqual(rec["retry_reason"], "engine_context_count")

    def test_second_refusal_is_terminal(self):
        self.serve(gw_replies=[context_error(32_000, 110_000), context_error(21_040, 120_000)])
        status, headers, body, secs = self.post(claude_code_continuation())
        self.assertEqual(status, 400)
        self.assertLess(secs, 3)
        self.assertEqual(len(self.gw.calls), 2)
        err = json.loads(body)["error"]
        self.assertEqual(err["code"], "context_length_exceeded")
        self.assertEqual(err["gx_budget"]["attempts"], 2)
        self.assertEqual(err["gx_budget"]["engine_input_tokens"], 120_000)
        self.assertEqual(headers.get("x-should-retry"), "false")

    def test_input_that_leaves_no_room_is_terminal_without_retry(self):
        self.serve(gw_replies=[context_error(32_000, 131_000)])
        status, _, body, _ = self.post(claude_code_continuation())
        self.assertEqual(status, 400)
        self.assertEqual(len(self.gw.calls), 1)
        self.assertFalse(json.loads(body)["error"]["retryable"])

    def test_generic_context_error_without_count_is_terminal(self):
        generic = ("error", (400, {"error": {"message": "prompt is too long", "code": "context_length_exceeded"}}))
        self.serve(gw_replies=[generic])
        status, _, body, _ = self.post({"model": "gx-auto", "messages": [{"role": "user", "content": "fix a.py"}]})
        self.assertEqual(status, 400)
        self.assertEqual(len(self.gw.calls), 1)
        self.assertEqual(json.loads(body)["error"]["code"], "context_length_exceeded")

    def test_impossible_input_is_refused_before_any_upstream_call(self):
        self.serve()
        huge = {"model": "gx-auto", "stream": True,
                "messages": [{"role": "user", "content": "x" * 3_000_000}]}
        status, headers, body, secs = self.post(huge)
        self.assertEqual(status, 400)
        self.assertLess(secs, 3)
        self.assertEqual(self.gw.calls, [])
        err = json.loads(body)["error"]
        self.assertEqual(err["code"], "context_length_exceeded")
        for key in ("model", "context_limit", "estimated_input_tokens", "tool_schema_tokens",
                    "requested_output_tokens", "safe_output_tokens", "clamped", "remaining_context"):
            self.assertIn(key, err["gx_budget"])


class TestDirectGxMax(RelayCase):
    def test_overflow_never_acquires(self):
        self.serve()
        status, _, body, _ = self.post({"model": "gx-max", "messages": [{"role": "user", "content": "x" * 3_000_000}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.lc.acquired, 0)
        self.assertEqual(json.loads(body)["error"]["code"], "context_length_exceeded")

    def test_direct_request_holds_the_engine_while_in_flight(self):
        self.serve()
        status, _, _, _ = self.post({"model": "gx-max", "max_tokens": 999_999,
                                     "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(self.lc.acquired, 1)
        self.assertEqual(self.lc.max_in_use, 1)
        self.assertEqual(self.lc.in_use, 0)
        self.assertEqual(self.mx.calls[0]["max_tokens"], 65_536)
        self.assertEqual(self.mx.calls[0]["model"], "/model")


class TestTextStatus(RelayCase):
    def test_status_exposes_limits_and_last_request(self):
        self.serve()
        self.post({"model": "gx-auto", "messages": [{"role": "user", "content": "fix app.py"}]})
        with urllib.request.urlopen(self.base + "/text/status", timeout=5) as r:
            data = json.load(r)["aliases"]
        self.assertEqual(data["gx-fast"]["context_limit"], 131_072)
        self.assertEqual(data["gx-fast"]["last_request"]["outcome"], "ok")
        self.assertEqual(data["gx-auto"]["last_decision"]["tier"], "gx-fast")
        self.assertIn("summary", data["gx-auto"]["last_decision"])
        self.assertIn("lifecycle", data["gx-max"])

    def test_non_object_body_is_rejected(self):
        self.serve()
        status, headers, _, _ = self.post(["not", "an", "object"])
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("x-should-retry"), "false")


if __name__ == "__main__":
    unittest.main()
