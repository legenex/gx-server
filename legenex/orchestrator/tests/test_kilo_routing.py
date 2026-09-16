"""gx-auto regression tests on Kilo-Code-shaped requests (D-030).

The failure these pin: a two-word Kilo prompt ("are you there?") carrying
Kilo's whole toolbox, a ~50 kB system prompt and a workspace file tree was
routed to gx-fast/gx-reason and sat in "Thinking" for minutes while a heavy
model cold-started. Tool-schema size is context, not task intent.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator.classifier import (  # noqa: E402
    INTENT_CONTINUATION,
    INTENT_CONVERSATIONAL,
    extract_features,
    request_fingerprint,
    route,
)
from gx_orchestrator.tiers import TIERS, Tier  # noqa: E402
from kilo_fixtures import (  # noqa: E402
    KILO_ROUTING_CASES,
    kilo_continuation,
    kilo_request,
)


class TestKiloFirstTurnRouting(unittest.TestCase):
    def test_every_case_routes_as_expected(self):
        for task, expected, why in KILO_ROUTING_CASES:
            with self.subTest(task=task[:40], why=why):
                d = route(kilo_request(task))
                self.assertEqual(d.tier.value, expected, d.reasons)

    def test_schema_is_large_but_fits_mini(self):
        f = extract_features(kilo_request("are you there?"))
        self.assertGreater(f.tool_schema_tokens, 10_000, "fixture must carry a heavy schema")
        self.assertGreater(f.prompt_tokens, 30_000, "fixture must carry a heavy prompt")
        self.assertLessEqual(f.total_context_needed, TIERS[Tier.MINI].max_context)
        self.assertEqual(f.intent, INTENT_CONVERSATIONAL)
        self.assertLessEqual(f.complexity_score, 0)

    def test_environment_details_trap_words_are_not_scored(self):
        f = extract_features(kilo_request("are you there?"))
        self.assertEqual(f.reasoning_score, 0)
        self.assertEqual(f.hard_score, 0)
        self.assertLess(f.task_tokens, 10)

    def test_user_message_tag_is_extracted(self):
        d = route(kilo_request("are you there?", tag="user_message"))
        self.assertIs(d.tier, Tier.MINI)

    def test_huge_max_tokens_does_not_escalate(self):
        d = route(kilo_request("hello", max_tokens=262_144))
        self.assertIs(d.tier, Tier.MINI)

    def test_decision_explains_schema_was_ignored(self):
        d = route(kilo_request("are you there?"))
        self.assertTrue(any("schema size does not raise" in r for r in d.reasons), d.reasons)


class TestKiloAgentLoop(unittest.TestCase):
    def test_native_tool_result_continues_on_task_tier(self):
        easy = route(kilo_continuation("add a dark mode toggle to the Checkout component"))
        self.assertEqual(easy.features.intent, INTENT_CONTINUATION)
        self.assertIs(easy.tier, Tier.FAST)

    def test_xml_protocol_tool_result_continues_on_task_tier(self):
        d = route(kilo_continuation("add a dark mode toggle", native_tool_role=False))
        self.assertEqual(d.features.intent, INTENT_CONTINUATION)
        self.assertIs(d.tier, Tier.FAST)

    def test_hard_task_stays_on_reason_through_the_loop(self):
        task = KILO_ROUTING_CASES[8][0]
        self.assertIs(route(kilo_request(task)).tier, Tier.REASON)
        self.assertIs(route(kilo_continuation(task)).tier, Tier.REASON)

    def test_conversational_task_loop_never_drops_below_fast(self):
        # Even if the original task was chit-chat, a tool loop is agentic work.
        d = route(kilo_continuation("are you there?"))
        self.assertIs(d.tier, Tier.FAST)


class TestFingerprint(unittest.TestCase):
    def test_stable_and_message_sensitive(self):
        a = kilo_request("are you there?")
        b = kilo_request("are you there?")
        c = kilo_request("hello")
        self.assertEqual(request_fingerprint(a), request_fingerprint(b))
        self.assertNotEqual(request_fingerprint(a), request_fingerprint(c))
        self.assertEqual(len(request_fingerprint(a)), 16)

    def test_degenerate(self):
        self.assertEqual(len(request_fingerprint({})), 16)


class TestPlainChatStillRoutes(unittest.TestCase):
    def test_simple_question_to_mini(self):
        self.assertIs(route({"messages": [{"role": "user", "content": "What is the capital of France?"}]}).tier, Tier.MINI)

    def test_coding_without_tools_to_fast(self):
        p = {"messages": [{"role": "user", "content": "Write a Python function that parses ISO dates in utils.py"}]}
        self.assertIs(route(p).tier, Tier.FAST)

    def test_hard_math_to_reason(self):
        p = {"messages": [{"role": "user", "content": "Prove that the sum of the first n odd numbers is n^2 and derive the closed form step by step."}]}
        self.assertIs(route(p).tier, Tier.REASON)


if __name__ == "__main__":
    unittest.main()
