"""Tests for gx_orchestrator.profiles: registry v2 loading, validation and
the reasoning-level -> chat_template_kwargs mapping (ARCHITECTURE-V41 §2).
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator.profiles import (  # noqa: E402
    ALIAS_AUTO,
    ALIAS_DIRECT,
    RegistryError,
    load_registry,
    parse_registry,
)
from registry_fixtures import fixture_registry_dict, write_fixture_registry  # noqa: E402


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = write_fixture_registry(Path(self.tmp.name) / "registry.json")

    def test_fixture_loads_and_validates(self):
        reg = load_registry(self.path)
        self.assertEqual(sorted(reg.profiles), ["balanced", "custom", "deep", "fast", "long", "swarm"])
        self.assertEqual(reg.runtime("mia-dsv41").served_model_id, "DeepSeek-v4.1-Flash-EXL3")
        self.assertTrue(reg.production_model().uncensored)

    def test_production_model_is_the_gx_max_binding(self):
        reg = load_registry(self.path)
        # gx-max is bound to the UNCENSORED model: the production requirement.
        self.assertEqual(reg.alias(ALIAS_DIRECT).model, "dsv41-flash-exl3-uncensored")
        self.assertEqual(reg.alias(ALIAS_AUTO).mode, "auto")

    def test_profile_accessors(self):
        reg = load_registry(self.path)
        fast = reg.profile("fast")
        self.assertEqual(fast.max_num_seqs, 1)
        self.assertEqual(fast.spec_method, "dspark")
        self.assertEqual(fast.dspark_tokens, 3)
        self.assertEqual(fast.max_model_len, 600000)
        self.assertEqual(fast.reasoning_default, "medium")
        swarm = reg.profile("swarm")
        self.assertEqual(swarm.spec_method, "none")
        self.assertEqual(swarm.dspark_tokens, 0)
        # The bounded custom profile carries its ranges.
        custom = reg.profile("custom")
        self.assertEqual(custom.max_num_seqs_bounds, (1, 4))
        self.assertEqual(custom.max_model_len_bounds, (8192, 600000))

    def test_unknown_profile_has_a_clear_error(self):
        reg = load_registry(self.path)
        with self.assertRaises(RegistryError) as ctx:
            reg.profile("turbo")
        self.assertIn("known profiles", str(ctx.exception))

    def test_default_profile_is_balanced_then_any(self):
        reg = load_registry(self.path)
        self.assertEqual(reg.default_profile().name, "balanced")
        data = fixture_registry_dict()
        del data["profiles"]["balanced"]
        del data["profiles"]["fast"]
        del data["profiles"]["deep"]
        self.assertEqual(parse_registry(data).default_profile().name, "long")

    def test_nodes_and_fabric(self):
        reg = load_registry(self.path)
        worker = reg.worker_node()
        self.assertEqual(worker.name, "gx10-02")
        self.assertEqual(worker.fabric_ips, ("192.168.100.11", "192.168.101.11"))
        self.assertEqual(reg.head_node().role, "head")


class TestValidation(unittest.TestCase):
    """Every malformed registry must fail with a message that names the
    problem -- the orchestrator must never guess from a broken file."""

    def _bad(self, mutate):
        data = fixture_registry_dict()
        mutate(data)
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(data)
        return str(ctx.exception)

    def test_missing_file(self):
        with self.assertRaises(RegistryError) as ctx:
            load_registry("/nonexistent/registry.json")
        self.assertIn("cannot read registry", str(ctx.exception))

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text("{not json")
            with self.assertRaises(RegistryError) as ctx:
                load_registry(p)
            self.assertIn("not valid JSON", str(ctx.exception))

    def test_wrong_schema_is_rejected_by_name(self):
        msg = self._bad(lambda d: d.update(schema=1))
        self.assertIn("schema", msg)

    def test_missing_profiles_section(self):
        msg = self._bad(lambda d: d.pop("profiles"))
        self.assertIn("profiles", msg)

    def test_missing_alias(self):
        msg = self._bad(lambda d: d["aliases"].pop("gx-auto"))
        self.assertIn("gx-auto", msg)

    def test_alias_pointing_at_unknown_model(self):
        msg = self._bad(lambda d: d["aliases"]["gx-max"].update(model="nope"))
        self.assertIn("unknown model", msg)

    def test_profile_with_bad_spec_method(self):
        msg = self._bad(lambda d: d["profiles"]["fast"].update(spec_method="mystery"))
        self.assertIn("spec_method", msg)

    def test_dspark_profile_without_tokens(self):
        msg = self._bad(lambda d: d["profiles"]["fast"].pop("dspark_tokens"))
        self.assertIn("dspark_tokens", msg)

    def test_reasoning_mapping_gap(self):
        msg = self._bad(lambda d: d["reasoning"]["mapping"].pop("medium"))
        self.assertIn("medium", msg)

    def test_profile_default_reasoning_not_in_vocabulary(self):
        msg = self._bad(lambda d: d["profiles"]["fast"].update(reasoning_default="sideways"))
        self.assertIn("reasoning_default", msg)

    def test_bounded_custom_without_ranges(self):
        msg = self._bad(lambda d: d["profiles"]["custom"].update(max_num_seqs=2))
        self.assertIn("bounded", msg)

    def test_non_object_root(self):
        with self.assertRaises(RegistryError):
            parse_registry([1, 2, 3])


class TestReasoningMapping(unittest.TestCase):
    """none/minimal -> thinking off; named levels -> reasoning_effort;
    numeric 1-100 allowed; anything else is an error, never a default."""

    def setUp(self):
        self.reg = parse_registry(fixture_registry_dict())
        self.spec = self.reg.reasoning

    def test_thinking_off_levels(self):
        for level in ("none", "minimal"):
            self.assertEqual(self.spec.kwargs(level), {"enable_thinking": False})

    def test_named_effort_levels(self):
        expected = {"low": 50, "medium": 62, "high": 75, "xhigh": 90, "max": 100}
        for level, value in expected.items():
            self.assertEqual(self.spec.kwargs(level), {"reasoning_effort": value})

    def test_numeric_levels_in_range(self):
        self.assertEqual(self.spec.kwargs(1), {"reasoning_effort": 1})
        self.assertEqual(self.spec.kwargs(75), {"reasoning_effort": 75})
        self.assertEqual(self.spec.kwargs(100), {"reasoning_effort": 100})

    def test_numeric_string_is_accepted(self):
        # Headers arrive as strings; "75" must behave like 75.
        self.assertEqual(self.spec.kwargs("75"), {"reasoning_effort": 75})

    def test_out_of_range_and_junk_are_errors(self):
        for bad in (0, 101, -1, "turbo", "", None, True):
            with self.assertRaises(RegistryError):
                self.spec.kwargs(bad)

    def test_case_insensitive_names(self):
        self.assertEqual(self.spec.kwargs("HIGH"), {"reasoning_effort": 75})

    def test_registry_helper(self):
        self.assertEqual(self.reg.reasoning_kwargs("max"), {"reasoning_effort": 100})


if __name__ == "__main__":
    unittest.main(verbosity=2)
