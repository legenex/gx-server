"""D-053: raw <think> reasoning never reaches `content` (stream and non-stream)."""
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator.reasoning_guard import (  # noqa: E402
    ChoiceFilter, StreamGuard, guard_completion_body, thinking_requested,
)
from gx_orchestrator.sse import DeltaSplitter  # noqa: E402


def sse(deltas, finish=True, reasoning_key="reasoning"):
    out = b""
    for i, d in enumerate(deltas):
        delta = {"content": d} if isinstance(d, str) else d
        doc = {"id": "c", "object": "chat.completion.chunk", "model": "m",
               "choices": [{"index": 0, "delta": delta,
                            "finish_reason": "stop" if finish and i == len(deltas) - 1 else None}]}
        out += b"data: " + json.dumps(doc).encode() + b"\n\n"
    return out + b"data: [DONE]\n\n"


def run_stream(raw: bytes, *, thinking: bool, cuts=None, splitter=True):
    guard = StreamGuard(thinking=thinking)
    split = DeltaSplitter()
    pieces = []
    if cuts is None:
        cuts = [raw]
    for c in cuts:
        g = guard.feed(c)
        pieces.append(split.feed(g) if splitter else g)
    tail = guard.flush()
    pieces.append((split.feed(tail) + split.flush()) if splitter else tail)
    text = b"".join(pieces)
    content, reasoning, events = "", "", []
    for ev in text.split(b"\n\n"):
        if not ev.startswith(b"data: {"):
            continue
        doc = json.loads(ev[6:])
        events.append(doc)
        for ch in doc["choices"]:
            d = ch.get("delta") or {}
            content += d.get("content") or ""
            reasoning += (d.get("reasoning") or "") + (d.get("reasoning_content") or "")
    return content, reasoning, events, text


def random_cuts(raw: bytes, seed: int):
    rnd = random.Random(seed)
    cuts, i = [], 0
    while i < len(raw):
        n = rnd.randint(1, 9)
        cuts.append(raw[i:i + n])
        i += n
    return cuts


class TestThinkingRequested(unittest.TestCase):
    def test_dialects(self):
        self.assertFalse(thinking_requested({"thinking": False}))
        self.assertFalse(thinking_requested({"enable_thinking": False}))
        self.assertTrue(thinking_requested({"thinking": True, "reasoning_effort": "max"}))
        self.assertTrue(thinking_requested({"reasoning_effort": 62}))


class TestStreamGuard(unittest.TestCase):
    def test_live_leak_shape_unclosed_think_at_offset_zero(self):
        raw = sse(["<think>We need answer: ", "the user wants a parser. ", "Let me write"])
        content, reasoning, _, _ = run_stream(raw, thinking=True)
        self.assertEqual(content, "")
        self.assertEqual(reasoning, "We need answer: the user wants a parser. Let me write")

    def test_tags_split_across_byte_chunks_keep_first_answer_token(self):
        raw = sse(["<thi", "nk>We need ", "to add.</th", "ink>1, 2", ", 3"])
        for seed in range(40):
            content, reasoning, _, text = run_stream(raw, thinking=True, cuts=random_cuts(raw, seed))
            self.assertEqual(content, "1, 2, 3", seed)
            self.assertEqual(reasoning, "We need to add.", seed)
            self.assertNotIn(b"think>", text.replace(b'"reasoning"', b""), seed)

    def test_mixed_delta_is_split_for_litellm(self):
        _, _, events, _ = run_stream(sse(["<think>x</think>Hello"]), thinking=True)
        deltas = [e["choices"][0]["delta"] for e in events]
        # Never one delta carrying both reasoning and content (D-052).
        self.assertFalse(any(d.get("reasoning") and d.get("content") for d in deltas))
        self.assertEqual("".join(d.get("content", "") for d in deltas), "Hello")

    def test_bare_close_tag_when_engine_sent_no_reasoning(self):
        raw = sse(["The user asks", " for 2+2.", "</think>", "\n\n4"])
        content, reasoning, _, _ = run_stream(raw, thinking=True)
        self.assertEqual(reasoning, "The user asks for 2+2.")
        self.assertEqual(content, "\n\n4")

    def test_engine_reasoning_present_passes_content_byte_for_byte(self):
        raw = sse([{"reasoning": "think think"}, {"content": "Hello"}, {"content": " world"}])
        content, reasoning, _, text = run_stream(raw, thinking=True, splitter=False)
        self.assertEqual((content, reasoning), ("Hello world", "think think"))
        self.assertEqual(text, raw)

    def test_thinking_off_plain_answer_is_untouched(self):
        raw = sse(["Hi", " there", "!"])
        content, _, _, text = run_stream(raw, thinking=False, splitter=False)
        self.assertEqual(content, "Hi there!")
        self.assertEqual(text, raw)

    def test_literal_tags_inside_an_answer_pass_through(self):
        # Review repros (L1): answers ABOUT tags must reach content verbatim.
        for answer in ("Use the <think> tag to wrap reasoning.",
                       'pattern = r"<think>(.*?)</think>"',
                       'head, tail = s.split("</think>")'):
            for thinking, lead in ((False, []), (True, [{"reasoning": "r"}])):
                raw = sse(lead + [answer[:9], answer[9:]])
                content, reasoning, _, _ = run_stream(raw, thinking=thinking)
                self.assertEqual(content, answer, (answer, thinking))
                self.assertEqual(reasoning, "r" if lead else "", (answer, thinking))
            out = json.loads(guard_completion_body(json.dumps({"choices": [{"index": 0, "message": {
                "role": "assistant", "content": answer}}]}).encode(), thinking=False))
            self.assertEqual(out["choices"][0]["message"]["content"], answer)

    def test_tags_after_a_closed_block_are_answer_text(self):
        raw = sse(["<think>plan</think>", "Use ", "<think>", " tags"])
        content, reasoning, _, _ = run_stream(raw, thinking=True)
        self.assertEqual((content, reasoning), ("Use <think> tags", "plan"))

    def test_held_text_is_flushed_when_no_close_tag_ever_comes(self):
        # Thinking requested, the model answered directly, no finish_reason event.
        raw = sse(["Plain answer", " without tags"], finish=False)
        content, reasoning, _, _ = run_stream(raw, thinking=True)
        self.assertEqual((content, reasoning), ("Plain answer without tags", ""))

    def test_reasoning_content_key_is_reused(self):
        raw = sse([{"reasoning_content": "a"}, {"content": "<think>b</think>c"}])
        _, _, events, _ = run_stream(raw, thinking=True, splitter=False)
        keys = set()
        for e in events:
            keys |= set(e["choices"][0]["delta"])
        self.assertIn("reasoning_content", keys)
        self.assertNotIn("reasoning", keys)

    def test_abort_drops_half_event(self):
        g = StreamGuard(thinking=True)
        g.feed(b'data: {"choices":[{"index":0,"delta":{"content":"<think>x')
        g.abort()
        self.assertEqual(g.flush(), b"")


class TestNonStream(unittest.TestCase):
    def body(self, content, **msg):
        return json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": content, **msg},
                                        "finish_reason": "stop"}]}).encode()

    def test_unclosed_think_moves_to_reasoning(self):
        out = json.loads(guard_completion_body(self.body("<think>We need answer: lots"), thinking=True))
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["content"], "")
        self.assertEqual(msg["reasoning"], "We need answer: lots")

    def test_closed_block_and_answer(self):
        out = json.loads(guard_completion_body(self.body("<think>a</think>323"), thinking=False))
        self.assertEqual(out["choices"][0]["message"]["content"], "323")
        self.assertEqual(out["choices"][0]["message"]["reasoning"], "a")

    def test_clean_body_is_returned_unchanged(self):
        body = self.body("323", reasoning="x")
        self.assertIs(guard_completion_body(body, thinking=True), body)

    def test_bare_close_with_thinking(self):
        out = json.loads(guard_completion_body(self.body("thoughts</think>answer"), thinking=True))
        self.assertEqual(out["choices"][0]["message"]["content"], "answer")
        self.assertEqual(out["choices"][0]["message"]["reasoning"], "thoughts")


class TestChoiceFilter(unittest.TestCase):
    def test_short_answer_that_looks_like_a_tag_prefix(self):
        f = ChoiceFilter(thinking=False)
        self.assertEqual(f.feed("<th"), ("", ""))
        self.assertEqual(f.finish(), ("<th", ""))

    def test_less_than_sign_in_code_is_kept(self):  # noqa: D102
        f = ChoiceFilter(thinking=False)
        c1, _ = f.feed("if a <")
        c2, _ = f.feed(" b: pass")
        c3, _ = f.finish()
        self.assertEqual(c1 + c2 + c3, "if a < b: pass")


class TestServerRelay(unittest.TestCase):
    """End to end through the real Handler: the leak never reaches the client."""

    def test_stream_and_non_stream_through_orchestrator(self):
        from server_harness import OrchestratorHarness  # noqa: PLC0415

        class _H(OrchestratorHarness):
            def runTest(self):  # noqa: N802
                pass
        h = _H()
        h.setUp()
        try:
            h.upstream.script.append({"stream": ["<thi", "nk>We need answer: ", "x</th", "ink>Hello"]})
            status, _, raw = h.chat({"model": "gx-max", "stream": True,
                                     "messages": [{"role": "user", "content": "q"}]}, raw=True)
            self.assertEqual(status, 200)
            content = reasoning = ""
            for ev in raw.split(b"\n\n"):
                if ev.startswith(b"data: {"):
                    d = json.loads(ev[6:])["choices"][0]["delta"]
                    content += d.get("content") or ""
                    reasoning += d.get("reasoning") or ""
            self.assertEqual(content, "Hello")
            self.assertEqual(reasoning, "We need answer: x")
            h.upstream.script.append({"status": 200, "json": {
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "<think>never closed"},
                             "finish_reason": "length"}]}})
            status, _, body = h.chat({"model": "gx-max", "messages": [{"role": "user", "content": "q"}]})
            self.assertEqual(status, 200)
            self.assertEqual(body["choices"][0]["message"]["content"], "")
            self.assertEqual(body["choices"][0]["message"]["reasoning"], "never closed")
        finally:
            h.doCleanups()


if __name__ == "__main__":
    unittest.main()
