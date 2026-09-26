"""B-032: gx-mini serves 32768 tokens per request slot (llama.cpp --ctx-size 65536 --parallel 2).

The tier table, the LiteLLM budget metadata and gx-auto routing must all use that real window, so a request that
cannot fit gx-mini is never sent there.
"""
import json
import re
import unittest
from pathlib import Path

from gx_orchestrator.classifier import route
from gx_orchestrator.tiers import TIERS, Tier

REPO = Path(__file__).resolve().parents[3]


def chat(words: int, model: str = "gx-auto") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello there " + "alpha bravo " * words}]}


class TestMiniRealWindow(unittest.TestCase):
    def test_tier_table_uses_the_slot_size(self):
        self.assertEqual(TIERS[Tier.MINI].max_context, 32_768)

    def test_config_files_agree_with_the_tier_table(self):
        node1 = (REPO / "legenex/gateway/llama-swap/node01.yaml").read_text()
        block = node1[node1.index("  gx-mini:"):]
        ctx = int(re.search(r"--ctx-size\s+(\d+)", block).group(1))
        parallel = int(re.search(r"--parallel\s+(\d+)", block).group(1))
        self.assertEqual(ctx // parallel, TIERS[Tier.MINI].max_context)
        litellm = (REPO / "legenex/gateway/litellm/config.yaml").read_text()
        mini = litellm[litellm.index("model_name: gx-mini"):litellm.index("model_name: gx-code")]
        max_in = int(re.search(r"max_input_tokens:\s*(\d+)", mini).group(1))
        max_out = int(re.search(r"max_output_tokens:\s*(\d+)", mini).group(1))
        self.assertLessEqual(max_in + max_out, TIERS[Tier.MINI].max_context)
        registry = json.loads((REPO / "legenex/models/registry.json").read_text())["aliases"]["gx-mini"]
        self.assertEqual(registry["context"], TIERS[Tier.MINI].max_context)

    def test_small_conversational_request_still_goes_to_mini(self):
        self.assertIs(route(chat(20)).tier, Tier.MINI)

    def test_oversized_request_is_never_routed_to_mini(self):
        # About 37k estimated tokens: more than gx-mini's 32768 window, less than gx-code's 65536. It must
        # escalate to the smallest tier that holds it (gx-code), never be sent to gx-mini.
        d = route(chat(10_000))
        self.assertIs(d.tier, Tier.CODE, d.reasons)
        self.assertIsNot(d.tier, Tier.MINI, d.reasons)
        self.assertGreaterEqual(TIERS[d.tier].max_context, d.features.total_context_needed, d.reasons)

    def test_request_between_the_old_and_real_window_is_not_sent_to_mini(self):
        # Sized (pessimistic estimate) between 32768 and the old 65536 mistake.
        for words in range(9_000, 12_000, 500):
            d = route(chat(words))
            if d.features.total_context_needed > TIERS[Tier.MINI].max_context:
                self.assertIsNot(d.tier, Tier.MINI, (words, d.features.total_context_needed, d.reasons))


if __name__ == "__main__":
    unittest.main()
