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
        status = _max_tier_status(_lc_status(State.READY), _READY_NODE2)
        self.assertEqual(status.state, AliasState.READY)
        self.assertTrue(status.usable)

    def test_down_with_node2_up_is_stopped_and_usable(self):
        """DOWN is a valid resting state for gx-max, never a fault (see
        lifecycle.py / ARCHITECTURE.md section 5) -- it must stay usable.
        """
        status = _max_tier_status(_lc_status(State.DOWN), _READY_NODE2)
        self.assertEqual(status.state, AliasState.STOPPED)
        self.assertTrue(status.usable)

    def test_down_with_node2_offline_reports_node2_unavailable(self):
        """The task's specific ask: gx-max must report something like
        'node2 unavailable' rather than a bare boolean when node 2 is down,
        WITHOUT this becoming a fault state -- acquiring both nodes is still
        the normal, attemptable next step.
        """
        status = _max_tier_status(_lc_status(State.DOWN), _OFFLINE_NODE2)
        self.assertEqual(status.state, AliasState.STOPPED)
        self.assertEqual(status.reason, "node2_unavailable")
        # Still usable: DOWN remains a valid resting state per the locked
        # lifecycle semantics -- this function must not invent a new fault.
        self.assertTrue(status.usable)

    def test_acquiring_is_queued_and_usable(self):
        status = _max_tier_status(_lc_status(State.ACQUIRING, detail="starting both ranks"), _READY_NODE2)
        self.assertEqual(status.state, AliasState.QUEUED)
        self.assertTrue(status.usable)

    def test_releasing_is_not_usable(self):
        """Matches the ORIGINAL TierHealth logic exactly: RELEASING was the
        one state excluded from `avail[Tier.MAX]`.
        """
        status = _max_tier_status(_lc_status(State.RELEASING, detail="graceful drain"), _READY_NODE2)
        self.assertFalse(status.usable)

    def test_last_error_surfaces_verbatim_when_present(self):
        status = _max_tier_status(
            _lc_status(State.DOWN, last_error="gx-max-start.sh exited 3: boom: rank1 died"),
            _READY_NODE2,
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
        )
        self.assertEqual(status.reason, "node2_unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
