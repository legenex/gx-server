"""Relay behaviour (D-039, V4.1): fail fast, correct once, never resend
unchanged.

The 2026-09-17 hang: for a STREAMED request the orchestrator sent `200` and
chunked headers before contacting the gateway, then wrote the gateway's 400
into the open stream. The client saw a stalled stream, timed out after about
five minutes and RETRIED the oversized request. The rule that fell out of
that incident still holds, now against the Mia kit's vLLM API:

* the upstream connection is opened BEFORE anything is written to the
  client, so a refusal is a real HTTP error;
* a context refusal carrying the engine's exact input count is corrected
  ONCE and retried immediately with a smaller output budget;
* nothing is ever resent unchanged;
* `x-gx-queue-wait-ms` is stamped only when the request was queued, per the
  gateway budget hook's contract (absent == never queued).
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator import budget as B  # noqa: E402
from tests.server_harness import (  # noqa: E402
    OrchestratorHarness,
    completion_body,
)


def _payload(**extra):
    p = {"model": "gx-max", "messages": [{"role": "user", "content": "17*19="}]}
    p.update(extra)
    return p


class TestNonStreamingRelay(OrchestratorHarness):
    def test_complete_json_is_returned_verbatim_with_usage_headers(self):
        status, headers, body = self.chat(_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "323")
        self.assertEqual(body["model"], "DeepSeek-v4.1-Flash-EXL3")
        self.assertEqual(headers["X-GX-Routed-To"], "gx-max")
        self.assertEqual(headers["X-GX-Profile"], "balanced")
        self.assertIn("X-GX-Output-Tokens", headers)
        self.assertNotIn("x-gx-queue-wait-ms", headers)  # never queued
        # in-flight accounting is balanced
        self.assertEqual(self.lifecycle.in_flight, 0)

    def test_model_id_and_reasoning_kwargs_are_injected_upstream(self):
        self.chat(_payload())
        sent = self.upstream.last_request()
        self.assertEqual(sent["model"], "DeepSeek-v4.1-Flash-EXL3")
        # balanced's reasoning_default is medium -> reasoning_effort 62
        self.assertEqual(sent["chat_template_kwargs"], {"reasoning_effort": 62})

    def test_requested_output_budget_is_forwarded_unchanged_when_it_fits(self):
        self.chat(_payload(max_tokens=1000))
        self.assertEqual(self.upstream.last_request()["max_tokens"], 1000)

    def test_upstream_500_is_relayed_with_status_and_body(self):
        self.upstream.script.append({"status": 503, "json": {"error": {
            "message": "vLLM is warming", "type": "server_error", "code": "overloaded"}}})
        status, headers, body = self.chat(_payload())
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["message"], "vLLM is warming")
        self.assertNotIn("x-should-retry", headers)  # server errors stay retryable
        # the scheduler recorded the failure
        self.assertEqual(self.scheduler.history(limit=1)[0]["state"], "error")

    def test_upstream_400_is_relayed_with_no_retry_header(self):
        self.upstream.script.append({"status": 400, "json": {"error": {
            "message": "bad parameter", "type": "invalid_request_error"}}})
        status, headers, body = self.chat(_payload())
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["message"], "bad parameter")
        self.assertTrue(body["error"]["retryable"] is False or
                        headers.get("x-should-retry") == "false")

    def test_dead_upstream_is_a_502_not_a_hang(self):
        import dataclasses
        dead = dataclasses.replace(self.cfg, gxmax_base="http://127.0.0.1:1/v1")
        self._handler_cls.cfg = dead
        try:
            status, headers, body = self.chat(_payload(), timeout=20)
        finally:
            self._handler_cls.cfg = self.cfg
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "bad_gateway")


class TestContextCorrection(OrchestratorHarness):
    """One correction from the engine's exact count, never a second."""

    REFUSAL = (
        "Error: this model's maximum context length is 65536 tokens. "
        "However, you requested 32000 output tokens and your prompt "
        "contains at least 33000 input tokens"
    )

    def test_exact_count_is_corrected_once_and_succeeds(self):
        # A realistic vLLM refusal: 50 000 input + the requested 32 000
        # output exceeds the engine's 65 536 window (the profile's advertised
        # window is far larger; the ENGINE's count is authoritative).
        refusal = (
            "Error: this model's maximum context length is 65536 tokens. "
            "However, you requested 32000 output tokens and your prompt "
            "contains at least 50000 input tokens"
        )
        self.upstream.script.append({"status": 400, "body": json.dumps({"error": {
            "message": refusal, "type": "invalid_request_error"}})})
        self.upstream.script.append({"status": 200, "json": completion_body()})
        status, headers, body = self.chat(_payload(max_tokens=32_000))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream.requests), 2)  # exactly one retry
        first, second = self.upstream.requests
        self.assertEqual(first["max_tokens"], 32_000)
        # corrected down, bounded by the engine's own arithmetic
        self.assertLess(second["max_tokens"], 32_000)
        self.assertGreaterEqual(second["max_tokens"], B.MIN_RETRY_OUTPUT)
        self.assertEqual(headers.get("X-GX-Retry-Reason"), "engine_context_count")

    def test_uncorrectable_refusal_is_terminal_with_budget_detail(self):
        # input 8000 in an 8192 window: nothing can be retried
        refusal = (
            "Error: this model's maximum context length is 8192 tokens. "
            "However, you requested 4000 output tokens and your prompt "
            "contains at least 8000 input tokens"
        )
        self.upstream.script.append({"status": 400, "body": json.dumps({"error": {
            "message": refusal, "type": "invalid_request_error"}})})
        status, headers, body = self.chat(_payload(max_tokens=4_000))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "context_length_exceeded")
        self.assertFalse(body["error"]["retryable"])
        detail = body["error"]["gx_budget"]
        self.assertEqual(detail["engine_input_tokens"], 8_000)
        self.assertEqual(detail["engine_context_limit"], 8_192)
        self.assertEqual(detail["attempts"], 1)
        self.assertIn("Shorten or compact", body["error"]["message"])
        # nothing was resent unchanged
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(headers.get("x-should-retry"), "false")


class TestStreamingRelay(OrchestratorHarness):
    def test_sse_is_passed_through_chunk_by_chunk(self):
        self.upstream.script.append({"stream": ["three", " ", "hundred"]})
        import urllib.request
        data = json.dumps(_payload(stream=True)).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer test-orchestrator-key")
        with urllib.request.urlopen(req, timeout=15) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            self.assertEqual(resp.headers.get("X-GX-Routed-To"), "gx-max")
            stream = resp.read().decode()
        self.assertIn("three", stream)
        self.assertIn("[DONE]", stream)
        self.assertEqual(self.lifecycle.in_flight, 0)

    def test_streamed_refusal_is_a_real_http_error_not_a_stalled_200(self):
        # THE D-039 incident shape: stream=true AND the upstream refuses.
        self.upstream.script.append({"status": 400, "body": json.dumps({"error": {
            "message": "maximum context length is 8192 tokens",
            "type": "invalid_request_error"}})})
        status, headers, body = self.chat(_payload(stream=True))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "context_length_exceeded")
        # nothing was ever streamed to the client
        self.assertEqual(headers.get("x-should-retry"), "false")


class TestQueueWaitHeader(OrchestratorHarness):
    def test_queued_request_carries_the_header_direct_does_not(self):
        # capacity 2: hold both slots with slow requests, so the third queues.
        self.upstream.script.append({"delay": 0.8, "json": completion_body()})
        self.upstream.script.append({"delay": 0.8, "json": completion_body()})
        results = {}

        def fire(tag):
            results[tag] = self.chat(_payload(), timeout=30)

        t1 = threading.Thread(target=fire, args=("a",))
        t2 = threading.Thread(target=fire, args=("b",))
        t3 = threading.Thread(target=fire, args=("c",))
        for t in (t1, t2):
            t.start()
        time.sleep(0.15)  # both slots taken
        t3.start()
        for t in (t1, t2, t3):
            t.join(timeout=30)
        for tag in "abc":
            self.assertEqual(results[tag][0], 200, results[tag])
        waits = {tag: results[tag][1].get("x-gx-queue-wait-ms") for tag in "abc"}
        self.assertIsNotNone(waits["c"])
        self.assertGreater(float(waits["c"]), 0.0)
        self.assertIsNone(waits["a"])
        self.assertIsNone(waits["b"])
        # the scheduler history shows the third one was queued first
        self.assertEqual(self.scheduler.status()["active"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
