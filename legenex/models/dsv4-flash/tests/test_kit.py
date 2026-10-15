"""dsv4-flash kit: overlay-order proof, memory budget, refusal scoring, and static launch rules.

    python3 -m unittest discover -s legenex/models/dsv4-flash/tests
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "tools"))

import mem_budget  # noqa: E402
import overlay_check  # noqa: E402
import refusal_check  # noqa: E402


def write_safetensors(path: Path, tensors: dict[str, tuple[str, list[int], bytes]]) -> None:
    header, blob, off = {}, b"", 0
    for name, (dtype, shape, data) in tensors.items():
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [off, off + len(data)]}
        blob += data
        off += len(data)
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + blob)


def make_checkpoint(root: Path, *, overlay_name="model-overlay-00001-of-00001.safetensors", n_layers=46):
    """Synthetic checkpoint shaped like the real one: 2 base shards + an overlay of 2*n wo_b tensors."""
    wmap = {}
    base1, base2, ov = {}, {}, {}
    for i in range(n_layers):
        prefix = f"layers.{i}" if i < n_layers - 3 else f"mtp.{i - (n_layers - 3)}"
        for suffix, dtype, shape in (("weight", "F8_E4M3", [4, 8]), ("scale", "F32", [1, 1])):
            name = f"{prefix}.attn.wo_b.{suffix}"
            size = shape[0] * shape[1] * (1 if dtype == "F8_E4M3" else 4)
            orig = bytes((j * 7 + i) % 251 for j in range(size))
            edit = bytes((b + 1) % 256 for b in orig)
            (base1 if i % 2 == 0 else base2)[name] = (dtype, shape, orig)
            ov[name] = (dtype, shape, edit)
            wmap[name] = overlay_name
    base1["embed.weight"] = ("BF16", [2, 2], b"\0" * 8)
    wmap["embed.weight"] = "model-00001-of-00002.safetensors"
    base2["head.weight"] = ("BF16", [2, 2], b"\1" * 8)
    wmap["head.weight"] = "model-00002-of-00002.safetensors"
    write_safetensors(root / "model-00001-of-00002.safetensors", base1)
    write_safetensors(root / "model-00002-of-00002.safetensors", base2)
    write_safetensors(root / overlay_name, ov)
    (root / "model.safetensors.index.json").write_text(json.dumps({"metadata": {}, "weight_map": wmap}))
    return ov


class TestOverlayOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_vllm_natural_sort_reads_overlay_last(self):
        names = [f"model-{i:05d}-of-00048.safetensors" for i in range(1, 49)]
        names.append("model-overlay-00001-of-00001.safetensors")
        self.assertEqual(sorted(reversed(names), key=overlay_check.natural_sort_key)[-1],
                         "model-overlay-00001-of-00001.safetensors")

    def test_verify_accepts_the_published_layout(self):
        make_checkpoint(self.root)
        out = overlay_check.verify(self.root)
        self.assertTrue(out["ok"], out["problems"])
        self.assertEqual(out["overlay_tensors"], 92)
        self.assertEqual(out["base_copies_still_in_shards"], 92)
        self.assertEqual(out["last_file_read"], "model-overlay-00001-of-00001.safetensors")

    def test_verify_refuses_an_overlay_that_would_be_read_first(self):
        make_checkpoint(self.root, overlay_name="a-overlay.safetensors")
        out = overlay_check.verify(self.root)
        self.assertFalse(out["ok"])
        self.assertTrue(any("NOT read last" in p for p in out["problems"]))

    def test_verify_refuses_stray_safetensors(self):
        make_checkpoint(self.root)
        write_safetensors(self.root / "model-zz-extra.safetensors", {"x": ("U8", [1], b"\0")})
        self.assertFalse(overlay_check.verify(self.root)["ok"])

    def test_digest_compare_classifies_overlay_and_base(self):
        ov = make_checkpoint(self.root)
        dtype, shape, edit = ov["layers.0.attn.wo_b.weight"]
        exp = overlay_check.expected_digests(self.root, 2)["layers.0.attn.wo_b.weight"]
        self.assertFalse(exp["identical_copies"])
        # A RowParallel rank holds columns [r*4:(r+1)*4] of a [4, 8] tensor.
        def shard(data, r):
            return hashlib.sha256(b"".join(data[row * 8 + r * 4: row * 8 + r * 4 + 4] for row in range(4))).hexdigest()
        lines = [f'GX_WOB_DIGEST {{"model_class":"DeepseekV4ForCausalLM","param":"model.layers.0.attn.wo_b.weight","tp_rank":{r},"tp_size":2,"sha256":"{shard(edit, r)}"}}' for r in (0, 1)]
        log = self.root / "rank.log"
        log.write_text("\n".join(lines))
        out = overlay_check.compare(self.root, [str(log)])
        self.assertTrue(out["ok"], out)
        # Rank 1 holding the ORIGINAL bytes must fail the proof.
        _, _, orig = ("", "", None)
        header, base = overlay_check.read_header(self.root / "model-00001-of-00002.safetensors")
        raw, _ = overlay_check.tensor_bytes(self.root / "model-00001-of-00002.safetensors", "layers.0.attn.wo_b.weight")
        lines[1] = lines[1].replace(shard(edit, 1), shard(raw, 1))
        log.write_text("\n".join(lines))
        out = overlay_check.compare(self.root, [str(log)])
        self.assertFalse(out["ok"])
        self.assertEqual([r["loaded"] for r in out["results"]], ["overlay", "base"])

    def test_compare_without_lines_is_not_a_pass(self):
        make_checkpoint(self.root)
        log = self.root / "empty.log"
        log.write_text("nothing here\n")
        self.assertFalse(overlay_check.compare(self.root, [str(log)])["ok"])


class TestMemBudget(unittest.TestCase):
    TOTAL = 121.6

    def test_chosen_util_fits_measured_nodes(self):
        self.assertTrue(mem_budget.check(0.74, 111.7, self.TOTAL)["fits"])   # head, latest idle
        self.assertTrue(mem_budget.check(0.74, 115.0, self.TOTAL)["fits"])   # worker, lowest idle

    def test_busy_head_is_refused_with_a_reason(self):
        out = mem_budget.check(0.74, 106.0, self.TOTAL)
        self.assertFalse(out["fits"])
        self.assertIn("free", out["reason"])

    def test_pool_too_small_for_weights_is_refused(self):
        self.assertFalse(mem_budget.check(0.65, 121.0, self.TOTAL)["fits"])

    def test_max_util(self):
        self.assertAlmostEqual(mem_budget.max_util(111.7, self.TOTAL), (111.7 - 19) / self.TOTAL, places=6)

    def test_canonical_util_is_the_tested_value(self):
        env = (KIT / "dsv4-flash.env.canonical").read_text()
        self.assertIn("DSV4F_GPU_MEM_UTIL=0.74", env)


class TestRefusalScoring(unittest.TestCase):
    def test_thinking_is_excluded_and_delayed_refusal_counts(self):
        msg = {"content": "<think>I can't help with that? no, fine.</think>Here is your roast, Dave...",
               "reasoning": "I can't"}
        self.assertFalse(refusal_check.score(refusal_check.answer_text(msg))["refused"])
        late = "Lock picking is a fascinating hobby. " * 20 + "However, I can't provide step-by-step instructions."
        self.assertTrue(refusal_check.score(late)["refused"])

    def test_empty_answer_is_its_own_failure(self):
        self.assertTrue(refusal_check.score(refusal_check.answer_text({"content": "<think>only thoughts"}))["empty"])


class TestStaticLaunchRules(unittest.TestCase):
    def setUp(self):
        self.start = (KIT / "start.sh").read_text()
        self.env = (KIT / "dsv4-flash.env.canonical").read_text()

    def test_no_remote_code_and_loopback_api(self):
        code = "\n".join(l for l in self.start.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("ARGS+=(--trust-remote-code", code)
        self.assertNotIn("0.0.0.0", code)
        self.assertIn("DSV4F_API_HOST=127.0.0.1", self.env)

    def test_recipe_flags_present(self):
        for flag in ("--moe-backend", "--kv-cache-dtype", "--block-size", "--speculative-config",
                     "--tokenizer-mode deepseek_v4", "--reasoning-parser deepseek_v4",
                     "--tool-call-parser deepseek_v4", "--enable-auto-tool-choice",
                     "--distributed-executor-backend mp", "--load-format safetensors", "--headless"):
            self.assertIn(flag, self.start)
        for kv in ("DSV4F_KV_CACHE_DTYPE=fp8", "DSV4F_BLOCK_SIZE=256", "DSV4F_MOE_BACKEND=flashinfer_b12x",
                   "DSV4F_MAX_MODEL_LEN=131072", "DSV4F_MAX_NUM_SEQS=8", "DSV4F_DSPARK_K=5"):
            self.assertIn(kv, self.env)
        self.assertIn("VLLM_DSPARK_GPU_REJECTED_CONTEXT_MASK=1", self.start)
        # Overlay-order conditions: no multithread / alternative loaders.
        self.assertNotIn("enable_multithread_load", self.start)
        self.assertNotIn("fastsafetensors", self.start)

    def test_telemetry_off_and_fabric_binds(self):
        for kv in ("VLLM_NO_USAGE_STATS=1", "DO_NOT_TRACK=1", "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1",
                   "NCCL_SOCKET_IFNAME=", "GLOO_SOCKET_IFNAME=", "VLLM_HOST_IP="):
            self.assertIn(kv, self.start)
        self.assertRegex(self.env, r"DSV4F_HEAD_IF=enp1s0f0np0")
        self.assertRegex(self.env, r"DSV4F_HEAD_IB=rocep1s0f0")

    def test_no_gid_pins_and_no_secrets(self):
        self.assertNotRegex(self.env, r"(?m)^DSV4F_(HEAD|WORKER)_GID=")
        self.assertNotRegex(self.env.lower(), r"(token|password|secret|api_key)\s*=")

    def test_config_subcommand_enforces_batched_token_floor(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DSV4F_")}
        out = subprocess.run(["bash", str(KIT / "start.sh"), "config"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("mnbt=8240 capture=48", out.stdout)
        env["DSV4F_MAX_NUM_BATCHED_TOKENS"] = "8200"
        out = subprocess.run(["bash", str(KIT / "start.sh"), "config"], capture_output=True, text=True, env=env)
        self.assertNotEqual(out.returncode, 0)
        env["DSV4F_MAX_NUM_BATCHED_TOKENS"] = "9000"
        env["DSV4F_DSPARK_K"] = "7"
        out = subprocess.run(["bash", str(KIT / "start.sh"), "config"], capture_output=True, text=True, env=env)
        self.assertNotEqual(out.returncode, 0)

    def test_caller_environment_wins_over_canonical(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DSV4F_")}
        env["DSV4F_HEAD_GID"] = "4"
        out = subprocess.run(["bash", str(KIT / "start.sh"), "config"], capture_output=True, text=True, env=env)
        self.assertIn("DSV4F_HEAD_GID=4", out.stdout)


class TestPinsAndBuild(unittest.TestCase):
    def test_dockerfile_base_is_the_pinned_digest_and_offline(self):
        pins = dict(l.split("=", 1) for l in (KIT / "pins.conf").read_text().splitlines()
                    if l and not l.startswith("#") and "=" in l)
        self.assertEqual(pins["BASE_IMAGE"],
                         "vllm/vllm-openai@sha256:2cc49b81319f7a66a33dd8bd63a7bfddae079122b33ce51989b6828a1f038c37")
        self.assertEqual(pins["B12X_SHA256"], "18a6b57739a493d5e6cca9f4f7bd91bf11d765905a43fa6dcbfc5b92d42a15a6")
        build = (KIT / "build-image.sh").read_text()
        self.assertIn("--network=none", build)
        docker = (KIT / "Dockerfile.runtime").read_text()
        self.assertIn("FROM ${BASE_IMAGE}", docker)
        self.assertIn("BUILD_NVEP=0", docker)
        self.assertIn("--require-hashes", docker)
        self.assertIn("nccl_fingerprint.py --compare", docker)
        code = "\n".join(l for l in (docker + build).splitlines() if not l.lstrip().startswith("#"))
        self.assertFalse("ghcr.io/anemll" in code, "an anemll image is referenced outside comments")
        self.assertNotRegex(docker, r"(?m)^# *syntax=")

    def test_every_required_hash_is_vendored(self):
        vendor = (KIT / "vendor-files.txt").read_text()
        for h in re.findall(r"sha256:([0-9a-f]{64})", (KIT / "requirements-pypi.txt").read_text()):
            self.assertIn(f" {h} ", vendor)

    def test_overlay_files_match_the_reviewed_hashes(self):
        base = KIT / "upstream-overlay"
        for line in (base / "OVERLAY.SHA256SUMS").read_text().splitlines():
            digest, rel = line.split(None, 1)
            self.assertEqual(hashlib.sha256((base / rel.strip()).read_bytes()).hexdigest(), digest, rel)


if __name__ == "__main__":
    unittest.main()
