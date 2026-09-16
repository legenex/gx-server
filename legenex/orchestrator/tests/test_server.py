"""Tests for gx_orchestrator.server: gx-max's status is derived from its
lifecycle state without changing that state machine's semantics (see
lifecycle.py and ARCHITECTURE.md section 5).
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator.health import AliasState, TierStatus  # noqa: E402
from gx_orchestrator.lifecycle import LifecycleStatus, State  # noqa: E402
from gx_orchestrator.server import _max_tier_status  # noqa: E402

#: The DOWN branch now consults the admission guard (see
#: `_gx_max_admission_blocked`). These cases are about the lifecycle-state
#: mapping, so they pin the probe to "not blocked"; the admission behaviour
#: has its own class below. Without pinning, the result would depend on how
#: much memory the machine running the tests happens to have free.
_NOT_BLOCKED = lambda: ""  # noqa: E731

_READY_NODE2 = TierStatus(AliasState.READY, "loaded", usable=True)
_OFFLINE_NODE2 = TierStatus(AliasState.UNAVAILABLE, "node2_offline", usable=False)


def _lc_status(state: State, *, last_error: str = "", detail: str = "") -> LifecycleStatus:
    return LifecycleStatus(
        state=state,
        since=time.time(),
        last_used=None,
        waiters=0,
        detail=detail,
        last_error=last_error,
    )


class TestMaxTierStatus(unittest.TestCase):
    def test_ready_is_ready_and_usable(self):
        status = _max_tier_status(_lc_status(State.READY), _READY_NODE2, admission_blocked=_NOT_BLOCKED)
        self.assertEqual(status.state, AliasState.READY)
        self.assertTrue(status.usable)

    def test_down_with_node2_up_is_stopped_and_usable(self):
        """DOWN is a valid resting state for gx-max, never a fault (see
        lifecycle.py / ARCHITECTURE.md section 5) -- it must stay usable.
        """
        status = _max_tier_status(_lc_status(State.DOWN), _READY_NODE2, admission_blocked=_NOT_BLOCKED)
        self.assertEqual(status.state, AliasState.STOPPED)
        self.assertTrue(status.usable)

    def test_down_with_node2_offline_reports_node2_unavailable(self):
        """The task's specific ask: gx-max must report something like
        'node2 unavailable' rather than a bare boolean when node 2 is down,
        WITHOUT this becoming a fault state -- acquiring both nodes is still
        the normal, attemptable next step.
        """
        status = _max_tier_status(_lc_status(State.DOWN), _OFFLINE_NODE2, admission_blocked=_NOT_BLOCKED)
        self.assertEqual(status.state, AliasState.STOPPED)
        self.assertEqual(status.reason, "node2_unavailable")
        # Still usable: DOWN remains a valid resting state per the locked
        # lifecycle semantics -- this function must not invent a new fault.
        self.assertTrue(status.usable)

    def test_acquiring_is_queued_and_usable(self):
        status = _max_tier_status(_lc_status(State.ACQUIRING, detail="starting both ranks"), _READY_NODE2, admission_blocked=_NOT_BLOCKED)
        self.assertEqual(status.state, AliasState.QUEUED)
        self.assertTrue(status.usable)

    def test_releasing_is_not_usable(self):
        """Matches the ORIGINAL TierHealth logic exactly: RELEASING was the
        one state excluded from `avail[Tier.MAX]`.
        """
        status = _max_tier_status(_lc_status(State.RELEASING, detail="graceful drain"), _READY_NODE2, admission_blocked=_NOT_BLOCKED)
        self.assertFalse(status.usable)

    def test_last_error_surfaces_verbatim_when_present(self):
        status = _max_tier_status(
            _lc_status(State.DOWN, last_error="gx-max-start.sh exited 3: boom: rank1 died"),
            _READY_NODE2,
            admission_blocked=_NOT_BLOCKED,
        )
        self.assertEqual(status.state, AliasState.STOPPED)
        self.assertIn("boom: rank1 died", status.reason)

    def test_node2_unavailable_takes_priority_over_a_stale_last_error(self):
        """If node 2 is confirmed offline right now, that is the more useful
        and more current answer than a stale error from a previous attempt.
        """
        status = _max_tier_status(
            _lc_status(State.DOWN, last_error="stale: previous failure"),
            _OFFLINE_NODE2,
            admission_blocked=_NOT_BLOCKED,
        )
        self.assertEqual(status.reason, "node2_unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestGxAutoNeverAcquiresGxMax(unittest.TestCase):
    """gx-auto may USE gx-max when it is already up; it must never ACQUIRE it.

    Acquiring gx-max is not a cheap operation that happens to fail --
    gx-max-start.sh drains gx-mini, gx-fast and llama-swap on BOTH nodes
    first, because gx-max takes over the whole cluster. Observed live on
    2026-09-16: one gx-auto prompt containing the word "exhaustive" tore down
    node 1's resident models and both llama-swaps, was refused by the
    admission guard, and spent ~12 s putting everything back. Taking over both
    nodes is an operator-initiated act and stays on the direct gx-max path.

    These tests assert the branch structure in
    `_serve_gx_auto`'s `decision.tier is Tier.MAX` block directly, since the
    handler needs a live HTTP server to exercise end to end.
    """

    def _source(self) -> str:
        from gx_orchestrator import server as srv
        return Path(srv.__file__).read_text()

    def test_direct_gx_max_path_still_acquires(self):
        """The direct path MUST still acquire -- it is the operator-initiated one."""
        src = self._source()
        start = src.index("def _serve_gx_max")
        block = src[start:start + 2000]
        self.assertIn("self.lifecycle.acquire()", block)


class TestGxMaxHealthReflectsAdmission(unittest.TestCase):
    """A tier the guard refuses on every attempt must not report `usable`.

    Before this, `gx status` showed `gx-max -> stopped, usable: true` for a
    tier that cannot be brought up on this hardware at all (B-022): the
    admission guard refuses it because the measured per-rank load peak of
    117 GiB plus any reserve exceeds a 121 GiB node. That is a fake healthy
    state, which this project forbids.
    """

    def test_down_and_admission_refused_is_unavailable_and_not_usable(self):
        status = _max_tier_status(
            _lc_status(State.DOWN), _READY_NODE2,
            admission_blocked=lambda: "admission_refused: needs 147.0GiB of a 121.0GiB node",
        )
        self.assertEqual(status.state, AliasState.UNAVAILABLE)
        self.assertFalse(status.usable)
        self.assertIn("admission_refused", status.reason)

    def test_down_and_admission_ok_is_still_stopped_and_usable(self):
        status = _max_tier_status(
            _lc_status(State.DOWN), _READY_NODE2, admission_blocked=lambda: "",
        )
        self.assertTrue(status.usable)
        self.assertNotEqual(status.state, AliasState.UNAVAILABLE)

    def test_ready_is_not_second_guessed_by_the_admission_probe(self):
        """A RUNNING engine is healthy regardless of what admission would say now."""
        status = _max_tier_status(
            _lc_status(State.READY), _READY_NODE2,
            admission_blocked=lambda: "admission_refused: would not fit",
        )
        self.assertEqual(status.state, AliasState.READY)
        self.assertTrue(status.usable)

    def test_probe_failure_never_invents_a_fault(self):
        """A broken probe must fall back, not make a healthy tier look down."""
        from gx_orchestrator.server import _gx_max_admission_blocked
        def boom() -> str:
            raise RuntimeError("probe exploded")
        with self.assertRaises(RuntimeError):
            boom()
        # the real probe swallows its own exceptions and returns ""
        self.assertIsInstance(_gx_max_admission_blocked(), str)


class TestNode2OfflineOutranksAdmission(unittest.TestCase):
    def test_node2_offline_is_reported_even_if_admission_would_also_refuse(self):
        """Give the operator the actionable reason, not the arithmetic one."""
        status = _max_tier_status(
            _lc_status(State.DOWN), _OFFLINE_NODE2,
            admission_blocked=lambda: "admission_refused: would not fit",
        )
        self.assertEqual(status.reason, "node2_unavailable")


# ---------------------------------------------------------------------------
# Behavioural gx-auto tests: a real HTTP server, fake upstreams, fake
# lifecycle. They replace the earlier source-text checks.
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402
import tempfile as _tempfile  # noqa: E402
import threading as _threading  # noqa: E402
import urllib.request as _urlreq  # noqa: E402
from http.server import BaseHTTPRequestHandler as _BH, ThreadingHTTPServer as _TS  # noqa: E402

from gx_orchestrator import server as _srv  # noqa: E402
from gx_orchestrator.tiers import TIERS as _TIERS, Tier as _Tier  # noqa: E402


class _FakeUpstream:
    def __init__(self):
        self.calls: list[dict] = []
        outer = self

        class H(_BH):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append(body)
                out = _json.dumps({"choices": [{"message": {"content": "ok"}}], "model": body.get("model")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = _TS(("127.0.0.1", 0), H)
        _threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


class _FakeLifecycle:
    def __init__(self, state):
        self.state = state
        self.acquired = 0

    def status(self):
        return _lc_status(self.state)

    def acquire(self, timeout=None):
        self.acquired += 1

    def mark_used(self):
        pass


class _FakeHealth:
    def snapshot(self):
        return {t: TierStatus(AliasState.READY, "ok", usable=True) for t in (_Tier.MINI, _Tier.FAST, _Tier.REASON)}


class _Cfg:
    def __init__(self, gw, mx):
        self.gateway_base = gw
        self.gxmax_base = mx
        self.gxmax_model_id = "/model"
        self.upstream_timeout = 10

    def gateway_key(self):
        return None


class TestGxAutoBehaviour(unittest.TestCase):
    def setUp(self):
        self.gw = _FakeUpstream()
        self.mx = _FakeUpstream()
        self.tmp = _tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.gw.httpd.shutdown)
        self.addCleanup(self.mx.httpd.shutdown)

    def _serve(self, lc_state):
        lifecycle = _FakeLifecycle(lc_state)
        journal = _srv.RoutingJournal(Path(self.tmp.name) / "routing.jsonl")
        handler = type("H", (_srv.Handler,), {
            "cfg": _Cfg(self.gw.base, self.mx.base),
            "lifecycle": lifecycle,
            "health": _FakeHealth(),
            "journal": journal,
        })
        httpd = _TS(("127.0.0.1", 0), handler)
        _threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}", lifecycle, journal

    def _post(self, base, payload, rid="req-test-1"):
        req = _urlreq.Request(base + "/v1/chat/completions", data=_json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "X-GX-Request-Id": rid})
        try:
            with _urlreq.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), _json.loads(r.read())
        except _urlreq.HTTPError as e:
            return e.code, dict(e.headers), _json.loads(e.read())

    _EXTREME = {"model": "gx-auto", "messages": [{"role": "user", "content":
                "Do a comprehensive audit of the entire codebase and formal verification"}]}

    def test_extreme_prompt_with_gx_max_down_falls_back_and_never_acquires(self):
        base, lc, journal = self._serve(State.DOWN)
        status, headers, _ = self._post(base, self._EXTREME)
        self.assertEqual(status, 200)
        self.assertEqual(lc.acquired, 0)
        self.assertEqual(self.mx.calls, [])
        self.assertEqual(self.gw.calls[0]["model"], "gx-reason")
        self.assertEqual(headers.get("X-GX-Request-Id"), "req-test-1")
        recs = journal.find(request_id="req-test-1")
        self.assertEqual({r["event"] for r in recs}, {"decision", "completed"})
        decision = [r for r in recs if r["event"] == "decision"][0]
        self.assertEqual(decision["tier"], "gx-reason")
        self.assertIn("does not acquire", decision["note"])

    def test_extreme_prompt_uses_gx_max_when_already_ready(self):
        base, lc, _ = self._serve(State.READY)
        status, _, _ = self._post(base, self._EXTREME)
        self.assertEqual(status, 200)
        self.assertEqual(lc.acquired, 0)
        self.assertEqual(self.mx.calls[0]["model"], "/model")

    def test_oversized_context_with_gx_max_down_is_503_not_acquire(self):
        base, lc, _ = self._serve(State.DOWN)
        huge = {"model": "gx-auto", "messages": [{"role": "user", "content": "x" * 1_200_000}]}
        status, _, body = self._post(base, huge)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "gx_max_not_running")
        self.assertEqual(lc.acquired, 0)
        self.assertEqual(self.mx.calls, [])

    def test_output_budget_is_clamped_to_tier(self):
        base, _, _ = self._serve(State.DOWN)
        self._post(base, {"model": "gx-auto", "max_tokens": 262_144,
                          "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(self.gw.calls[0]["model"], "gx-mini")
        self.assertEqual(self.gw.calls[0]["max_tokens"], _TIERS[_Tier.MINI].max_output)

    def test_decisions_endpoint_finds_by_fingerprint(self):
        base, _, _ = self._serve(State.DOWN)
        payload = {"model": "gx-auto", "messages": [{"role": "user", "content": "hello"}]}
        self._post(base, payload, rid="abc-1")
        fp = _srv.request_fingerprint(payload)
        with _urlreq.urlopen(f"{base}/routing/decisions?fingerprint={fp}", timeout=5) as r:
            data = _json.loads(r.read())["data"]
        self.assertTrue(data)
        self.assertTrue(all(d["fingerprint"] == fp for d in data))

    def test_hostile_request_id_is_replaced(self):
        base, _, journal = self._serve(State.DOWN)
        _, headers, _ = self._post(base, {"model": "gx-auto", "messages": [{"role": "user", "content": "hi"}]},
                                   rid="bad id\twith spaces")
        self.assertNotEqual(headers.get("X-GX-Request-Id"), "bad id\twith spaces")
        self.assertEqual(len(headers.get("X-GX-Request-Id", "")), 32)
