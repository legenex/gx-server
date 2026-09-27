"""Validate the shipped registry.json against the schema-2 loader.

Run: python3 -m unittest discover -s legenex/models/tests -t .
(or: python3 -m unittest legenex.models.tests.test_registry from the repo root)

Checks the shipped file is valid, then re-checks the contract invariants the
orchestrator and dashboard rely on: required keys, IP formats, profile bounds
(max_num_seqs 1..4), reasoning mapping completeness, and that both public
aliases reference a real model and runtime. Also proves the validator itself
rejects broken registries (negative tests on mutated copies).
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registry  # noqa: E402 - stdlib-only loader/validator

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "registry.json"


def _shipped() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


class ShippedRegistryTest(unittest.TestCase):
    """The file in the tree must validate, and hold the documented contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reg = _shipped()
        errors = registry.validate(cls.reg)
        cls.errors = errors

    def test_valid(self) -> None:
        self.assertEqual(self.errors, [])

    def test_load_file(self) -> None:
        loaded = registry.load(str(REGISTRY_PATH))
        self.assertEqual(loaded["schema"], 2)

    def test_schema_is_2(self) -> None:
        self.assertEqual(self.reg["schema"], 2)

    def test_required_top_level_keys(self) -> None:
        for key in ("schema", "cluster", "nodes", "runtimes", "models",
                    "aliases", "profiles", "reasoning", "capabilities"):
            self.assertIn(key, self.reg, f"missing top-level key {key!r}")

    def test_cluster_names_the_nodes(self) -> None:
        cluster = self.reg["cluster"]
        self.assertEqual(cluster["name"], "legenex-dual-gx10")
        self.assertIn(cluster["head"], self.reg["nodes"])
        self.assertIn(cluster["worker"], self.reg["nodes"])

    def test_node_ips_are_valid_ipv4(self) -> None:
        for name, node in self.reg["nodes"].items():
            for key in ("lan_ip", "tailscale_ip"):
                self.assertRegex(node[key], r"^\d+\.\d+\.\d+\.\d+$",
                                 f"{name}.{key} is not dotted-quad: {node[key]!r}")
            for rail, ip in node["fabric"].items():
                self.assertRegex(ip, r"^\d+\.\d+\.\d+\.\d+$",
                                 f"{name}.fabric.{rail} is not dotted-quad: {ip!r}")
            self.assertTrue(node["hcas"], f"{name} has no HCAs")
            self.assertTrue(node["ssh"], f"{name} has no ssh target")

    def test_runtime_pinned(self) -> None:
        self.assertIn("mia-dsv41", self.reg["runtimes"])
        rt = self.reg["runtimes"]["mia-dsv41"]
        self.assertEqual(rt["commit"], "6f7d1590ad49a2b8995188e45d7b9db31e677452")
        self.assertEqual(rt["image"],
                         "ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks:2.9bpw")
        self.assertEqual(rt["api"], "http://127.0.0.1:8888/v1")  # loopback only
        self.assertEqual(rt["served_model_id"], "DeepSeek-v4.1-Flash-EXL3")

    def test_both_models_pinned_with_packs(self) -> None:
        models = self.reg["models"]
        self.assertEqual(
            set(models),
            {"dsv41-flash-exl3-stock", "dsv41-flash-exl3-uncensored"},
        )
        stock = models["dsv41-flash-exl3-stock"]
        unc = models["dsv41-flash-exl3-uncensored"]
        self.assertEqual(stock["revision"], "64ba41b6c916a587db06eae2e19b7845f7be6e6b")
        self.assertEqual(unc["revision"], "8a27b35fc5b145fa05ee965c7d7b243b047915f7")
        self.assertEqual(stock["path"], "/srv/models/dsv41/model")
        self.assertEqual(unc["path"], "/srv/models/dsv41/uncensored")
        self.assertFalse(stock["uncensored"])
        self.assertTrue(unc["uncensored"])
        notes = unc["serving_notes"]
        self.assertEqual(notes["gpu_mem_util"], 0.85)
        self.assertEqual(notes["kv_bytes"], 1073741824)
        self.assertEqual(notes["max_num_batched_tokens"], 2048)
        self.assertEqual(notes["vllm_sparse_indexer_max_logits_mb"], 256)

    def test_public_aliases_only_and_bound(self) -> None:
        self.assertEqual(set(self.reg["aliases"]), {"gx-max", "gx-auto"})
        for name, alias in self.reg["aliases"].items():
            self.assertIn(alias["model"], self.reg["models"],
                          f"{name} references unknown model")
            self.assertIn(alias["runtime"], self.reg["runtimes"],
                          f"{name} references unknown runtime")
            self.assertIn(alias["mode"], ("direct", "auto"))
            self.assertTrue(alias["description"])
            self.assertEqual(alias["model"], "dsv41-flash-exl3-uncensored")
        self.assertEqual(self.reg["aliases"]["gx-max"]["mode"], "direct")
        self.assertEqual(self.reg["aliases"]["gx-auto"]["mode"], "auto")

    def test_six_profiles_present_with_bounds(self) -> None:
        profiles = self.reg["profiles"]
        self.assertEqual(
            set(profiles),
            {"fast", "balanced", "swarm", "deep", "long", "custom"},
        )
        for name, profile in profiles.items():
            seqs = profile["max_num_seqs"]
            if profile.get("bounded"):
                self.assertIsInstance(seqs, list, f"{name}: bounded needs a range")
                lo, hi = seqs
                self.assertLessEqual(lo, hi)
                self.assertGreaterEqual(lo, 1)
                self.assertLessEqual(hi, 4)
                mlo, mhi = profile["max_model_len"]
                self.assertLessEqual(mlo, mhi)
            else:
                self.assertIsInstance(seqs, int)
                self.assertGreaterEqual(seqs, 1, f"{name}.max_num_seqs below 1")
                self.assertLessEqual(seqs, 4, f"{name}.max_num_seqs above 4")
                self.assertIsInstance(profile["max_model_len"], int)
            # spec_method is optional (custom omits it per the contract).
            self.assertIn(profile.get("spec_method", "dspark"), ("dspark", "none"))
            self.assertIn(profile["reasoning_default"], self.reg["reasoning"]["levels"])
            self.assertTrue(profile["target"])

    def test_reasoning_mapping_valid(self) -> None:
        reasoning = self.reg["reasoning"]
        levels = reasoning["levels"]
        self.assertEqual(set(reasoning["mapping"]), set(levels),
                         "mapping must cover every level exactly")
        lo, hi = reasoning["numeric_range"]
        self.assertLess(lo, hi)
        for level, params in reasoning["mapping"].items():
            self.assertTrue(params, f"mapping for {level!r} is empty")

    def test_capabilities_booleans(self) -> None:
        caps = self.reg["capabilities"]
        for key in ("vision", "tools", "structured_output", "reasoning"):
            self.assertIsInstance(caps[key], bool)

    def test_no_secret_shaped_values(self) -> None:
        """Public repo: no credential-looking strings anywhere in the file."""
        text = REGISTRY_PATH.read_text(encoding="utf-8").lower()
        for marker in ("sk-", "api_key=", "password=", "bearer "):
            self.assertNotIn(marker, text)


class ValidatorRejectsTest(unittest.TestCase):
    """The validator must reject broken registrics with precise messages."""

    def _errors(self, **mutate) -> list[str]:
        data = copy.deepcopy(_shipped())
        for dotted, value in mutate.items():
            node: object = data
            keys = dotted.split(".")
            for key in keys[:-1]:
                assert isinstance(node, dict)
                node = node[key]
            assert isinstance(node, dict)
            node[keys[-1]] = value
        return registry.validate(data)

    def test_bad_ip(self) -> None:
        errors = self._errors(**{"nodes.gx10-01.lan_ip": "10.60.21.999"})
        self.assertTrue(any("lan_ip" in e and "not a valid IPv4" in e for e in errors))

    def test_bad_revision(self) -> None:
        errors = self._errors(**{"models.dsv41-flash-exl3-stock.revision": "main"})
        self.assertTrue(any("revision" in e for e in errors))

    def test_alias_unknown_model(self) -> None:
        errors = self._errors(**{"aliases.gx-max.model": "gx-mini"})
        self.assertTrue(any("unknown model" in e for e in errors))

    def test_alias_unknown_runtime(self) -> None:
        errors = self._errors(**{"aliases.gx-auto.runtime": "llama-swap"})
        self.assertTrue(any("unknown runtime" in e for e in errors))

    def test_bad_alias_mode(self) -> None:
        errors = self._errors(**{"aliases.gx-max.mode": "fallback"})
        self.assertTrue(any("mode" in e for e in errors))

    def test_profile_seqs_out_of_range(self) -> None:
        errors = self._errors(**{"profiles.fast.max_num_seqs": 8})
        self.assertTrue(any("max_num_seqs" in e and "1..4" in e for e in errors))

    def test_profile_bad_reasoning_default(self) -> None:
        errors = self._errors(**{"profiles.balanced.reasoning_default": "ultra"})
        self.assertTrue(any("reasoning_default" in e for e in errors))

    def test_reasoning_mapping_gap(self) -> None:
        errors = self._errors(**{"reasoning.mapping.minimal": None})
        # setting the entry to None fails its own check; remove instead
        data = copy.deepcopy(_shipped())
        del data["reasoning"]["mapping"]["minimal"]
        errors = registry.validate(data)
        self.assertTrue(any("missing entries" in e for e in errors))

    def test_missing_profile(self) -> None:
        data = copy.deepcopy(_shipped())
        del data["profiles"]["swarm"]
        errors = registry.validate(data)
        self.assertTrue(any("swarm" in e and "missing" in e for e in errors))

    def test_wrong_schema(self) -> None:
        errors = self._errors(**{"schema": 1})
        self.assertTrue(any("schema" in e for e in errors))

    def test_serving_note_bounds(self) -> None:
        errors = self._errors(
            **{"models.dsv41-flash-exl3-uncensored.serving_notes.gpu_mem_util": 1.7}
        )
        self.assertTrue(any("gpu_mem_util" in e for e in errors))

    def test_loads_rejects_bad_json(self) -> None:
        with self.assertRaises(registry.RegistryError):
            registry.loads("{not json")
        with self.assertRaises(registry.RegistryError):
            registry.loads(json.dumps({"schema": 2}))


if __name__ == "__main__":
    unittest.main()
