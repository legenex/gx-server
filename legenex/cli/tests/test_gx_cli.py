"""Tests for the gx CLI (legenex/cli/gx.py).

Hermetic: only pure functions and fixtures are tested -- meminfo parsing,
the GID-table fixture logic, the reasoning ladder, registry normalization
for both schemas, argparse wiring/validation, and output formatting helpers.
No network, no cluster, no docker.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gx  # noqa: E402


def write_temp(content: str, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as fh:
        fh.write(content)
        return Path(fh.name)


# ---------------------------------------------------------------------------
# meminfo parsing
# ---------------------------------------------------------------------------


class TestReadMeminfo(unittest.TestCase):
    def test_parses_real_looking_meminfo(self):
        sample = (
            "MemTotal:       126934440 kB\n"
            "MemFree:         5000000 kB\n"
            "MemAvailable:   117440512 kB\n"
            "SwapTotal:       66060288 kB\n"
        )
        path = write_temp(sample, ".meminfo")
        try:
            info = gx.read_meminfo(str(path))
            self.assertEqual(info["MemTotal"], 126934440)
            self.assertEqual(info["MemAvailable"], 117440512)
        finally:
            path.unlink()

    def test_tolerates_junk_lines(self):
        path = write_temp("garbage line\nMemAvailable:   42 kB\nalso bad: x kB\n", ".meminfo")
        try:
            info = gx.read_meminfo(str(path))
            self.assertEqual(info, {"MemAvailable": 42})
        finally:
            path.unlink()

    def test_missing_file_returns_none(self):
        self.assertIsNone(gx.read_meminfo("/nonexistent/meminfo"))


class TestMemAvailableGib(unittest.TestCase):
    def test_converts_kib_to_gib(self):
        orig = gx.read_meminfo
        try:
            gx.read_meminfo = lambda p="/proc/meminfo": {"MemAvailable": 1048576}
            self.assertEqual(gx.mem_available_gib(), 1.0)
        finally:
            gx.read_meminfo = orig


# ---------------------------------------------------------------------------
# GID table fixture (doctor fabric check)
# ---------------------------------------------------------------------------


def make_fake_sys(
    root: Path,
    *,
    active: bool = True,
    gid3_zero: bool = False,
    with_ports: bool = True,
) -> Path:
    """Build a minimal /sys/class/infiniband-shaped tree."""
    hca = root / "rocep1s0f0" / "ports" / "1"
    hca.mkdir(parents=True)
    if with_ports:
        (hca / "state").write_text("4: ACTIVE\n" if active else "2: DOWN\n")
        gids = hca / "gids"
        gids.mkdir()
        (gids / "0").write_text("0000:0000:0000:0000:0000:0000:0000:0000 fe80::scope\n")
        if gid3_zero:
            (gids / "3").write_text("0000:0000:0000:0000:0000:0000:0000:0000  scope\n")
        else:
            (gids / "3").write_text("fe80:0000:0000:0000:0a0a:0a0a:0a0a:0a0a  scope\n")
    return root / "rocep1s0f0"


class TestGidTable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gx-gid-"))

    def tearDown(self):
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        self.tmp.rmdir()

    def test_active_port_with_valid_gid3_passes(self):
        hca = make_fake_sys(self.tmp)
        ok, detail = gx._gid_table_ok(hca, 3)
        self.assertTrue(ok)
        self.assertIn("gid3 ok", detail)

    def test_all_zero_gid3_fails(self):
        hca = make_fake_sys(self.tmp, gid3_zero=True)
        ok, detail = gx._gid_table_ok(hca, 3)
        self.assertFalse(ok)
        self.assertIn("non-zero GID", detail)

    def test_no_ports_fails(self):
        hca = self.tmp / "rocep1s0f0"
        hca.mkdir(parents=True)
        ok, _ = gx._gid_table_ok(hca, 3)
        self.assertFalse(ok)

    def test_missing_gids_dir_fails(self):
        import shutil

        hca = make_fake_sys(self.tmp)
        shutil.rmtree(hca / "ports" / "1" / "gids")
        ok, _ = gx._gid_table_ok(hca, 3)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# reasoning ladder
# ---------------------------------------------------------------------------


class TestReasoningLadder(unittest.TestCase):
    def test_none_and_minimal_disable_thinking(self):
        for level in ("none", "minimal"):
            kwargs = gx.reasoning_kwargs(level)
            self.assertEqual(kwargs, {"enable_thinking": False})
            self.assertNotIn("reasoning_effort", kwargs)

    def test_efforts_match_the_pinned_ladder(self):
        expected = {"low": 50, "medium": 62, "high": 75, "xhigh": 90, "max": 100}
        for level, effort in expected.items():
            self.assertEqual(
                gx.reasoning_kwargs(level),
                {"enable_thinking": True, "reasoning_effort": effort},
            )

    def test_unknown_level_raises_gxerror(self):
        with self.assertRaises(gx.GxError):
            gx.reasoning_kwargs("ultra")

    def test_all_levels_are_valid_parser_choices(self):
        parser = gx.build_parser()
        for level in gx.REASONING_LEVELS:
            args = parser.parse_args(["start", "--reasoning", level])
            self.assertEqual(args.reasoning, level)


# ---------------------------------------------------------------------------
# registry normalization (schema 1 + schema 2)
# ---------------------------------------------------------------------------

SCHEMA2_SAMPLE = {
    "schema": 2,
    "cluster": {"name": "legenex-dual-gx10", "head": "gx10-01", "worker": "gx10-02"},
    "nodes": {
        "gx10-01": {"role": "head", "user": "legenex", "lan_ip": "10.60.21.37",
                    "fabric": {"rail1": "192.168.100.10"}, "hcas": ["rocep1s0f0"]},
    },
    "profiles": {
        "fast": {"max_num_seqs": 1, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "medium"},
        "deep": {"max_num_seqs": 2, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "max"},
    },
    "models": {
        "dsv41-flash-exl3-stock": {
            "source": "Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw",
            "revision": "64ba41b6c916a587db06eae2e19b7845f7be6e6b",
            "path": "/srv/models/dsv41/model", "uncensored": False, "quant": "exl3-2.9bpw-mul1",
            "max_context": 600000,
        },
        "dsv41-flash-exl3-uncensored": {
            "source": "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw",
            "revision": "8a27b35fc5b145fa05ee965c7d7b243b047915f7",
            "path": "/srv/models/dsv41/uncensored", "uncensored": True, "quant": "exl3-2.9bpw-mul1",
            "max_context": 262144,
        },
    },
}

SCHEMA1_SAMPLE = {
    "schema": 1,
    "aliases": {
        "gx-mini": {
            "repository": "HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive",
            "revision": "c09d", "path": "/srv/models/gguf/x",
            "uncensored": "yes: aggressive refusal removal",
        },
        "gx-reason": {
            "repository": "wyattearp/Qwen3.8-27B-Uncensored-NVFP4",
            "revision": "91ec", "path": "/srv/models/vllm/y",
            "uncensored": None,
        },
    },
}


class TestRegistryCards(unittest.TestCase):
    def test_schema2_models_normalize(self):
        cards = gx.registry_model_cards(SCHEMA2_SAMPLE)
        by_id = {c["id"]: c for c in cards}
        self.assertTrue(by_id["dsv41-flash-exl3-uncensored"]["uncensored"])
        self.assertFalse(by_id["dsv41-flash-exl3-stock"]["uncensored"])
        self.assertEqual(by_id["dsv41-flash-exl3-stock"]["revision"],
                         "64ba41b6c916a587db06eae2e19b7845f7be6e6b")

    def test_schema1_aliases_normalize(self):
        cards = gx.registry_model_cards(SCHEMA1_SAMPLE)
        by_id = {c["id"]: c for c in cards}
        self.assertTrue(by_id["gx-mini"]["uncensored"])       # "yes: ..." string
        self.assertFalse(by_id["gx-reason"]["uncensored"])    # None

    def test_profiles_and_nodes_helpers(self):
        self.assertEqual(set(gx.registry_profiles(SCHEMA2_SAMPLE)), {"fast", "deep"})
        self.assertIn("gx10-01", gx.registry_nodes(SCHEMA2_SAMPLE))
        self.assertEqual(gx.registry_profiles(SCHEMA1_SAMPLE), {})

    def test_broken_registry_shapes_do_not_crash(self):
        self.assertEqual(gx.registry_model_cards({"schema": 2}), [])
        self.assertEqual(gx.registry_model_cards({"models": "nope"}), [])
        self.assertEqual(gx.registry_nodes({}), {})


# ---------------------------------------------------------------------------
# load_registry errors
# ---------------------------------------------------------------------------


class TestLoadRegistry(unittest.TestCase):
    def test_missing_registry_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = gx.REGISTRY_PATH
            gx.REGISTRY_PATH = Path(tmp) / "nope.json"
            try:
                with self.assertRaises(gx.GxError) as ctx:
                    gx.load_registry()
                self.assertIn("registry not found", str(ctx.exception))
            finally:
                gx.REGISTRY_PATH = old

    def test_invalid_json_is_a_clear_error(self):
        path = write_temp("{ not json", ".json")
        old = gx.REGISTRY_PATH
        gx.REGISTRY_PATH = path
        try:
            with self.assertRaises(gx.GxError) as ctx:
                gx.load_registry()
            self.assertIn("not valid JSON", str(ctx.exception))
        finally:
            gx.REGISTRY_PATH = old
            path.unlink()


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


class TestFormatting(unittest.TestCase):
    def test_fmt_bytes(self):
        self.assertEqual(gx.fmt_bytes(500), "500 B")
        self.assertEqual(gx.fmt_bytes(2048), "2.0 KiB")
        self.assertEqual(gx.fmt_bytes(1024**3), "1.0 GiB")
        self.assertEqual(gx.fmt_bytes(None), "?")

    def test_friendly_error_wraps_http_and_url_errors(self):
        import urllib.error

        err = gx._friendly_error(urllib.error.HTTPError(
            "url", 500, "boom", None, None), "GET /health")  # type: ignore[arg-type]
        self.assertIn("HTTP 500", str(err))
        err = gx._friendly_error(urllib.error.URLError("refused"), "GET /health")
        self.assertIn("cannot reach the server", str(err))


# ---------------------------------------------------------------------------
# argparse wiring / arg validation
# ---------------------------------------------------------------------------


class TestParser(unittest.TestCase):
    def setUp(self):
        self.parser = gx.build_parser()

    def test_every_documented_subcommand_exists(self):
        expected = {
            "status", "doctor", "start", "stop", "restart", "drain", "max", "auto",
            "profile", "queue", "requests", "logs", "benchmark", "models", "nodes",
            "storage", "backup", "update",
        }
        choices = set(self.parser._subparsers._group_actions[0].choices)  # noqa: SLF001
        self.assertEqual(choices, expected)

    def test_start_defaults_and_overrides(self):
        args = self.parser.parse_args(["start"])
        self.assertIsNone(args.profile)
        self.assertEqual(args.reasoning, "medium")
        args = self.parser.parse_args(["start", "--profile", "deep", "--reasoning", "max"])
        self.assertEqual(args.profile, "deep")
        self.assertEqual(args.reasoning, "max")

    def test_bad_reasoning_level_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["start", "--reasoning", "ultra"])
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_benchmark_suite_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["benchmark", "--suite", "mega"])
        self.assertEqual(ctx.exception.code, 2)

    def test_requests_last_is_int(self):
        self.assertEqual(self.parser.parse_args(["requests", "--last", "5"]).last, 5)

    def test_subcommand_is_required(self):
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args([])
        self.assertEqual(ctx.exception.code, 2)

    def test_main_routes_to_function_and_catches_gxerror(self):
        # build_parser() reads the module attribute at call time, so patching
        # the subcommand function here routes main() into the fake.
        def fake_status(args):
            raise gx.GxError("boom")

        orig = gx.cmd_status
        gx.cmd_status = fake_status
        try:
            rc = gx.main(["status"])
        finally:
            gx.cmd_status = orig
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
