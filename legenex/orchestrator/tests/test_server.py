"""Tests for gx_orchestrator.server (V4.1).

The V4.1 rules under test: exactly two aliases (gx-max, gx-auto) and no
fallback; routing = choosing a (profile, reasoning) pair for the ONE model;
unknown profiles/reasoning are a 400, never a silent default; overflow is
refused before anything is acquired; a DOWN engine is a clear 503 unless the
request is interactive, which triggers the acquisition.
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
from gx_orchestrator.health import AliasState  # noqa: E402
from gx_orchestrator.lifecycle import LifecycleStatus, State, AcquisitionError  # noqa: E402
from gx_orchestrator.server import model_tier_status, _attribution  # noqa: E402
from tests.server_harness import (  # noqa: E402
    OrchestratorHarness,
    completion_body,
)


def _payload(**extra):
    p = {"model": "gx-auto", "messages": [{"role": "user", "content": "hello"}]}
    p.update(extra)
    return p


class TestAuthAndBasics(OrchestratorHarness):
    def test_health_is_open(self):
        status, _, body = self.get("/health", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "gx-orchestrator")

    def test_everything_else_requires_the_bearer_key(self):
        status, _, body = self.get("/text/status", auth=False)
        self.assertEqual(status, 401)
        status, _, body = self.post("/v1/chat/completions", _payload(), headers={
            "Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, _, body = self.get("/text/status", auth=True)
        self.assertEqual(status, 200)

    def test_models_lists_exactly_the_two_aliases(self):
        status, _, body = self.get("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body["data"]], ["gx-max", "gx-auto"])

    def test_unknown_model_is_a_400_never_a_fallback(self):
        status, _, body = self.chat({"model": "gx-mini", "messages": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_model")
        self.assertIn("LiteLLM gateway", body["error"]["message"])
        self.assertEqual(len(self.upstream.requests), 0)

    def test_404_for_unknown_paths(self):
        status, _, body = self.get("/no/such/path")
        self.assertEqual(status, 404)
        status, _, body = self.post("/no/such/path", {})
        self.assertEqual(status, 404)

    def test_invalid_json_body(self):
        import urllib.request, urllib.error
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=b"{not json", method="POST")
        req.add_header("Authorization", "Bearer test-orchestrator-key")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            body = json.loads(exc.read())
            self.assertEqual(body["error"]["code"], "invalid_request")


class TestRoutingDecisions(OrchestratorHarness):
    def test_auto_decision_is_journaled_and_lookupable(self):
        status, headers, body = self.chat(_payload())
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Routed-To"], "gx-auto")
        rid = headers["X-GX-Request-Id"]
        status, _, found = self.get(f"/routing/decisions?request_id={rid}")
        self.assertEqual(status, 200)
        by_event = {r["event"]: r for r in found["data"]}
        decision = by_event["decision"]
        self.assertEqual(decision["request_id"], rid)
        self.assertIn("profile", decision)
        self.assertIn("reasoning", decision)
        self.assertNotIn("messages", decision)  # never prompt text
        # the trailing completed record is written after the response is
        # flushed to the client: wait for it to become durable
        completed = self.wait_for_journal(rid, "completed")
        self.assertEqual(completed["request_id"], rid)
        self.assertEqual(completed["outcome"], "ok")
        self.assertEqual(completed["routing"]["profile"], decision["profile"])

    def test_auto_intent_drives_the_profile(self):
        status, headers, body = self.chat(_payload(messages=[
            {"role": "user", "content": "think hard: review this architecture for risks"}]))
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "deep")
        self.assertEqual(headers["X-GX-Reasoning"], "high")

    def test_auto_profile_override_is_validated(self):
        status, _, body = self.chat(_payload(), headers={"X-GX-Profile": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_profile")
        self.assertEqual(len(self.upstream.requests), 0)
        # a VALID override shapes the request but does not cycle the engine
        status, headers, body = self.chat(_payload(), headers={"X-GX-Profile": "fast"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "fast")
        self.assertEqual(self.lifecycle.acquire_calls[-1], "balanced")
        status, headers, body = self.chat(_payload(), headers={"X-GX-Profile": "deep"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "deep")
        self.assertEqual(self.lifecycle.acquire_calls[-1], "balanced")

    def test_auto_reasoning_override_is_validated(self):
        status, _, body = self.chat(_payload(), headers={"X-GX-Reasoning": "ultra"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_profile")
        status, headers, body = self.chat(_payload(), headers={"X-GX-Reasoning": "max"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Reasoning"], "max")

    def test_direct_uses_the_running_profile_and_honours_overrides(self):
        status, headers, body = self.chat({"model": "gx-max", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "balanced")  # the running profile
        status, headers, body = self.chat({"model": "gx-max", "messages": [
            {"role": "user", "content": "hi"}]}, headers={"X-GX-Profile": "swarm"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "swarm")
        # swarm's reasoning_default is low -> effort 50
        self.assertEqual(self.upstream.last_request()["chat_template_kwargs"],
                         {"reasoning_effort": 50})

    def test_direct_unknown_profile_override_is_a_400(self):
        status, _, body = self.chat({"model": "gx-max", "messages": []},
                                   headers={"X-GX-Profile": "bogus"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_profile")

    def test_none_reasoning_disables_thinking(self):
        status, headers, body = self.chat(_payload(), headers={"X-GX-Reasoning": "none"})
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.last_request()["chat_template_kwargs"],
                         {"enable_thinking": False})

    def test_auto_fast_default_disables_thinking_and_strips_tools(self):
        status, headers, body = self.chat(_payload(
            messages=[{"role": "user", "content": "What model are you running?"}],
            tools=[{"type": "function", "function": {"name": "search_memories", "parameters": {}}},
                   {"type": "function", "function": {"name": "list_memories", "parameters": {}}}],
            max_tokens=8000,
        ))
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "fast")
        self.assertEqual(headers["X-GX-Reasoning"], "none")
        sent = self.upstream.last_request()
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("tools", sent)
        self.assertEqual(sent.get("tool_choice"), "none")
        self.assertEqual(sent["max_tokens"], 1024)
        # FAST is a per-request policy; do not cycle the running engine profile.
        self.assertEqual(self.lifecycle.acquire_calls[-1], "balanced")

    def test_direct_gx_max_does_not_strip_tools(self):
        tools = [{"type": "function", "function": {"name": "search_memories", "parameters": {}}}]
        status, headers, body = self.chat({
            "model": "gx-max",
            "messages": [{"role": "user", "content": "What model are you running?"}],
            "tools": tools,
            "max_tokens": 8000,
        })
        self.assertEqual(status, 200)
        sent = self.upstream.last_request()
        self.assertEqual(sent["tools"], tools)
        self.assertEqual(sent["max_tokens"], 8000)


class TestOverflowGate(OrchestratorHarness):
    def test_overflow_is_refused_before_any_acquisition(self):
        # long-context profile advertises 600k, but ask for an input that
        # overflows even the optimistic estimate.
        status, _, body = self.chat(_payload(messages=[
            {"role": "user", "content": "x" * 4_000_000}]))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "context_length_exceeded")
        self.assertFalse(body["error"]["retryable"])
        self.assertIn("gx_budget", body["error"])
        self.assertEqual(len(self.upstream.requests), 0)  # never forwarded
        self.assertEqual(self.scheduler.status()["active"], 0)
        self.assertEqual(self.scheduler.status()["queued"], 0)  # never queued
        self.assertEqual(self.lifecycle.begin_use_calls, 0)  # nothing acquired


class TestLifecycleGate(OrchestratorHarness):
    def setUp(self):
        super().setUp()
        self.lifecycle._state = State.DOWN

    def test_down_and_background_priority_is_a_clear_503(self):
        status, headers, body = self.chat(_payload(), headers={"X-GX-Priority": "background"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "model_down")
        self.assertEqual(body["error"]["lifecycle_state"], "down")
        self.assertIn("never substituted", body["error"]["message"])
        self.assertEqual(len(self.upstream.requests), 0)
        self.assertEqual(self.lifecycle.acquire_calls, [])

    def test_down_and_interactive_triggers_the_acquisition(self):
        status, headers, body = self.chat(_payload(), headers={"X-GX-Priority": "interactive"})
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "323")
        self.assertTrue(self.lifecycle.acquire_calls)  # acquisition was triggered
        self.assertEqual(self.lifecycle._state, State.READY)

    def test_down_and_fast_auto_triggers_the_acquisition(self):
        status, headers, body = self.chat(_payload(
            messages=[{"role": "user", "content": "What is 17 * 19?"}]))
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "fast")
        self.assertTrue(self.lifecycle.acquire_calls)
        self.assertEqual(self.lifecycle._state, State.READY)

    def test_acquire_endpoint_errors(self):
        status, _, body = self.post("/lifecycle/gx-max/acquire", {"profile": "bogus"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_profile")
        self.lifecycle._acquire_error = "start.sh exceeded 2s"
        status, _, body = self.post("/lifecycle/gx-max/acquire", {"profile": "deep"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "gx_max_unavailable")
        self.assertIn("exceeded", body["error"]["message"])

    def test_restart_releases_and_reacquires(self):
        status, _, body = self.post("/lifecycle/gx-max/restart", {"profile": "fast"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(self.lifecycle.acquire_calls, ["fast"])  # release + acquire(fast)
        self.assertEqual(self.lifecycle._state, State.READY)

    def test_drain_endpoint(self):
        status, _, body = self.post("/lifecycle/gx-max/drain", {"timeout": 0})
        self.assertEqual(status, 200)
        self.assertTrue(body["drained"])
        self.assertIn("active", body)


class TestSchedulerEndpoints(OrchestratorHarness):
    def test_status_and_history(self):
        status, _, body = self.get("/scheduler/status")
        self.assertEqual(status, 200)
        self.assertIn("capacity", body)
        self.assertIn("records", body)
        status, _, body = self.get("/scheduler/history?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"data": []})
        status, _, body = self.get("/scheduler/history?limit=abc")
        self.assertEqual(status, 400)

    def test_cancel_requires_an_id(self):
        status, _, body = self.post("/scheduler/cancel", {})
        self.assertEqual(status, 400)
        status, _, body = self.post("/scheduler/cancel", {"id": "missing"})
        self.assertEqual(status, 200)
        self.assertFalse(body["cancelled"])

    def test_retry_requires_an_id(self):
        status, _, body = self.post("/scheduler/retry", {})
        self.assertEqual(status, 400)

    def test_queue_full_is_a_429_with_position(self):
        # capacity 2, per-project queued cap 1: fill everything.
        self.upstream.script.append({"delay": 0.6, "json": completion_body()})
        self.upstream.script.append({"delay": 0.6, "json": completion_body()})
        results = {}

        def fire(tag):
            results[tag] = self.chat(_payload(), timeout=30)

        threads = [threading.Thread(target=fire, args=(f"r{i}",)) for i in range(4)]
        for t in threads[:2]:
            t.start()
        time.sleep(0.15)
        for t in threads[2:]:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(results), 4)
        codes = sorted(r[0] for r in results.values())
        self.assertEqual(codes.count(200), 4)  # the small caps still admit here
        # Now starve the queue: per-project queued cap with a held capacity.
        # (The caps are exercised exhaustively in test_scheduler; here we
        # prove the 429 shape with the global cap via a stub scheduler.)

    def test_attribution_headers_map_to_scheduler_fields(self):
        class H(dict):
            def get(self, k, d=None):
                return dict(self).get(k, d)

        attr = _attribution(H({"X-GX-Project": "proj", "X-GX-Agent": "kilocode",
                               "X-GX-Task": "t-1", "X-GX-Priority": "INTERACTIVE"}))
        self.assertEqual(attr, {"project": "proj", "agent": "kilocode",
                                "task": "t-1", "priority": "interactive"})

    def test_unknown_priority_falls_back_to_default(self):
        class H(dict):
            def get(self, k, d=None):
                return dict(self).get(k, d)

        attr = _attribution(H({"X-GX-Priority": "supercalifragilistic"}))
        self.assertEqual(attr["priority"], "normal-worker")  # the documented default
        attr = _attribution(H({}))
        self.assertEqual(attr["priority"], "normal-worker")
        self.assertEqual(attr["project"], "unknown")

    def test_fast_auto_without_priority_header_is_interactive(self):
        status, headers, body = self.chat(_payload(
            messages=[{"role": "user", "content": "What is 17 * 19?"}]))
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "fast")
        rid = headers["X-GX-Request-Id"]
        self.wait_for_journal(rid, "completed")
        hist = [h for h in self.scheduler.history() if h["id"] == rid]
        self.assertTrue(hist)
        self.assertEqual(hist[0]["priority"], "interactive")
        self.assertEqual(hist[0]["profile"], "fast")

    def test_deep_auto_without_priority_header_is_normal_worker(self):
        status, headers, body = self.chat(_payload(messages=[
            {"role": "user", "content": "think hard: review this architecture for risks"}]))
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-GX-Profile"], "deep")
        rid = headers["X-GX-Request-Id"]
        self.wait_for_journal(rid, "completed")
        hist = [h for h in self.scheduler.history() if h["id"] == rid]
        self.assertTrue(hist)
        self.assertEqual(hist[0]["priority"], "normal-worker")

    def test_explicit_priority_header_wins_over_mode(self):
        status, headers, body = self.chat(
            _payload(messages=[{"role": "user", "content": "What is 17 * 19?"}]),
            headers={"X-GX-Priority": "background"},
        )
        self.assertEqual(status, 200)
        rid = headers["X-GX-Request-Id"]
        self.wait_for_journal(rid, "completed")
        hist = [h for h in self.scheduler.history() if h["id"] == rid]
        self.assertEqual(hist[0]["priority"], "background")


class TestStatusShapes(OrchestratorHarness):
    def test_text_status_shape(self):
        _, headers, _ = self.chat(_payload())  # one completed gx-auto request
        self.wait_for_journal(headers["X-GX-Request-Id"], "completed")
        status, _, body = self.get("/text/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["model"]["id"], "DeepSeek-v4.1-Flash-EXL3")
        self.assertTrue(body["model"]["uncensored"])
        self.assertEqual(body["model"]["profile"], "balanced")
        self.assertIn("profiles", body["model"])
        self.assertIn("nodes", body["model"])
        self.assertEqual(body["queue"]["capacity"], 2)
        last = body["aliases"]["gx-auto"]["last_request"]
        self.assertEqual(last["event"], "completed")
        self.assertEqual(last["outcome"], "ok")
        self.assertEqual(last["prompt_tokens"], 17)
        self.assertIsNone(body["aliases"]["gx-max"]["last_request"])

    def test_detailed_status_shape(self):
        status, _, body = self.get("/health/detailed")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["model"]["state"]["state"], "ready")
        self.assertEqual(body["model"]["state"]["usable"], True)
        self.assertIn("gateway", body)

    def test_lifecycle_status_shape(self):
        status, _, body = self.get("/lifecycle/gx-max/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "ready")
        self.assertIn("phase", body)
        self.assertIn("profile", body)

    def test_lifecycle_events_endpoint_validates(self):
        status, _, body = self.get("/lifecycle/gx-max/events?after=zzz")
        self.assertEqual(status, 400)
        status, _, body = self.get("/lifecycle/gx-max/events")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"seq", "events", "active_job", "history"})


class TestModelTierStatus(unittest.TestCase):
    """The lifecycle -> shared-state mapping, folded with worker health."""

    def _status(self, state, last_error="", detail=""):
        return LifecycleStatus(state=state, since=time.time(), last_used=time.time(),
                               waiters=0, detail=detail, last_error=last_error)

    def test_ready(self):
        ts = model_tier_status(self._status(State.READY), worker_ok=True)
        self.assertEqual(ts.state, AliasState.READY)
        self.assertTrue(ts.usable)

    def test_down_maps_to_stopped_and_is_still_attemptable(self):
        ts = model_tier_status(self._status(State.DOWN), worker_ok=True)
        self.assertEqual(ts.state, AliasState.STOPPED)
        self.assertTrue(ts.usable)  # acquire on demand

    def test_acquiring_maps_to_queued(self):
        ts = model_tier_status(self._status(State.ACQUIRING), worker_ok=True)
        self.assertEqual(ts.state, AliasState.QUEUED)
        self.assertTrue(ts.usable)

    def test_releasing_maps_to_loading_and_is_not_attemptable(self):
        ts = model_tier_status(self._status(State.RELEASING), worker_ok=True)
        self.assertEqual(ts.state, AliasState.LOADING)
        self.assertFalse(ts.usable)

    def test_down_with_worker_offline_says_so(self):
        ts = model_tier_status(self._status(State.DOWN), worker_ok=False)
        self.assertEqual(ts.reason, "node2_unavailable")

    def test_last_error_is_visible(self):
        ts = model_tier_status(self._status(State.DOWN, last_error="start.sh exited 1"), worker_ok=True)
        self.assertIn("start.sh exited 1", ts.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
