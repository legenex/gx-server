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
    COMPLEXITY_FAST,
    COMPLEXITY_REASON,
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


def img_msg(text: str, **kw):
    """A user message carrying both a text part and an image part."""
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ],
        **kw,
    }


def text_for_tokens(target: int) -> str:
    """Build a string whose `estimate_tokens()` is exactly `target`.

    Probes the REAL `estimate_tokens` function rather than re-deriving its
    chars-per-token formula, so boundary tests stay correct even if that
    ratio ever changes.
    """
    assert target >= 1
    length = max(1, int(target * 3.2))
    while estimate_tokens("x" * length) < target:
        length += 1
    while estimate_tokens("x" * length) > target:
        length -= 1
    text = "x" * length
    assert estimate_tokens(text) == target, (estimate_tokens(text), target)
    return text


#: Filler that keeps a crafted prompt inside the 30-2000 token band, so the
#: length-based complexity adjustments (`length:very_short` / `long_instruction`
#: in classifier.py) never contaminate a boundary test that is only trying to
#: isolate keyword-pattern weights.
NEUTRAL_PAD = (
    " filler word here to keep the length comfortably inside the thirty to "
    "two thousand token band so no length bonus or penalty applies at all"
)


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


# ------------------------------------------------------------------------------
# Boundary values: exactly at a cut-point vs. one unit past it.
# ------------------------------------------------------------------------------


class TestComplexityBoundaries(unittest.TestCase):
    """COMPLEXITY_FAST (>=1 -> gx-fast) and COMPLEXITY_REASON (>=4 -> gx-reason)."""

    def test_complexity_zero_stays_mini(self):
        text = "Give me the weekly sales figures for the north region please." + NEUTRAL_PAD
        f = extract_features(msg(text))
        self.assertEqual(f.complexity_score, 0)
        self.assertIs(route(msg(text)).tier, Tier.MINI)

    def test_complexity_exactly_at_fast_threshold(self):
        # A single weight-1 reasoning pattern ("why does").
        text = "Why does this configuration break on restart?" + NEUTRAL_PAD
        f = extract_features(msg(text))
        self.assertEqual(f.complexity_score, COMPLEXITY_FAST)
        self.assertIs(route(msg(text)).tier, Tier.FAST)

    def test_complexity_one_below_reason_threshold_stays_fast(self):
        # debug/root-cause (+2) + why-does (+1) = 3, one short of COMPLEXITY_REASON.
        text = (
            "Please debug this issue and explain why does it happen." + NEUTRAL_PAD
        )
        f = extract_features(msg(text))
        self.assertEqual(f.complexity_score, COMPLEXITY_REASON - 1)
        self.assertIs(route(msg(text)).tier, Tier.FAST)

    def test_complexity_exactly_at_reason_threshold(self):
        # Same as above plus "implement" (+1) = 4 = COMPLEXITY_REASON exactly.
        text = (
            "Please debug this issue, explain why does it happen, and "
            "implement a fix." + NEUTRAL_PAD
        )
        f = extract_features(msg(text))
        self.assertEqual(f.complexity_score, COMPLEXITY_REASON)
        self.assertIs(route(msg(text)).tier, Tier.REASON)


class TestContextBoundary(unittest.TestCase):
    """context > MAX_SINGLE_NODE_CONTEXT -> gx-max; context == it -> gx-max is NOT forced."""

    def test_context_exactly_at_threshold_does_not_force_max(self):
        # prompt_tokens + max_tokens must land EXACTLY on the threshold.
        text = text_for_tokens(MAX_SINGLE_NODE_CONTEXT - 1)
        payload = msg(text, max_tokens=1)
        f = extract_features(payload)
        self.assertEqual(f.total_context_needed, MAX_SINGLE_NODE_CONTEXT)
        self.assertIsNot(
            route(payload).tier,
            Tier.MAX,
            "context == threshold must not force gx-max (the rule is strictly '>')",
        )

    def test_context_one_token_over_threshold_forces_max(self):
        text = text_for_tokens(MAX_SINGLE_NODE_CONTEXT)
        payload = msg(text, max_tokens=1)
        f = extract_features(payload)
        self.assertEqual(f.total_context_needed, MAX_SINGLE_NODE_CONTEXT + 1)
        self.assertIs(route(payload).tier, Tier.MAX)

    def test_large_context_threshold_matches_derived_constant(self):
        # LARGE_CONTEXT_THRESHOLD must never drift from the tier table (see
        # ARCHITECTURE.md section 6: "derived from the tier table").
        self.assertEqual(LARGE_CONTEXT_THRESHOLD, MAX_SINGLE_NODE_CONTEXT)


class TestHardScoreBoundary(unittest.TestCase):
    """hard_score >= HARD_SCORE_MAX (3) -> gx-max. No pattern weighs 1 or 2, so
    the only reachable boundary is 0 (no explicit-extreme signal) vs. 3 (one
    matched pattern) -- there is no partial/accumulated path to this category,
    which is the point of D-005."""

    def test_hard_score_exactly_at_threshold_triggers_max(self):
        text = "Please research this paper on transformer architectures for me." + NEUTRAL_PAD
        f = extract_features(msg(text))
        self.assertEqual(f.hard_score, HARD_SCORE_MAX)
        self.assertIs(route(msg(text)).tier, Tier.MAX)

    def test_hard_score_zero_never_reaches_max_alone(self):
        text = "This is a novel idea, thanks so much." + NEUTRAL_PAD  # "novel" w/o qualifier
        f = extract_features(msg(text))
        self.assertEqual(f.hard_score, 0)
        self.assertIsNot(route(msg(text)).tier, Tier.MAX)


class TestHardCategoryFalsePositives(unittest.TestCase):
    """Adversarial inputs that could confuse the keyword-based `hard` scoring.

    D-005 exists specifically so gx-max cannot be reached by accident. A
    keyword scorer is exactly the kind of mechanism that can regress that
    guarantee via an overly loose regex -- see the `exhaustive` bug fixed in
    classifier.py's `_HARD_PATTERNS` (this class pins that fix).
    """

    def test_extremely_simple_does_not_trigger_hard_category(self):
        # The architecture doc's "explicit 'extreme' marker" is a description
        # of the category, not a literal keyword -- the literal word
        # "extreme"/"extremely" must not itself be a signal.
        text = (
            "Can you write an extremely simple function to add two numbers?"
            + NEUTRAL_PAD
        )
        f = extract_features(msg(text))
        self.assertEqual(f.hard_score, 0)
        self.assertIsNot(route(msg(text)).tier, Tier.MAX)

    def test_bare_exhaustive_does_not_trigger_hard_category(self):
        # Regression test for a real bug: `\bexhaustiv|\bcomprehensive\b.*\b
        # (analysis|review|audit)\b` let "exhaustive" alone (no qualifying
        # noun) score +3 in the `hard` category on its own -- a single benign
        # word reaching HARD_SCORE_MAX and routing straight to gx-max. Fixed
        # by requiring the qualifier for both alternatives.
        text = "Please give an exhaustive list of HTTP status codes." + NEUTRAL_PAD
        f = extract_features(msg(text))
        self.assertEqual(
            f.hard_score, 0, "bare 'exhaustive' must not alone reach the hard category"
        )
        self.assertIsNot(route(msg(text)).tier, Tier.MAX)

    def test_bare_comprehensive_does_not_trigger_hard_category(self):
        # Pre-existing intended behaviour (not part of the bug): must still
        # hold after the fix.
        text = "We need a comprehensive plan for the product launch." + NEUTRAL_PAD
        f = extract_features(msg(text))
        self.assertEqual(f.hard_score, 0)

    def test_exhaustive_with_qualifier_still_triggers(self):
        # The fix must not remove genuine detection: "exhaustive" + a
        # qualifying noun is still an explicitly extreme request.
        text = (
            "Give an exhaustive analysis of this dataset and a comprehensive "
            "review of the results." + NEUTRAL_PAD
        )
        f = extract_features(msg(text))
        self.assertGreaterEqual(f.hard_score, HARD_SCORE_MAX)
        self.assertIs(route(msg(text)).tier, Tier.MAX)

    def test_hard_reasoning_words_alone_do_not_touch_hard_category(self):
        # "debug", "refactor", "derive" etc. are `reasoning`-category, never
        # `hard`-category, no matter how many stack up (this is the direct
        # regression surface for D-005/D-003).
        text = (
            "Debug this stack trace, derive the time complexity, prove the "
            "invariant, and refactor the whole function." + NEUTRAL_PAD
        )
        f = extract_features(msg(text))
        self.assertEqual(f.hard_score, 0)
        self.assertIsNot(route(msg(text)).tier, Tier.MAX)


class TestMixedSignals(unittest.TestCase):
    """Combinations of image / tools / high complexity signals."""

    def test_image_plus_tools_plus_explicit_extreme_lands_on_vision_reason(self):
        # hard_score >= 3 alone would pick gx-max, but gx-max has no vision.
        # The vision override then picks the most capable VISION tier at or
        # below gx-max's cost, which is gx-reason (gx-max itself is not
        # vision-capable per tiers.py).
        payload = img_msg(
            "Do a comprehensive audit of the entire codebase and formal verification",
            tools=[{"name": "search"}],
        )
        f = extract_features(payload)
        self.assertTrue(f.has_images)
        self.assertTrue(f.has_tools)
        self.assertGreaterEqual(f.hard_score, HARD_SCORE_MAX)
        d = route(payload)
        self.assertIs(d.tier, Tier.REASON)
        self.assertTrue(TIERS[d.tier].vision)

    def test_image_plus_tools_plus_trivial_dispatch_still_gets_tool_floor(self):
        # Trivial + tools alone raises gx-mini -> gx-fast (the tool floor).
        # Adding an image must not undo that, and gx-fast is already
        # vision-capable so no further override is needed.
        payload = img_msg(
            "Classify this ticket into one of three buckets." + NEUTRAL_PAD,
            tools=[{"name": "classify"}],
        )
        f = extract_features(payload)
        self.assertTrue(f.has_images)
        self.assertTrue(f.has_tools)
        d = route(payload)
        self.assertIs(d.tier, Tier.FAST)
        self.assertTrue(TIERS[d.tier].vision)

    def test_image_alone_on_huge_context_still_respects_context_constraint(self):
        # Regression test for a real bug: the vision override used to pick
        # "the most capable vision tier" by cost_rank alone, with no check
        # that the tier's context window could actually hold the request. A
        # ~292k-token prompt (needs gx-max) plus an image got silently routed
        # to gx-reason (131_072 max_context) -- less than half what the
        # request needed, guaranteed to fail or truncate upstream. gx-max has
        # no vision at all, but if NO vision-capable tier can hold the
        # context either, staying on gx-max (full context, no vision) is
        # strictly better than a vision tier that cannot hold the prompt.
        payload = img_msg(oversized_prompt())
        f = extract_features(payload)
        self.assertGreater(f.total_context_needed, TIERS[Tier.REASON].max_context)
        d = route(payload)
        self.assertIs(d.tier, Tier.MAX)


class TestVeryLongPrompts(unittest.TestCase):
    def test_medium_context_nudges_trivial_request_off_mini(self):
        # A trivial-scored request ("hi ...") that is nonetheless too big for
        # gx-mini's context must be nudged up to the cheapest tier that can
        # actually hold it, even though its complexity score alone would
        # never leave gx-mini.
        text = "hi " + text_for_tokens(95_000)
        payload = msg(text)
        f = extract_features(payload)
        self.assertLess(f.complexity_score, COMPLEXITY_FAST)
        self.assertGreaterEqual(f.total_context_needed, 60_000)
        self.assertGreater(f.total_context_needed, TIERS[Tier.MINI].max_context)
        d = route(payload)
        self.assertIsNot(d.tier, Tier.MINI)
        self.assertGreaterEqual(TIERS[d.tier].max_context, f.total_context_needed)

    def test_very_long_prompt_under_threshold_does_not_force_max(self):
        text = text_for_tokens(MAX_SINGLE_NODE_CONTEXT - 5_000)
        d = route(msg(text, max_tokens=1))
        self.assertIsNot(d.tier, Tier.MAX)


class TestDegenerateInput(unittest.TestCase):
    """Empty / malformed payloads must never raise and must fail safe (gx-mini)."""

    def test_empty_payload(self):
        d = route({})
        self.assertIs(d.tier, Tier.MINI)

    def test_messages_key_missing_entirely(self):
        f = extract_features({})
        self.assertEqual(f.prompt_tokens, 0)

    def test_messages_value_is_none(self):
        d = route({"messages": None})
        self.assertIs(d.tier, Tier.MINI)

    def test_no_user_turn_only_system(self):
        payload = {"messages": [{"role": "system", "content": "you are a helpful assistant"}]}
        d = route(payload)
        self.assertIs(d.tier, Tier.MINI)

    def test_content_is_none(self):
        payload = {"messages": [{"role": "user", "content": None}]}
        d = route(payload)  # must not raise
        self.assertIs(d.tier, Tier.MINI)

    def test_content_list_with_junk_parts_does_not_crash(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text"},  # missing "text" key
                        {"foo": "bar"},  # unrelated mapping
                        "a bare string list item",
                        {"type": "image_url"},  # missing image_url payload, still counts as image
                    ],
                }
            ]
        }
        f = extract_features(payload)  # must not raise
        self.assertTrue(f.has_images)
        route(payload)  # must not raise

    def test_role_matching_is_case_sensitive(self):
        # Documents current, intentional behaviour: OpenAI-spec roles are
        # always lowercase, so "USER" is not treated as a user turn and
        # contributes no complexity signal. If this ever needs to change it
        # must be a deliberate decision, not a silent side effect.
        payload = {"messages": [{"role": "USER", "content": "prove this theorem"}]}
        f = extract_features(payload)
        self.assertEqual(f.complexity_score, -1, "uppercase role must not be read as a user turn")
        self.assertIs(route(payload).tier, Tier.MINI)

    def test_empty_string_content(self):
        d = route(msg(""))
        self.assertIs(d.tier, Tier.MINI)


class TestDowngradeLogging(unittest.TestCase):
    """`downgraded_from` must reflect what gx-auto actually did, for gx.routing
    logging (ARCHITECTURE.md section 6: 'every decision is logged... ')."""

    def test_multi_hop_fallback_records_original_tier_not_intermediate_hop(self):
        # gx-reason AND gx-fast both unavailable: the router must walk past
        # gx-fast straight to gx-mini, and downgraded_from must say gx-reason
        # (what the task actually needed), not gx-fast (a hop it skipped).
        payload = msg("Debug this stack trace and derive the time complexity")
        pre = route(payload)
        self.assertIs(pre.tier, Tier.REASON)  # sanity: this is the undegraded choice

        d = route(payload, available={Tier.REASON: False, Tier.FAST: False})
        self.assertIs(d.tier, Tier.MINI)
        self.assertIs(d.downgraded_from, Tier.REASON)

    def test_downgraded_from_is_none_when_nothing_changes(self):
        d = route(msg(oversized_prompt()), busy={Tier.MAX: True})
        self.assertIs(d.tier, Tier.MAX)
        self.assertIsNone(
            d.downgraded_from, "no actual downgrade happened; the field must stay None"
        )

    def test_downgrade_reason_is_present_in_log_dict(self):
        payload = msg("Do a comprehensive audit of the entire codebase and formal verification")
        d = route(payload, busy={Tier.MAX: True})
        log = d.as_log_dict()
        self.assertEqual(log["downgraded_from"], "gx-max")
        self.assertTrue(any("unavailable/busy" in r for r in log["reasons"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
