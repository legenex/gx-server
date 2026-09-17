"""Context budgeting (D-039): never knowingly forward a request that cannot fit.

Hermetic. Run with:  python3 -m unittest discover -s legenex/orchestrator/tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator import budget as B  # noqa: E402
from gx_orchestrator.server import clamp_output_budget, tier_budget  # noqa: E402
from gx_orchestrator.tiers import TIERS, Tier  # noqa: E402
from kilo_fixtures import claude_code_continuation  # noqa: E402

#: The engine's own message for the 2026-09-17 failure, verbatim.
OBSERVED_ENGINE_ERROR = (
    "litellm.ContextWindowExceededError: litellm.BadRequestError: ContextWindowExceededError: "
    "OpenAIException - This model's maximum context length is 65536 tokens. However, you requested "
    "32000 output tokens and your prompt contains at least 33537 input tokens, for a total of at "
    "least 65537 tokens. Please reduce the length of the input prompt or the number of requested "
    "output tokens. (parameter=input_tokens, value=33537)"
)


def chat(text: str, **kw):
    return {"messages": [{"role": "user", "content": text}], **kw}


class TestEstimate(unittest.TestCase):
    def test_every_part_of_the_request_counts(self):
        base = B.estimate_input(chat("hello"))
        with_system = B.estimate_input({"messages": [
            {"role": "system", "content": "s" * 3200}, {"role": "user", "content": "hello"}]})
        self.assertGreaterEqual(with_system.system_tokens, 1000)
        self.assertGreater(with_system.total_tokens, base.total_tokens + 1000)

        tools = [{"type": "function", "function": {"name": "t", "description": "d" * 3200}}]
        with_tools = B.estimate_input(chat("hello", tools=tools))
        self.assertGreaterEqual(with_tools.tool_schema_tokens, 1000)
        self.assertEqual(with_tools.tool_count, 1)
        self.assertGreater(with_tools.total_tokens, base.total_tokens + 1000)

    def test_tool_calls_tool_results_and_reasoning_count(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "reasoning_content": "r" * 3200,
             "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "read", "arguments": json.dumps({"p": "a" * 3200})}}]},
            {"role": "tool", "tool_call_id": "1", "content": "z" * 3200},
        ]
        est = B.estimate_input({"messages": msgs})
        self.assertGreater(est.history_tokens, 2900)
        self.assertEqual(est.message_count, 3)

    def test_image_bytes_are_not_text(self):
        huge_b64 = "data:image/png;base64," + "A" * 2_000_000
        est = B.estimate_input({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": huge_b64}}]}]})
        self.assertEqual(est.image_count, 1)
        self.assertLess(est.total_tokens, 5_000)

    def test_unknown_parts_count_text_not_binary(self):
        est = B.estimate_input({"messages": [{"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "q" * 3200}]},
            {"type": "document", "data": "B" * 100_000}]}]})
        self.assertGreater(est.total_tokens, 900)
        self.assertLess(est.total_tokens, 2_000)

    def test_upper_is_pessimistic_and_lower_is_optimistic(self):
        # The observed request: engine 33 537 tokens.
        est = B.estimate_input(claude_code_continuation())
        self.assertGreater(est.total_tokens, 33_537)
        self.assertLess(est.lower_bound_tokens, 33_537)

    def test_degenerate(self):
        for payload in ({}, {"messages": None}, {"messages": ["x", 3]}, None):
            est = B.estimate_input(payload)  # must not raise
            self.assertEqual(est.message_count, 0)


class TestComputeBudget(unittest.TestCase):
    def test_short_request_is_untouched(self):
        b = tier_budget(chat("hello", max_tokens=2048), Tier.REASON)
        self.assertEqual(b.status, B.STATUS_OK)
        self.assertEqual(b.output_tokens, 2048)
        self.assertFalse(b.clamped)

    def test_smaller_safe_request_is_preserved(self):
        b = tier_budget(chat("x" * 100_000, max_tokens=500), Tier.REASON)
        self.assertEqual(b.output_tokens, 500)
        self.assertFalse(b.clamped)

    def test_no_max_tokens_stays_unset_when_room_exists(self):
        payload = chat("hello")
        b = tier_budget(payload, Tier.FAST)
        self.assertIsNone(b.output_tokens)
        self.assertNotIn("max_tokens", B.apply_budget(payload, b))

    def test_engine_output_ceiling_still_applies(self):
        out = clamp_output_budget(chat("hello", max_tokens=262_144), Tier.MINI)
        self.assertEqual(out["max_tokens"], TIERS[Tier.MINI].max_output)

    def test_max_completion_tokens_is_clamped_too(self):
        payload = claude_code_continuation()
        payload.pop("max_tokens")
        payload["max_completion_tokens"] = 32_000
        out = clamp_output_budget(payload, Tier.REASON)
        self.assertLess(out["max_completion_tokens"], 32_000)
        self.assertNotIn("max_tokens", out)

    def test_observed_65536_failure_is_clamped_to_fit(self):
        """EXACT regression: 22 tools, ~18k schema tokens, max_tokens 32000, 65 536 window."""
        payload = claude_code_continuation()
        self.assertEqual(payload["max_tokens"], 32_000)
        b = tier_budget(payload, Tier.REASON)
        self.assertEqual(b.context_limit, 65_536)
        self.assertEqual(b.tool_count, 22)
        self.assertGreater(b.tool_schema_tokens, 17_000)
        self.assertEqual(b.requested_output_tokens, 32_000)
        self.assertTrue(b.clamped)
        self.assertEqual(b.status, B.STATUS_CLAMPED)
        self.assertLessEqual(b.estimated_input_tokens + b.output_tokens + b.safety_margin, 65_536)
        # The engine's real count was 33 537: the clamped request fits it too.
        self.assertLessEqual(33_537 + b.output_tokens, 65_536)
        out = B.apply_budget(payload, b)
        self.assertEqual(out["max_tokens"], b.output_tokens)
        self.assertEqual(payload["max_tokens"], 32_000, "the caller's payload is not mutated")
        # The fields the spec requires are all present.
        for key in ("model", "context_limit", "estimated_input_tokens", "tool_schema_tokens",
                    "requested_output_tokens", "safe_output_tokens", "clamped",
                    "remaining_context", "error_code"):
            self.assertIn(key, b.as_dict())

    def test_same_request_on_fast_is_not_clamped(self):
        b = tier_budget(claude_code_continuation(), Tier.FAST)
        self.assertEqual(b.status, B.STATUS_OK)
        self.assertEqual(b.output_tokens, 32_000)

    def test_input_that_cannot_fit_is_overflow(self):
        text = "x" * int(70_000 * B.CHARS_PER_TOKEN_LOWER)
        b = tier_budget(chat(text, max_tokens=100), Tier.REASON)
        self.assertEqual(b.status, B.STATUS_OVERFLOW)
        self.assertFalse(b.fits)
        self.assertEqual(b.error_code, "context_length_exceeded")
        self.assertIsNone(b.output_tokens)

    def test_tight_input_gets_a_small_allowance_not_a_refusal(self):
        # Pessimistically over the window, optimistically inside it.
        text = "x" * int(66_000 * B.CHARS_PER_TOKEN_UPPER)
        b = tier_budget(chat(text, max_tokens=32_000), Tier.REASON)
        self.assertEqual(b.status, B.STATUS_TIGHT)
        self.assertEqual(b.output_tokens, B.MIN_USEFUL_OUTPUT)
        self.assertTrue(b.clamped)

    def test_margin_scales_with_window(self):
        self.assertEqual(B.safety_margin(8_192), B.SAFETY_MARGIN_MIN)
        self.assertEqual(B.safety_margin(327_680), int(327_680 * B.SAFETY_MARGIN_FRACTION))

    def test_bool_and_junk_max_tokens_are_ignored(self):
        self.assertIsNone(B.requested_output({"max_tokens": True}))
        self.assertIsNone(B.requested_output({"max_tokens": "lots"}))
        self.assertIsNone(B.requested_output({"max_tokens": -5}))
        self.assertEqual(B.requested_output({"max_tokens": "300"}), 300)


class TestEngineFeedback(unittest.TestCase):
    def test_parses_the_observed_vllm_message(self):
        err = B.parse_context_error(OBSERVED_ENGINE_ERROR)
        self.assertEqual((err.context_limit, err.input_tokens), (65_536, 33_537))

    def test_parses_older_vllm_and_llamacpp(self):
        old = ("This model's maximum context length is 8192 tokens. However, you requested 9000 "
               "tokens (7000 in the messages, 2000 in the completion).")
        self.assertEqual(B.parse_context_error(old).input_tokens, 7000)
        cpp = "request (70000 tokens) exceeds the available context size (65536 tokens), try increasing it"
        err = B.parse_context_error(cpp)
        self.assertEqual((err.context_limit, err.input_tokens), (65_536, 70_000))

    def test_generic_and_unrelated(self):
        self.assertEqual(B.parse_context_error('{"code":"context_length_exceeded"}'), B.EngineContextError(None, None))
        self.assertIsNone(B.parse_context_error("All non-assistant messages must contain 'content'"))
        self.assertIsNone(B.parse_context_error(""))

    def _budget(self, output: int, requested: int = 32_000):
        base = tier_budget(chat("x", max_tokens=requested), Tier.REASON)
        from dataclasses import replace
        return replace(base, output_tokens=output)

    def test_correction_uses_the_exact_count_once(self):
        b = self._budget(20_000)
        fixed = B.corrected_output(B.EngineContextError(65_536, 50_000), b)
        self.assertEqual(fixed, 65_536 - 50_000 - B.EXACT_RETRY_MARGIN)

    def test_no_correction_that_would_resend_an_equivalent_payload(self):
        b = self._budget(10_000)
        self.assertIsNone(B.corrected_output(B.EngineContextError(65_536, 33_537), b))

    def test_no_correction_without_a_count_or_without_room(self):
        b = self._budget(20_000)
        self.assertIsNone(B.corrected_output(B.EngineContextError(None, None), b))
        self.assertIsNone(B.corrected_output(B.EngineContextError(65_536, 65_400), b))

    def test_error_payload_is_structured_and_not_retryable(self):
        b = tier_budget(chat("x" * int(70_000 * B.CHARS_PER_TOKEN_LOWER)), Tier.REASON)
        body = B.error_payload(b, message=B.overflow_message(b), attempts=1, elapsed_ms=3.2)
        err = body["error"]
        self.assertEqual(err["code"], "context_length_exceeded")
        self.assertEqual(err["type"], "invalid_request_error")
        self.assertFalse(err["retryable"])
        for key in ("model", "context_limit", "estimated_input_tokens", "tool_schema_tokens",
                    "requested_output_tokens", "safe_output_tokens", "clamped", "remaining_context",
                    "error_code", "attempts", "elapsed_ms"):
            self.assertIn(key, err["gx_budget"])
        json.dumps(body)


class TestTierTableMatchesServedConfig(unittest.TestCase):
    """The budget is only authoritative if the tier table matches what is served."""

    REPO = Path(__file__).resolve().parents[3]

    def _flag(self, text: str, flag: str) -> list[int]:
        import re
        return [int(m) for m in re.findall(rf"{flag}\s+(\d+)", text)]

    def test_llama_swap_windows(self):
        node1 = (self.REPO / "legenex/gateway/llama-swap/node01.yaml").read_text()
        node2 = (self.REPO / "legenex/gateway/llama-swap/node02.yaml").read_text()
        self.assertIn(TIERS[Tier.FAST].max_context, self._flag(node1, "--max-model-len"))
        ctx = self._flag(node1, "--ctx-size")[0]
        parallel = self._flag(node1, "--parallel")[0]
        self.assertEqual(TIERS[Tier.MINI].max_context, ctx // parallel)
        self.assertEqual(set(self._flag(node2, "--max-model-len")), {TIERS[Tier.REASON].max_context})

    def test_gx_max_window(self):
        conf = (self.REPO / "legenex/lifecycle/gx-max.conf").read_text()
        import re
        m = re.search(r'GXMAX_CONTEXT_LENGTH="\$\{GXMAX_CONTEXT_LENGTH:-(\d+)\}"', conf)
        self.assertEqual(int(m.group(1)), TIERS[Tier.MAX].max_context)

    def test_registry_windows(self):
        reg = json.loads((self.REPO / "legenex/models/registry.json").read_text())["aliases"]
        for tier in (Tier.MINI, Tier.FAST, Tier.REASON, Tier.MAX):
            self.assertEqual(reg[tier.value]["context"], TIERS[tier].max_context, tier)


if __name__ == "__main__":
    unittest.main()
