"""D-052: deltas mixing reasoning and content are split before LiteLLM sees them."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator.sse import DeltaSplitter, split_event  # noqa: E402


def _event(delta, **extra):
    doc = {"id": "c1", "object": "chat.completion.chunk",
           "choices": [{"index": 0, "delta": delta, "finish_reason": None, **extra}]}
    return b"data: " + json.dumps(doc).encode()


def _events(raw: bytes):
    return [json.loads(e[5:]) for e in raw.split(b"\n\n") if e.startswith(b"data: {")]


class TestSplitEvent(unittest.TestCase):
    def test_mixed_delta_becomes_reasoning_then_content(self):
        out = _events(split_event(_event({"reasoning": ".", "content": "1"})))
        self.assertEqual([e["choices"][0]["delta"] for e in out],
                         [{"reasoning": "."}, {"content": "1"}])

    def test_reasoning_content_key_and_finish_reason_on_last(self):
        ev = _event({"role": "assistant", "reasoning_content": "x", "content": "y"}, finish_reason="stop")
        first, second = _events(split_event(ev))
        self.assertEqual(first["choices"][0]["delta"], {"role": "assistant", "reasoning_content": "x"})
        self.assertIsNone(first["choices"][0]["finish_reason"])
        self.assertEqual(second["choices"][0]["delta"], {"content": "y"})
        self.assertEqual(second["choices"][0]["finish_reason"], "stop")

    def test_other_events_pass_through_byte_for_byte(self):
        for ev in (_event({"content": "1"}), _event({"reasoning": "a"}),
                   _event({"reasoning": "", "content": "z"}), b"data: [DONE]",
                   b": keep-alive", b'data: {"broken json, "content", "reasoning"'):
            self.assertEqual(split_event(ev), ev + b"\n\n")


class TestDeltaSplitter(unittest.TestCase):
    def test_events_split_across_arbitrary_chunk_boundaries(self):
        stream = (_event({"reasoning": "think"}) + b"\n\n"
                  + _event({"reasoning": ".", "content": "1"}) + b"\n\n"
                  + _event({"content": ", 2"}) + b"\n\ndata: [DONE]\n\n")
        for size in (1, 3, 7, 64, len(stream)):
            sp = DeltaSplitter()
            out = b"".join(sp.feed(stream[i:i + size]) for i in range(0, len(stream), size)) + sp.flush()
            deltas = [e["choices"][0]["delta"] for e in _events(out)]
            self.assertEqual("".join(d.get("content", "") for d in deltas), "1, 2", size)
            self.assertEqual("".join(d.get("reasoning", "") for d in deltas), "think.", size)
            self.assertFalse(any("content" in d and "reasoning" in d for d in deltas))
            self.assertTrue(out.endswith(b"data: [DONE]\n\n"))

    def test_incomplete_tail_is_held_then_flushed_unchanged(self):
        sp = DeltaSplitter()
        self.assertEqual(sp.feed(b"data: {\"partial"), b"")
        self.assertEqual(sp.flush(), b"data: {\"partial")


if __name__ == "__main__":
    unittest.main()
