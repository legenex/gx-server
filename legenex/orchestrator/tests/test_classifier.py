"""Unit tests for the deterministic gx-auto router.

These tests run with no cluster, no network and no model: the classifier is a
pure function. Run with:  python3 -m unittest discover -s legenex/orchestrator
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator.classifier import (  # noqa: E402
    HARD_SCORE_MAX,
    LARGE_CONTEXT_THRESHOLD,
    estimate_tokens,
    extract_features,
    route,
)
from gx_orchestrator.tiers import MAX_SINGLE_NODE_CONTEXT, TIERS, Tier  # noqa: E402


def oversized_prompt() -> str:
    """A prompt no single-node tier can hold (chars, pessimistically ~3.2/token)."""
    return "x" * int((MAX_SINGLE_NODE_CONTEXT + 20_000) * 3.3)


def msg(text: str, **kw):
    return {"messages": [{"role": "user", "content": text}], **kw}


class TestTokenEstimation(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_pessimistic(self):
        # We must never UNDER-estimate, or we route a prompt to a tier that
        # cannot hold it.
        text = "word " * 1000
        self.assertGreater(estimate_tokens(text), 1000)


class TestFeatureExtraction(unittest.TestCase):
    def test_detects_image_parts(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                    ],
                }
            ]
        }
        self.assertTrue(extract_features(payload).has_images)

    def test_no_false_image_positive(self):
        self.assertFalse(extract_features(msg("describe an image of a cat")).has_images)

    def test_only_last_user_turn_scores_complexity(self):
        payload = {
            "messages": [
                {"role": "user", "content": "prove this theorem and derive the integral"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "thanks"},
            ]
        }
        f = extract_features(payload)
        self.assertLessEqual(f.complexity_score, 0, "earlier turns must not drive routing")

    def test_tools_detected(self):
        self.assertTrue(extract_features(msg("do it", tools=[{"name": "x"}])).has_tools)


class TestTierSelection(unittest.TestCase):
    def test_trivial_to_mini(self):
        for text in ("hi", "hello", "thanks", "what is the date"):
            self.assertIs(route(msg(text)).tier, Tier.MINI, text)

    def test_dispatch_to_mini(self):
        self.assertIs(route(msg("Classify this ticket into one of three buckets.")).tier, Tier.MINI)

    def test_tool_floor_raises_to_fast(self):
        d = route(msg("look it up", tools=[{"name": "search"}]))
        self.assertIs(d.tier, Tier.FAST)

    def test_hard_reasoning_to_reason_not_max(self):
        # gx-max owns BOTH nodes; ordinary hard work must not reach it.
        d = route(msg("Debug this stack trace and derive the time complexity, then refactor it"))
        self.assertIs(d.tier, Tier.REASON)

    def test_explicit_extreme_to_max(self):
        d = route(msg("Do a comprehensive audit of the entire codebase and formal verification"))
        self.assertIs(d.tier, Tier.MAX)
        self.assertGreaterEqual(d.features.hard_score, HARD_SCORE_MAX)

    def test_huge_context_forces_max(self):
        d = route(msg(oversized_prompt()))
        self.assertIs(d.tier, Tier.MAX)

    def test_determinism(self):
        p = msg("Design a system architecture for a distributed queue")
        self.assertEqual(route(p).tier, route(p).tier)


class TestVisionRouting(unittest.TestCase):
    def _img(self, text: str):
        return {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                    ],
                }
            ]
        }

    def test_image_lands_on_vision_capable_tier(self):
        d = route(self._img("what is in this picture"))
        self.assertTrue(TIERS[d.tier].vision, f"{d.tier} has no vision")

    def test_image_never_routes_to_non_vision_tier(self):
        d = route(self._img("Do a comprehensive audit of the entire codebase and formal verification"))
        self.assertTrue(TIERS[d.tier].vision)


class TestAvailabilityFallback(unittest.TestCase):
    def test_busy_max_falls_back_for_auto(self):
        payload = msg("Do a comprehensive audit of the entire codebase and formal verification")
        d = route(payload, busy={Tier.MAX: True})
        self.assertIsNot(d.tier, Tier.MAX)
        self.assertIs(d.downgraded_from, Tier.MAX)

    def test_unavailable_tier_falls_back(self):
        d = route(msg("Debug this stack trace and derive the time complexity"), available={Tier.REASON: False})
        self.assertIsNot(d.tier, Tier.REASON)

    def test_fallback_never_breaks_context_constraint(self):
        # A prompt that only gx-max can hold must stay on gx-max even when busy:
        # there is nowhere smaller for it to go. Degrading here would silently
        # truncate the user's input.
        d = route(msg(oversized_prompt()), busy={Tier.MAX: True})
        self.assertIs(d.tier, Tier.MAX)

    def test_fallback_preserves_vision(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "analyse this diagram in depth"},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                    ],
                }
            ]
        }
        d = route(payload, busy={Tier.FAST: True, Tier.REASON: True})
        self.assertTrue(TIERS[d.tier].vision)


class TestDecisionLogging(unittest.TestCase):
    def test_reasons_always_present(self):
        d = route(msg("hello"))
        self.assertTrue(d.reasons)

    def test_log_dict_is_json_safe(self):
        import json
        json.dumps(route(msg("hi")).as_log_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
