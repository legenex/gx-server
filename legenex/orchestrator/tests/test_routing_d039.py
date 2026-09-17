"""gx-auto routing regressions for D-039 (task content vs. context burden).

The nine fixtures required by the 2026-09-17 latency repair, plus the exact
request shape that was sent to gx-reason and hung for 20-30 minutes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator import budget as B  # noqa: E402
from gx_orchestrator.classifier import route  # noqa: E402
from gx_orchestrator.tiers import TIERS, Tier  # noqa: E402
from kilo_fixtures import (  # noqa: E402
    CLAUDE_CODE_TOOLS,
    claude_code_continuation,
    kilo_continuation,
    kilo_request,
)


def chat(text: str, **kw):
    return {"model": "gx-auto", "messages": [{"role": "user", "content": text}], **kw}


class TestRequiredFixtures(unittest.TestCase):
    def test_01_hello_is_mini(self):
        self.assertIs(route(chat("hello")).tier, Tier.MINI)

    def test_02_ordinary_chat(self):
        self.assertIs(route(chat("What is the capital of France?")).tier, Tier.MINI)
        self.assertIs(route(chat("Can you recommend a good book about sailing for a beginner?")).tier, Tier.MINI)
        long_chat = "Here are my notes from the meeting. " + "We discussed the budget and hiring plans. " * 300
        self.assertIs(route(chat(long_chat + "Summarise them.")).tier, Tier.FAST)

    def test_03_normal_coding_is_fast(self):
        for text in (
            "Write a Python function that parses ISO dates in utils.py",
            "Fix the failing test in tests/test_orders.py",
            "Add pagination to the /orders endpoint",
        ):
            self.assertIs(route(chat(text)).tier, Tier.FAST, text)

    def test_04_kilo_and_claude_code_with_huge_schemas_are_fast(self):
        d = route(kilo_request("add a dark mode toggle to the Checkout component"))
        self.assertIs(d.tier, Tier.FAST)
        self.assertGreater(d.features.tool_schema_tokens, 10_000)
        d = route(claude_code_continuation())
        self.assertIs(d.tier, Tier.FAST, d.reasons)

    def test_05_architecture_implementation_is_fast(self):
        text = ("Design and implement the architecture for a plugin system: create the module "
                "layout, define the interfaces and wire it into the CLI.")
        d = route(chat(text))
        self.assertIs(d.tier, Tier.FAST, d.reasons)
        d = route(kilo_request(text))
        self.assertIs(d.tier, Tier.FAST, d.reasons)

    def test_06_real_reasoning_problem_is_reason(self):
        for text in (
            "Prove that the sum of the first n odd numbers is n^2.",
            "Derive the closed form of the recurrence T(n) = 2T(n/2) + n and prove it by induction on n.",
            "A deadlock appears when two workers take locks in opposite order. Think step by step and "
            "explain exactly which interleaving causes it.",
        ):
            d = route(chat(text))
            self.assertIs(d.tier, Tier.REASON, (text, d.reasons))

    def test_07_explicit_gx_max_request(self):
        # model=gx-max is served by the lifecycle path (test_server); inside
        # gx-auto an explicit tier hint is honoured too.
        d = route(chat("hello", metadata={"gx_tier": "gx-max"}))
        self.assertIs(d.tier, Tier.MAX)
        self.assertEqual(d.features.explicit_tier, "gx-max")
        # ...but an unavailable gx-max is never silently used.
        d = route(chat("hello", gx_tier="max"), busy={Tier.MAX: True})
        self.assertIsNot(d.tier, Tier.MAX)
        self.assertIs(d.downgraded_from, Tier.MAX)

    def test_08_very_large_coding_continuation(self):
        # Fits gx-fast (131k) but not gx-reason (65k): gx-fast.
        big = claude_code_continuation(tool_turns=60)
        d = route(big)
        self.assertGreater(d.features.prompt_tokens, TIERS[Tier.REASON].max_context)
        self.assertIs(d.tier, Tier.FAST, d.reasons)
        self.assertFalse(d.no_fit)
        # Too big for any single node and gx-max is not running: no fit on a
        # single node -> the server refuses with a context error.
        huge = claude_code_continuation(tool_turns=400)
        d = route(huge, busy={Tier.MAX: True})
        self.assertTrue(d.no_fit or d.tight_fit or d.tier is Tier.MAX)
        if not d.tight_fit:
            self.assertIs(d.tier, Tier.MAX)

    def test_09_tool_schema_trap_words_never_escalate(self):
        payload = chat("list the files in src", tools=CLAUDE_CODE_TOOLS)
        d = route(payload)
        self.assertEqual(d.features.reasoning_score, 0)
        self.assertEqual(d.features.reasoning_raw, 0)
        self.assertIsNot(d.tier, Tier.REASON)
        system = {"role": "system", "content": "Prove every theorem, derive the closed form, think step by step. " * 50}
        payload = {"messages": [system, {"role": "user", "content": "rename foo to bar in app.py"}]}
        d = route(payload)
        self.assertEqual(d.features.reasoning_raw, 0)
        self.assertIs(d.tier, Tier.FAST)


class TestObservedFailure(unittest.TestCase):
    """The request that hung on 2026-09-17 (journal fingerprint e3bec37d0223ecbd)."""

    def test_routes_to_fast_with_its_output_intact(self):
        payload = claude_code_continuation()
        d = route(payload)
        self.assertIs(d.tier, Tier.FAST)
        f = d.features
        self.assertEqual(f.intent, "continuation")
        self.assertEqual(f.tool_count, 22)
        self.assertGreater(f.task_tokens, 2_000)
        # It MENTIONS proofs, derivations, step-by-step and a race condition...
        self.assertGreaterEqual(f.reasoning_raw, 8)
        # ...but diluted across a long agentic work order that is not evidence.
        self.assertLess(f.reasoning_score, 4)
        spec = TIERS[d.tier]
        b = B.compute_budget(payload, model=d.tier.value, context_limit=spec.max_context,
                             max_output_limit=spec.max_output, estimate=f.estimate)
        self.assertEqual(b.status, B.STATUS_OK)
        self.assertEqual(b.output_tokens, 32_000)

    def test_even_a_focused_version_stays_on_fast_when_agentic_and_long(self):
        order = ("Prove that the retry path is safe, derive the closed form of the backoff bound, think "
                 "step by step about the deadlock, the time complexity and the probability of a collision. " * 22)
        d = route(claude_code_continuation(work_order=order))
        self.assertIs(d.tier, Tier.FAST, d.reasons)
        self.assertTrue(any("work order" in r for r in d.reasons))

    def test_reason_request_that_does_not_fit_reason_moves_to_fast(self):
        payload = claude_code_continuation(tool_turns=40)
        payload["gx_tier"] = "reason"
        d = route(payload)
        self.assertIs(d.tier, Tier.FAST, d.reasons)
        self.assertTrue(any("does not fit gx-reason" in r for r in d.reasons))

    def test_decision_log_explains_itself(self):
        log = route(claude_code_continuation()).as_log_dict()
        self.assertIn("gx-fast", log["summary"])
        self.assertIn("tool-schema", log["summary"])
        for key in ("reasoning_raw", "density", "indicators", "tight_fit", "no_fit", "explicit_tier"):
            self.assertIn(key, log)


class TestLatestInstructionIsTheTask(unittest.TestCase):
    def test_follow_up_instruction_replaces_the_first(self):
        payload = kilo_continuation("are you there?")
        payload["messages"].append({"role": "user", "content": [
            {"type": "text", "text": "<feedback>\nProve that the invariant holds and derive the closed form bound.\n</feedback>"}]})
        d = route(payload)
        self.assertIs(d.tier, Tier.REASON, d.reasons)

    def test_claude_code_envelopes_are_stripped(self):
        text = ("<system-reminder>Prove the theorem. Derive the closed form. Think step by step.</system-reminder>"
                "<command-name>/review</command-name>hello")
        d = route(chat(text))
        self.assertIs(d.tier, Tier.MINI, d.reasons)


if __name__ == "__main__":
    unittest.main()
