"""Tests for ops/bench/run_bench.py — hermetic (no network, no cluster).

Covers metric parsing (StreamAccumulator on fixture SSE lines), the JSONL
append format, and the prompt builders.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_bench as rb  # noqa: E402


class TestPromptBuilders(unittest.TestCase):
    def test_prose_prompt_is_deterministic_per_seed(self):
        a = rb.build_prose_prompt(8000, seed=42)
        b = rb.build_prose_prompt(8000, seed=42)
        self.assertEqual(a, b)

    def test_prose_prompt_scales_with_target(self):
        small = rb.build_prose_prompt(100)
        large = rb.build_prose_prompt(8000)
        self.assertLess(len(small), len(large) / 20)
        # rough token estimate for the large one (chars/4 heuristic)
        self.assertGreater(len(large) / 4, 4000)

    def test_prose_prompt_is_pure_filler_with_instruction(self):
        prompt = rb.build_prose_prompt(200)
        self.assertIn("synthetic passage", prompt)
        self.assertIn("done", prompt)

    def test_smoke_prompt_is_the_mia_smoke(self):
        self.assertIn("17", rb.SMOKE_PROMPT)
        self.assertIn("19", rb.SMOKE_PROMPT)

    def test_tool_call_request_shape(self):
        req = rb.build_tool_call_request()
        tool = req["tools"][0]["function"]
        self.assertEqual(tool["name"], "get_weather")
        self.assertEqual(tool["parameters"]["required"], ["city"])
        self.assertEqual(req["messages"][0]["role"], "user")


class TestStreamAccumulator(unittest.TestCase):
    """Fixture SSE lines from a fake streaming completion."""

    def _run_fixture(self, events: list[str]):
        acc = rb.StreamAccumulator()
        started = 0.0
        ttfb = ttft = last = None
        clock = [0.0]
        for line in events:
            clock[0] += 0.1
            now = clock[0]
            # mirror stream_chat: only real SSE data lines count as first byte
            if ttfb is None and line.strip().startswith("data:"):
                ttfb = now
            marks = acc.feed(line, now=now, started=started)
            if marks["ttft_ms"] is not None:
                if ttft is None:
                    ttft = now
                last = now
        return acc.metrics(started=started, ttfb=ttfb, ttft=ttft,
                           last_chunk=last, total_s=clock[0])

    def test_content_ttft_and_usage_parsed(self):
        events = [
            'data: {"choices": [{"delta": {"role": "assistant"}}]}',
            'data: {"choices": [{"delta": {"content": "323"}}]}',
            'data: {"choices": [{"delta": {"content": "!"}, "finish_reason": "stop"}]}',
            'data: {"usage": {"prompt_tokens": 12, "completion_tokens": 2, '
            '"prompt_tokens_details": {"cached_tokens": 8}}, "choices": []}',
            "data: [DONE]",
        ]
        m = self._run_fixture(events)
        self.assertEqual(m["content"], "323!")
        self.assertEqual(m["finish_reason"], "stop")
        self.assertEqual(m["prompt_tokens"], 12)
        self.assertEqual(m["completion_tokens"], 2)
        self.assertEqual(m["cached_tokens"], 8)
        # first content delta arrives at t=0.2s (second data line)
        self.assertEqual(m["ttft_ms"], 200)
        self.assertEqual(m["ttfb_ms"], 100)
        # queue wait proxy = time-to-first-byte (see README)
        self.assertEqual(m["queue_wait_ms"], 100)

    def test_decode_tps_derived_from_last_chunk(self):
        events = [
            'data: {"choices": [{"delta": {"content": "a"}}]}',
            'data: {"choices": [{"delta": {"content": "b"}}]}',
            'data: {"choices": [{"delta": {"content": "c"}}]}',
            'data: {"usage": {"prompt_tokens": 8000, "completion_tokens": 30}, "choices": []}',
            "data: [DONE]",
        ]
        m = self._run_fixture(events)
        # TTFT 100 ms, last content chunk at 300 ms -> 30 tok / 0.2 s = 150 tps
        self.assertEqual(m["decode_tps"], 150.0)
        # prefill = 8000 tok / 0.1 s = 80000 tps
        self.assertEqual(m["prefill_tps"], 80000.0)
        self.assertEqual(m["queue_wait_ms"], 100)

    def test_tool_call_deltas_collected(self):
        events = [
            'data: {"choices": [{"delta": {"tool_calls": [{"function": {"name": "get_weather"}}]}}]}',
            "data: [DONE]",
        ]
        m = self._run_fixture(events)
        self.assertEqual(m["tool_calls"][0]["function"]["name"], "get_weather")

    def test_junk_lines_are_ignored(self):
        events = [
            ": keep-alive comment",
            "not-an-sse-line",
            'data: {broken json',
            'data: {"choices": [{"delta": {"content": "ok"}}]}',
            "data: [DONE]",
        ]
        m = self._run_fixture(events)
        self.assertEqual(m["content"], "ok")
        self.assertEqual(m["prompt_tokens"], None)
        self.assertEqual(m["decode_tps"], None)

    def test_empty_stream_degrades(self):
        m = self._run_fixture(["data: [DONE]"])
        self.assertEqual(m["content"], "")
        self.assertIsNone(m["ttft_ms"])
        self.assertIsNone(m["decode_tps"])
        # the [DONE] byte is still a real first byte -> queue-wait proxy set
        self.assertEqual(m["queue_wait_ms"], 100)


class TestJsonlAppend(unittest.TestCase):
    def test_append_record_creates_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "results.jsonl"
            rec1 = rb.make_record(label="t", suite="quick", op="smoke", streams=1,
                                  profile="fast", extra={"smoke_ok": True})
            rec2 = rb.make_record(label="t", suite="quick", op="smoke", streams=1,
                                  profile="fast", error="boom")
            rb.append_records(path, [rec1, rec2])
            lines = path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            parsed = [json.loads(line) for line in lines]
            self.assertEqual(parsed[0]["ts"], rec1["ts"])
            self.assertTrue(parsed[0]["smoke_ok"])
            self.assertEqual(parsed[1]["error"], "boom")

    def test_record_schema_fields_are_stable(self):
        rec = rb.make_record(label="t", suite="quick", op="short_decode",
                             streams=1, profile="fast")
        expected = {
            "ts", "label", "suite", "op", "profile", "streams", "prompt_tokens",
            "completion_tokens", "cached_tokens", "ttft_ms", "prefill_tps",
            "decode_tps", "aggregate_tps", "queue_wait_ms", "total_ms", "error",
        }
        self.assertEqual(set(rec), expected)
        self.assertEqual(rec["error"], "")
        self.assertIsNone(rec["ttft_ms"])

    def test_record_copies_result_metrics(self):
        result = {
            "ttft_ms": 221, "prefill_tps": 995.0, "decode_tps": 31.6,
            "queue_wait_ms": 12, "total_ms": 3000,
            "prompt_tokens": 100, "completion_tokens": 64, "cached_tokens": 0,
        }
        rec = rb.make_record(label="t", suite="quick", op="short_decode",
                             streams=1, profile="fast", result=result)
        self.assertEqual(rec["ttft_ms"], 221)
        self.assertEqual(rec["decode_tps"], 31.6)
        self.assertEqual(rec["prompt_tokens"], 100)

    def test_append_record_single_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.jsonl"
            rb.append_record(path, rb.make_record(label="x", suite="quick",
                                                  op="run_summary", streams=0, profile="p"))
            content = path.read_text()
            self.assertTrue(content.endswith("\n"))
            self.assertEqual(len(content.strip().splitlines()), 1)
            json.loads(content)


class TestMemParsing(unittest.TestCase):
    def test_read_meminfo_fixture(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo", delete=False) as fh:
            fh.write("MemTotal: 126934440 kB\nMemAvailable: 61789456 kB\n")
            path = fh.name
        try:
            info = rb.read_meminfo(path)
            self.assertEqual(info["MemAvailable"], 61789456)
        finally:
            Path(path).unlink()

    def test_missing_meminfo_is_none(self):
        self.assertIsNone(rb.read_meminfo("/nonexistent/meminfo"))


class TestArgValidation(unittest.TestCase):
    def test_suite_choices(self):
        with self.assertRaises(SystemExit):
            rb.main(["--suite", "mega"])

    def test_label_recorded_in_records(self):
        rec = rb.make_record(label="matrix-fast", suite="quick", op="smoke",
                             streams=1, profile="fast")
        self.assertEqual(rec["label"], "matrix-fast")


if __name__ == "__main__":
    unittest.main()
