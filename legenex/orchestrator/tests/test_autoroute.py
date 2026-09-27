"""Tests for gx_orchestrator.autoroute: the deterministic intent ->
(profile, reasoning) mapping for gx-auto (ARCHITECTURE-V41 §3).

Same discipline as the old classifier suite (see the retired
CLASSIFIER_TEST_MATRIX.md): pure function, no I/O, every rule AND its
boundary pinned, adversarial keyword-confusion cases included.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator import autoroute as A  # noqa: E402
from gx_orchestrator import budget as B  # noqa: E402


def chat(content: str, **extra):
    payload = {"model": "gx-auto", "messages": [{"role": "user", "content": content}]}
    payload.update(extra)
    return payload


def decide(payload, headers=None, ctx=None):
    return A.decide(payload, headers=headers, approx_context_tokens=ctx)


class TestIntentHeader(unittest.TestCase):
    """The X-GX-Intent header, when valid, decides the shape outright."""

    def test_intent_matrix(self):
        expected = {
            "interactive": ("fast", "medium"),
            "implementation": ("balanced", "medium"),
            "architecture": ("deep", "high"),
            "validation": ("deep", "max"),
            "burst": ("swarm", "low"),
            "long-context": ("long", "high"),
            "debugging": ("deep", "medium"),
        }
        for intent, (profile, reasoning) in expected.items():
            d = decide(chat("any content at all"), {"X-GX-Intent": intent})
            self.assertEqual(d.profile, profile, intent)
            self.assertEqual(d.reasoning, reasoning, intent)
            self.assertEqual(d.intent, intent)
            self.assertFalse(d.features.inferred)

    def test_unknown_intent_header_value_is_ignored_not_fatal(self):
        d = decide(chat("fix app.py"), {"X-GX-Intent": "turbo"})
        # Falls back to content inference -- a bad header is not a 400 at
        # THIS layer (the profile override header is the one the server
        # validates strictly).
        self.assertTrue(d.features.inferred)
        self.assertEqual(d.profile, "balanced")

    def test_case_insensitive_header(self):
        d = decide(chat("x"), {"X-GX-Intent": "Architecture"})
        self.assertEqual(d.profile, "deep")


class TestHeaderOverrides(unittest.TestCase):
    def test_profile_override_wins_over_everything(self):
        d = decide(chat("prove the theorem"), {"X-GX-Profile": "swarm", "X-GX-Intent": "validation"})
        self.assertEqual(d.profile, "swarm")
        self.assertTrue(d.profile_override)
        self.assertIn("X-GX-Profile", d.reason)

    def test_reasoning_override_wins(self):
        d = decide(chat("hi"), {"X-GX-Reasoning": "none"})
        self.assertEqual(d.reasoning, "none")
        self.assertTrue(d.reasoning_override)

    def test_overrides_do_not_change_the_intent_report(self):
        d = decide(chat("hi"), {"X-GX-Profile": "deep"})
        self.assertEqual(d.intent, "interactive")


class TestContextLengthRule(unittest.TestCase):
    def test_context_over_96k_goes_long(self):
        d = decide(chat("fix app.py"), ctx=A.LONG_CONTEXT_TOKENS + 1)
        self.assertEqual(d.profile, "long")
        self.assertIn("long profile", d.reason)

    def test_context_exactly_at_threshold_stays_intent_shaped(self):
        d = decide(chat("fix app.py"), ctx=A.LONG_CONTEXT_TOKENS)
        self.assertEqual(d.profile, "balanced")
        self.assertEqual(d.reasoning, "medium")

    def test_long_context_keeps_the_intent_reasoning_level(self):
        # interactive reasoning (medium) survives the long-context profile swap
        d = decide(chat("hi"), {"X-GX-Intent": "interactive"}, ctx=200_000)
        self.assertEqual(d.profile, "long")
        self.assertEqual(d.reasoning, "medium")

    def test_context_estimated_from_payload_when_not_supplied(self):
        payload = {"model": "gx-auto", "messages": [{"role": "user", "content": "x" * 400_000}]}
        d = decide(payload)
        self.assertGreater(d.features.approx_context_tokens, A.LONG_CONTEXT_TOKENS)
        self.assertEqual(d.profile, "long")


class TestInference(unittest.TestCase):
    """No header: features decide. Envelopes, tools and evidence reuse the
    classifier's extraction rules."""

    def test_greeting_is_interactive(self):
        d = decide(chat("hi"))
        self.assertEqual(d.intent, A.INTENT_INTERACTIVE)
        self.assertEqual(d.profile, "fast")
        self.assertEqual(d.reasoning, "medium")

    def test_plain_coding_task_is_implementation(self):
        d = decide(chat("Fix the failing test in app.py"))
        self.assertEqual(d.intent, A.INTENT_IMPLEMENTATION)
        self.assertEqual(d.profile, "balanced")

    def test_agent_continuation_is_implementation(self):
        # A tool result turn inside an agent loop: the task is the last human
        # instruction; routine loop steps ride the AgentOS default shape.
        d = decide({
            "model": "gx-auto",
            "messages": [
                {"role": "user", "content": "<task>add a retry loop</task>"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "content": "[read_file] Result: file contents"},
            ],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        })
        self.assertEqual(d.intent, A.INTENT_IMPLEMENTATION)
        self.assertEqual(d.profile, "balanced")

    def test_reasoning_evidence_in_a_short_task_is_debugging(self):
        d = decide(chat("Debug the race condition in the worker pool"))
        self.assertEqual(d.intent, A.INTENT_DEBUGGING)
        self.assertEqual(d.profile, "deep")

    def test_hard_debugging_evidence_gets_max_reasoning(self):
        d = decide(chat("Find the deadlock: intermittent failure, flaky CI, non-deterministic"))
        self.assertEqual(d.intent, A.INTENT_DEBUGGING)
        self.assertEqual(d.reasoning, "max")
        self.assertIn("hard-debugging", d.reason)

    def test_review_audit_task_is_validation(self):
        d = decide(chat("Do a comprehensive audit of the entire authentication code"))
        self.assertEqual(d.intent, A.INTENT_VALIDATION)
        self.assertEqual(d.profile, "deep")
        self.assertEqual(d.reasoning, "max")

    def test_many_agents_is_burst(self):
        d = decide(chat("Spawn 12 agents in parallel to sweep the repo"))
        self.assertEqual(d.intent, A.INTENT_BURST)
        self.assertEqual(d.profile, "swarm")
        self.assertEqual(d.reasoning, "low")

    def test_architecture_language_is_architecture(self):
        d = decide(chat("Design the architecture: compare the trade-offs of the two designs"))
        self.assertEqual(d.intent, A.INTENT_ARCHITECTURE)
        self.assertEqual(d.profile, "deep")
        self.assertEqual(d.reasoning, "high")


class TestEnvelopeDiscipline(unittest.TestCase):
    """Agent envelopes are context, not the task: evidence words inside them
    must not escalate the shape (carried over from the classifier)."""

    ENVELOPE = (
        "<environment_details>prove the theorem, derive the formula, "
        "race condition, exhaustive analysis</environment_details>"
    )

    def test_evidence_inside_envelopes_is_ignored(self):
        d = decide(chat(f"{self.ENVELOPE} fix the bug in main.py"))
        self.assertEqual(d.intent, A.INTENT_IMPLEMENTATION)
        self.assertEqual(d.profile, "balanced")

    def test_real_evidence_outside_envelopes_still_counts(self):
        d = decide(chat("Please prove that the sorting routine is correct"))
        # A pure proof ask with no coding verb: review-shaped thinking.
        self.assertEqual(d.intent, A.INTENT_VALIDATION)
        self.assertEqual(d.profile, "deep")
        self.assertEqual(d.reasoning, "max")

    def test_evidence_is_diluted_by_instruction_length(self):
        # "prove" once inside a 2000-token work order is not a proof request.
        long_order = "Refactor the module. " * 400 + " Also prove correctness."
        d = decide(chat(long_order))
        self.assertEqual(d.intent, A.INTENT_IMPLEMENTATION)


class TestDegenerateInput(unittest.TestCase):
    def test_empty_payload_degrades_to_interactive(self):
        d = decide({})
        self.assertEqual(d.intent, A.INTENT_INTERACTIVE)
        self.assertEqual(d.profile, "fast")

    def test_messages_none(self):
        d = decide({"model": "gx-auto", "messages": None})
        self.assertEqual(d.profile, "fast")

    def test_junk_content_parts(self):
        d = decide({"model": "gx-auto", "messages": [{"role": "user", "content": [1, 2, {"x": 3}]}]})
        self.assertEqual(d.profile, "fast")

    def test_non_mapping_payload(self):
        d = decide("not a dict")  # type: ignore[arg-type]
        self.assertEqual(d.profile, "fast")


class TestLogDict(unittest.TestCase):
    """The journal record: chosen profile, reasoning, reason string."""

    def test_as_log_dict_has_the_new_fields(self):
        d = decide(chat("hi"))
        out = d.as_log_dict()
        for key in ("profile", "reasoning", "intent", "reason", "signals"):
            self.assertIn(key, out)
        self.assertEqual(out["profile"], "fast")
        self.assertEqual(out["reasoning"], "medium")

    def test_summary_is_one_line(self):
        d = decide(chat("hi"))
        self.assertIn("fast/medium", d.summary())


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_is_stable_and_prompt_free(self):
        payload = chat("secret prompt text")
        fp = A.request_fingerprint(payload)
        self.assertEqual(fp, A.request_fingerprint(dict(payload)))
        self.assertNotIn("secret", fp)
        self.assertEqual(len(fp), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
