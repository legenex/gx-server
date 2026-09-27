"""Context budgeting (D-039, V4.1): never knowingly forward a request that
cannot fit the profile's served window.

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
from gx_orchestrator.config import Config  # noqa: E402
from gx_orchestrator.server import profile_budget  # noqa: E402
from tests.registry_fixtures import load_fixture_registry  # noqa: E402


def _payload(chars_per_msg: int = 1000, n_msgs: int = 10, **extra) -> dict:
    msgs = [{"role": "user", "content": "x" * chars_per_msg} for _ in range(n_msgs)]
    return {"messages": msgs, **extra}


def _claude_code_like_payload() -> dict:
    """The D-039 failure shape: ~20 k tokens of tool schema plus an agentic
    'give me your whole output window' request."""
    tools = []
    for i in range(40):
        tools.append({
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": "y" * 400,
                "parameters": {"type": "object", "properties": {
                    f"arg_{j}": {"type": "string", "description": "z" * 60}
                    for j in range(6)
                }},
            },
        })
    return {
        "messages": [{"role": "user", "content": "please continue"}],
        "tools": tools,
        "max_tokens": 32000,
    }


class TestEstimation(unittest.TestCase):
    def test_text_messages(self):
        est = B.estimate_input(_payload(3200, 20))
        # 64 000 chars / 3.2 = 20 000 pessimistic
        self.assertEqual(est.message_count, 20)
        self.assertEqual(est.history_tokens, 20_001)

    def test_system_vs_history_split(self):
        payload = {
            "messages": [
                {"role": "system", "content": "s" * 320},
                {"role": "user", "content": "u" * 320},
            ]
        }
        est = B.estimate_input(payload)
        self.assertEqual(est.system_tokens, 101)
        self.assertEqual(est.history_tokens, 101)

    def test_tool_schema_counted_never_binary(self):
        payload = {
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 50_000}},
            ]}],
            "tools": [{"type": "function", "function": {
                "name": "f", "description": "d" * 320,
                "parameters": {"type": "object"},
            }}],
        }
        est = B.estimate_input(payload)
        self.assertEqual(est.image_count, 1)
        self.assertEqual(est.tool_count, 1)
        # the base64 blob must not be counted as prompt text
        self.assertLess(est.total_tokens, 5_000)
        # image tokens are counted at the upper bound
        self.assertGreaterEqual(est.total_tokens, B.IMAGE_TOKENS_UPPER)

    def test_reasoning_replay_is_history(self):
        payload = {"messages": [
            {"role": "assistant", "content": "", "reasoning_content": "r" * 3200},
        ]}
        est = B.estimate_input(payload)
        self.assertEqual(est.history_tokens, 1_001)

    def test_requested_output(self):
        self.assertEqual(B.requested_output({"max_tokens": 5000}), 5000)
        self.assertEqual(B.requested_output({"max_completion_tokens": 7}), 7)
        self.assertIsNone(B.requested_output({"max_tokens": 0}))
        self.assertIsNone(B.requested_output({"max_tokens": True}))  # bool is not a budget
        self.assertIsNone(B.requested_output({"max_tokens": "junk"}))
        self.assertIsNone(B.requested_output({}))


class TestComputeBudget(unittest.TestCase):
    LIMIT = 65_536

    def budget(self, payload, **kw):
        return B.compute_budget(
            payload, model="gx-max:balanced", context_limit=self.LIMIT,
            max_output_limit=32_768, **kw,
        )

    def test_request_that_fits_is_forwarded_unchanged(self):
        b = self.budget(_payload(320, 10) | {"max_tokens": 4096})
        self.assertEqual(b.status, B.STATUS_OK)
        self.assertFalse(b.clamped)
        self.assertEqual(b.output_tokens, 4096)
        applied = B.apply_budget(_payload(320, 10) | {"max_tokens": 4096}, b)
        self.assertEqual(applied["max_tokens"], 4096)

    def test_huge_requested_output_is_clamped(self):
        # The D-039 shape against a 16 k window: the agentic 32 k output
        # request cannot survive, the budget must clamp it to what fits.
        b = B.compute_budget(
            _claude_code_like_payload(), model="gx-max:balanced",
            context_limit=16_384, max_output_limit=4_096,
        )
        self.assertTrue(b.fits)
        self.assertEqual(b.requested_output_tokens, 32_000)
        self.assertLess(b.output_tokens, 32_000)
        self.assertLessEqual(b.output_tokens, b.max_output_limit)
        self.assertTrue(b.clamped)
        applied = B.apply_budget(_claude_code_like_payload(), b)
        self.assertLess(applied["max_tokens"], 32_000)
        # TIGHT: the pessimistic estimate leaves only a minimal allowance,
        # so the request goes out with a small budget and the engine's own
        # tokenizer decides (corrected once, never resent unchanged).
        self.assertLessEqual(b.output_tokens, B.MIN_USEFUL_OUTPUT)
        self.assertTrue(b.fits)  # engine decides, not refused

    def test_no_output_budget_large_window_is_left_to_engine(self):
        b = self.budget(_payload(320, 10))
        self.assertEqual(b.status, B.STATUS_OK)
        self.assertIsNone(b.output_tokens)

    def test_certain_overflow_is_refused(self):
        b = self.budget(_payload(3_200, 200))  # 200 000 optimistic chars
        self.assertEqual(b.status, B.STATUS_OVERFLOW)
        self.assertFalse(b.fits)
        self.assertEqual(b.error_code, B.ERROR_CODE)
        self.assertEqual(b.safe_output_tokens, 0)
        self.assertIsNone(b.output_tokens)
        msg = B.overflow_message(b)
        self.assertIn("context window", msg)
        self.assertIn("Shorten", msg)

    def test_borderline_is_tight_not_overflow(self):
        # optimistic says it fits, pessimistic is unsure -> engine decides
        b = self.budget(_payload(3_200, 100), min_output=1024)
        self.assertIn(b.status, (B.STATUS_TIGHT, B.STATUS_CLAMPED))
        self.assertTrue(b.fits)

    def test_error_payload_shape(self):
        b = self.budget(_payload(3_200, 200))
        err = B.error_payload(b, message="too long", attempts=1, elapsed_ms=12.5)
        self.assertEqual(err["error"]["code"], B.ERROR_CODE)
        self.assertFalse(err["error"]["retryable"])
        self.assertEqual(err["error"]["gx_budget"]["attempts"], 1)
        self.assertEqual(err["error"]["gx_budget"]["elapsed_ms"], 12.5)
        self.assertTrue(json.loads(json.dumps(err)))  # log-safe


class TestEngineFeedback(unittest.TestCase):
    def test_parse_vllm_exact_counts(self):
        b = B.compute_budget(
            _payload(3_200, 100) | {"max_tokens": 32_000},
            model="gx-max:long", context_limit=65_536, max_output_limit=32_768,
        )
        err = B.parse_context_error(
            "Error: this model's maximum context length is 65536 tokens. "
            "However, you requested 32000 output tokens and your prompt "
            "contains at least 33200 input tokens"
        )
        self.assertEqual(err.context_limit, 65_536)
        self.assertEqual(err.input_tokens, 33_200)
        # A refusal consistent with the engine's own arithmetic: the input
        # count leaves just enough for a corrected, smaller output.
        err = B.EngineContextError(65_536, 65_000)
        fixed = B.corrected_output(err, b)
        self.assertIsNotNone(fixed)
        self.assertGreaterEqual(fixed, B.MIN_RETRY_OUTPUT)
        self.assertLessEqual(fixed, 65_536 - 65_000 - B.EXACT_RETRY_MARGIN)

    def test_correction_is_bounded_by_request_and_ceiling(self):
        b = B.compute_budget(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1000},
            model="m", context_limit=8_192, max_output_limit=4_096,
        )
        err = B.EngineContextError(8_192, 7_600)
        fixed = B.corrected_output(err, b)
        self.assertLessEqual(fixed, 1000)  # never more than requested
        self.assertGreaterEqual(fixed, B.MIN_RETRY_OUTPUT)

    def test_no_retry_when_input_leaves_nothing(self):
        b = B.compute_budget(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1000},
            model="m", context_limit=8_192, max_output_limit=4_096,
        )
        self.assertIsNone(B.corrected_output(B.EngineContextError(8_192, 8_100), b))

    def test_no_retry_when_unknown_input_count(self):
        b = B.compute_budget({"messages": []}, model="m",
                             context_limit=8_192, max_output_limit=4_096)
        self.assertIsNone(B.corrected_output(B.EngineContextError(None, None), b))

    def test_no_retry_when_the_engine_already_refused_that_budget(self):
        b = B.compute_budget(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 500},
            model="m", context_limit=8_192, max_output_limit=4_096,
        )
        # b.output_tokens == 500 (fits, unclamped); engine says input leaves 600
        err = B.EngineContextError(8_192, 7_600)
        self.assertIsNone(B.corrected_output(err, b))

    def test_parse_unrelated_error_is_none(self):
        self.assertIsNone(B.parse_context_error("rate limit exceeded"))
        self.assertIsNone(B.parse_context_error(""))

    def test_parse_generic_context_error(self):
        e = B.parse_context_error("Prompt is too long: 90000 tokens > 65536 maximum")
        self.assertIsNotNone(e)
        self.assertEqual(B.parse_context_error("ContextWindowExceededError"), B.EngineContextError(None, None))


class TestProfileBudget(unittest.TestCase):
    """The V4.1 server glue: a profile's served window is the budget's window."""

    def setUp(self):
        self.registry = load_fixture_registry()
        self.cfg = Config(gxmax_max_output=32_768)

    def test_window_comes_from_the_profile(self):
        for name, spec in self.registry.profiles.items():
            b = profile_budget(_payload(100, 2) | {"max_tokens": 999}, self.registry, name, self.cfg)
            self.assertEqual(b.context_limit, spec.max_model_len)
            self.assertEqual(b.model, f"gx-max:{name}")

    def test_swarm_small_window_clamps_harder_than_fast(self):
        payload = _payload(3_200, 40) | {"max_tokens": 32_000}
        swarm = profile_budget(payload, self.registry, "swarm", self.cfg)
        fast = profile_budget(payload, self.registry, "fast", self.cfg)
        self.assertLess(swarm.context_limit, fast.context_limit)
        self.assertLessEqual(swarm.output_tokens, fast.output_tokens)

    def test_swarm_window_overflows_where_fast_does_not(self):
        payload = _payload(3_200, 550)  # ~1.76M chars: overflows swarm's 256k window
        swarm = profile_budget(payload, self.registry, "swarm", self.cfg)
        fast = profile_budget(payload, self.registry, "fast", self.cfg)
        self.assertFalse(swarm.fits)
        self.assertEqual(swarm.status, B.STATUS_OVERFLOW)
        self.assertTrue(fast.fits)

    def test_output_ceiling_from_config(self):
        b = profile_budget({"messages": [{"role": "user", "content": "hi"}]},
                           self.registry, "balanced", Config(gxmax_max_output=1_000))
        self.assertEqual(b.max_output_limit, 1_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
