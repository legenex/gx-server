"""Wan 2.2 LoRA support (D-040): discovery, header validation, noise
classification, managed workflow generation and the router API.

Hermetic: temporary LoRA roots with synthetic safetensors headers (tensor
data is never needed), a fake ComfyUI, no GPU.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router import lora_chain  # noqa: E402
from gx_media_router.comfy import failure_code, rejection_code  # noqa: E402
from gx_media_router.config import Config  # noqa: E402
from gx_media_router.lora_catalog import (HeaderError, LoraCatalog, LoraRoot, analyse,  # noqa: E402
                                          classify_noise, pair_stem, parse_roots, read_header)
from gx_media_router.server import build_server  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

from tests.test_router import FakeComfy  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"
T2V = "wan22-t2v-a14b-uncensored"


def wan_keys(dim: int = 5120, blocks: int = 40, rank: int = 16, style: str = "peft") -> dict:
    header: dict = {}
    for b in range(blocks):
        for part in ("self_attn.q", "cross_attn.k", "ffn.0"):
            if style == "peft":
                header[f"diffusion_model.blocks.{b}.{part}.lora_A.weight"] = [rank, dim]
                header[f"diffusion_model.blocks.{b}.{part}.lora_B.weight"] = [dim, rank]
            else:
                flat = f"lora_unet_blocks_{b}_{part.replace('.', '_')}"
                header[f"{flat}.lora_down.weight"] = [rank, dim]
                header[f"{flat}.lora_up.weight"] = [dim, rank]
    return header


def write_safetensors(path: Path, shapes: dict, metadata: dict | None = None, *, pad: int = 8) -> Path:
    header: dict = {k: {"dtype": "F16", "shape": v, "data_offsets": [0, 0]} for k, v in shapes.items()}
    if metadata is not None:
        header["__metadata__"] = metadata
    raw = json.dumps(header).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\x00" * pad)
    return path


class LoraComfy(FakeComfy):
    """FakeComfy that also answers /object_info for LoraLoaderModelOnly."""

    def __init__(self) -> None:
        super().__init__()
        self.lora_names: list[str] | None = []
        self.object_info_error: Exception | None = None

    def node_input_options(self, node_class, input_name):
        if self.object_info_error:
            raise self.object_info_error
        return None if self.lora_names is None else list(self.lora_names)


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_header_is_parsed_without_reading_tensor_data(self):
        p = write_safetensors(self.dir / "ok.safetensors", wan_keys(blocks=2), {"ss_output_name": "x"})
        header, raw = read_header(p, p.stat().st_size)
        self.assertIn("__metadata__", header)
        self.assertEqual(len(raw), struct.unpack("<Q", p.read_bytes()[:8])[0])

    def test_invalid_files_are_rejected(self):
        cases = {
            "tiny": b"\x01\x02",
            "too_long": struct.pack("<Q", 2**40) + b"{}" + b"\x00" * 16,
            "beyond_file": struct.pack("<Q", 500) + b"{}" + b"\x00" * 16,
            "not_json": struct.pack("<Q", 10) + b"not json!!" + b"\x00" * 8,
            "not_object": struct.pack("<Q", 2) + b"[]" + b"\x00" * 8,
            "html": struct.pack("<Q", 6) + b"<html>" + b"\x00" * 8,
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                p = self.dir / f"{name}.safetensors"
                p.write_bytes(data)
                with self.assertRaises(HeaderError):
                    read_header(p, p.stat().st_size)

    def test_offsets_outside_the_file_are_rejected(self):
        header = {"a.lora_A.weight": {"dtype": "F16", "shape": [1, 1], "data_offsets": [0, 4096]}}
        raw = json.dumps(header).encode()
        p = self.dir / "bad.safetensors"
        p.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\x00" * 8)
        with self.assertRaises(HeaderError):
            read_header(p, p.stat().st_size)


class AnalyseTests(unittest.TestCase):
    def verdict(self, shapes):
        header = {k: {"dtype": "F16", "shape": v, "data_offsets": [0, 0]} for k, v in shapes.items()}
        return analyse(header)

    def test_wan_14b_peft_and_kohya_are_compatible(self):
        for style in ("peft", "kohya"):
            with self.subTest(style=style):
                v = self.verdict(wan_keys(style=style))
                self.assertEqual((v["family"], v["compatibility"]), ("wan-14b", "compatible"))
                self.assertEqual((v["hidden_dim"], v["blocks"], v["rank"]), (5120, 40, 16))
                self.assertEqual(v["key_format"], style)

    def test_other_wan_sizes_are_incompatible(self):
        self.assertEqual(self.verdict(wan_keys(dim=3072, blocks=30))["family"], "wan-5b")
        self.assertEqual(self.verdict(wan_keys(dim=3072, blocks=30))["compatibility"], "incompatible")
        self.assertEqual(self.verdict(wan_keys(dim=1536, blocks=30))["compatibility"], "incompatible")

    def test_non_wan_models_are_incompatible(self):
        qwen = {"diffusion_model.transformer_blocks.0.img_mlp.net.0.proj.lora_A.weight": [32, 3072]}
        flux = {"lora_unet_double_blocks_0_img_attn_qkv.lora_down.weight": [16, 3072]}
        sdxl = {"lora_unet_input_blocks_4_1_proj_in.lora_down.weight": [16, 640]}
        for shapes, family in ((qwen, "qwen-image"), (flux, "flux"), (sdxl, "stable-diffusion")):
            with self.subTest(family=family):
                v = self.verdict(shapes)
                self.assertEqual((v["family"], v["compatibility"]), (family, "incompatible"))

    def test_unrecognised_layouts_are_unknown_not_guessed(self):
        v = self.verdict({"model.layers.0.mlp.lora_A.weight": [8, 4096]})
        self.assertEqual(v["compatibility"], "unknown")
        i2v = wan_keys(blocks=2)
        i2v["diffusion_model.blocks.0.cross_attn.k_img.lora_A.weight"] = [16, 5120]
        self.assertEqual(self.verdict(i2v)["compatibility"], "unknown")
        self.assertEqual(self.verdict({})["compatibility"], "incompatible")

    def test_the_installed_lightx2v_layout_is_compatible(self):
        # Shapes observed on gx10-02 (wan2.2_t2v_lightx2v_4steps_lora_v1.1_*): rank 64, kohya down/up
        # under diffusion_model.*, 40 blocks, ffn width 13824, plus scalar alphas.
        shapes = {}
        for b in range(40):
            for part, (o, i) in {"self_attn.q": (5120, 5120), "ffn.0": (13824, 5120), "ffn.2": (5120, 13824)}.items():
                shapes[f"diffusion_model.blocks.{b}.{part}.lora_down.weight"] = [64, i]
                shapes[f"diffusion_model.blocks.{b}.{part}.lora_up.weight"] = [o, 64]
                shapes[f"diffusion_model.blocks.{b}.{part}.alpha"] = []
        v = self.verdict(shapes)
        self.assertEqual((v["family"], v["compatibility"], v["rank"]), ("wan-14b", "compatible", 64))


class NoiseTests(unittest.TestCase):
    def test_filename_markers(self):
        cases = {
            "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors": "high",
            "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors": "low",
            "Wan2.2_LightX2V_high_n54vv.safetensors": "high",
            "Wan2.2_LightX2V_low_n54vv.safetensors": "low",
            "MyStyle-HighNoise.safetensors": "high",
            "MyStyleLowNoise.safetensors": "low",
            "my style HN v2.safetensors": "high",
            "my.style.LN.safetensors": "low",
            "mystyle-highnoise.safetensors": "high",
            "yellow_noise_texture.safetensors": "unknown",
            "hollow_knight.safetensors": "unknown",
            "high_and_low_noise.safetensors": "unknown",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify_noise(name, {})[0], expected)

    def test_folders_and_conflicts(self):
        self.assertEqual(classify_noise("wan22/high_noise/style.safetensors", {})[:2], ("high", "folder"))
        self.assertEqual(classify_noise("wan22/low_noise/style.safetensors", {})[:2], ("low", "folder"))
        self.assertEqual(classify_noise("wan22/general/style.safetensors", {})[:2], ("general", "folder"))
        noise, source, reason, _ = classify_noise("wan22/high_noise/style_low_noise.safetensors", {})
        self.assertEqual((noise, source), ("unknown", None))
        self.assertIn("conflicting", reason)

    def test_metadata_marker_and_conflict(self):
        self.assertEqual(classify_noise("style.safetensors", {"ss_output_name": "style_high_noise"})[:2],
                         ("high", "metadata"))
        self.assertEqual(classify_noise("style_low.safetensors", {"ss_output_name": "style_high"})[0], "unknown")

    def test_pair_keys_match_across_separator_and_case_variants(self):
        self.assertEqual(pair_stem("Style_High_Noise_v2"), pair_stem("style-low-noise-v2"))
        self.assertEqual(pair_stem("StyleHN"), pair_stem("style_LN"))
        k_high = classify_noise("wan22/high_noise/Style_v1.safetensors", {})[3]
        k_low = classify_noise("wan22/low_noise/style-v1.safetensors", {})[3]
        self.assertEqual(k_high, k_low)
        self.assertNotEqual(classify_noise("a/style.safetensors", {})[3], classify_noise("b/style.safetensors", {})[3])


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.shared = base / "shared"
        self.video = base / "video"
        self.outside = base / "outside"
        for d in (self.shared, self.video, self.outside):
            d.mkdir()
        self.catalog = LoraCatalog([LoraRoot("shared", self.shared, "/srv/models/shared/loras"),
                                    LoraRoot("video", self.video, "/srv/models/video/loras")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_recursive_discovery_names_match_comfyui(self):
        write_safetensors(self.video / "root_high_noise.safetensors", wan_keys(blocks=2))
        write_safetensors(self.video / "wan22" / "paired" / "Style_high_noise.safetensors", wan_keys(blocks=2))
        write_safetensors(self.video / "wan22" / "paired" / "Style_low_noise.safetensors", wan_keys(blocks=2))
        write_safetensors(self.video / "wan22" / "general" / "grain.safetensors", wan_keys(blocks=2))
        (self.video / "notes.txt").write_text("not a model")
        (self.video / "old.ckpt").write_bytes(b"x" * 32)
        write_safetensors(self.video / ".hidden.safetensors", wan_keys(blocks=1))
        write_safetensors(self.video / ".git" / "x.safetensors", wan_keys(blocks=1))
        result = self.catalog.rescan(["root_high_noise.safetensors", "wan22/paired/Style_high_noise.safetensors",
                                      "wan22/paired/Style_low_noise.safetensors"])
        names = [f["name"] for f in result["data"]]
        self.assertEqual(names, ["root_high_noise.safetensors", "wan22/general/grain.safetensors",
                                 "wan22/paired/Style_high_noise.safetensors",
                                 "wan22/paired/Style_low_noise.safetensors"])
        by = {f["name"]: f for f in result["data"]}
        self.assertEqual(by["wan22/general/grain.safetensors"]["noise"], "general")
        self.assertFalse(by["wan22/general/grain.safetensors"]["comfy_visible"])
        self.assertFalse(by["wan22/general/grain.safetensors"]["usable"])
        self.assertTrue(by["wan22/paired/Style_high_noise.safetensors"]["usable"])
        self.assertEqual(by["wan22/paired/Style_high_noise.safetensors"]["path"],
                         "/srv/models/video/loras/wan22/paired/Style_high_noise.safetensors")
        self.assertEqual(by["wan22/paired/Style_high_noise.safetensors"]["pair_key"],
                         by["wan22/paired/Style_low_noise.safetensors"]["pair_key"])

    def test_invalid_files_are_listed_as_invalid(self):
        (self.video / "broken.safetensors").write_bytes(b"\x00" * 64)
        entry = self.catalog.rescan()["data"][0]
        self.assertFalse(entry["valid"])
        self.assertEqual(entry["compatibility"], "incompatible")
        self.assertTrue(entry["error"])

    def test_name_collision_follows_comfyui_search_order(self):
        write_safetensors(self.shared / "same.safetensors", wan_keys(blocks=1))
        write_safetensors(self.video / "same.safetensors", wan_keys(blocks=1))
        files = self.catalog.rescan()["data"]
        shared = next(f for f in files if f["root"] == "shared")
        video = next(f for f in files if f["root"] == "video")
        self.assertIsNone(shared["shadowed_by"])
        self.assertEqual(video["shadowed_by"], shared["id"])
        self.assertEqual(self.catalog.get("same.safetensors").root, "shared")

    def test_symlinks_outside_the_roots_are_not_followed(self):
        write_safetensors(self.outside / "secret.safetensors", wan_keys(blocks=1))
        os.symlink(self.outside, self.video / "escape")
        os.symlink(self.outside / "secret.safetensors", self.video / "link.safetensors")
        result = self.catalog.rescan()
        names = {f["name"]: f for f in result["data"]}
        self.assertNotIn("escape/secret.safetensors", names)
        self.assertFalse(names["link.safetensors"]["valid"])
        self.assertTrue(any("outside" in p for p in result["problems"]))

    def test_missing_root_is_reported_and_files_disappear_after_removal(self):
        p = write_safetensors(self.video / "gone.safetensors", wan_keys(blocks=1))
        self.assertEqual(len(self.catalog.rescan()["data"]), 1)
        p.unlink()
        self.assertEqual(self.catalog.rescan()["data"], [])
        cat = LoraCatalog([LoraRoot("video", Path(self.tmp.name) / "nope", "/x")])
        self.assertTrue(cat.rescan()["problems"])

    def test_parse_roots(self):
        roots = parse_roots("shared=/srv/loras/shared=/srv/models/shared/loras;video=/a=/b")
        self.assertEqual([r.label for r in roots], ["shared", "video"])
        for bad in ("x=/a", "X=/a=/b", "a=rel=/b", "a=/a=/b;a=/c=/d"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_roots(bad)
        self.assertEqual(parse_roots(""), [])


class ChainTests(unittest.TestCase):
    """Workflow generation against the shipped known-good template."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        for name in ("pair_high_noise", "pair_low_noise", "two_high_noise", "two_low_noise", "hi_only_HN"):
            write_safetensors(root / f"{name}.safetensors", wan_keys(blocks=2))
        write_safetensors(root / "wan22" / "general" / "grain.safetensors", wan_keys(blocks=2))
        write_safetensors(root / "mystery.safetensors", {"model.layers.0.lora_A.weight": [8, 4096]})
        write_safetensors(root / "qwen.safetensors",
                          {"diffusion_model.transformer_blocks.0.img_mlp.lora_A.weight": [8, 3072]})
        (root / "corrupt.safetensors").write_bytes(b"\x00" * 64)
        cls.catalog = LoraCatalog([LoraRoot("video", root, "/srv/models/video/loras")])
        cls.catalog.loader_available = True
        cls.catalog.rescan()
        cls.workflow = WorkflowRegistry(WORKFLOW_DIR).get(T2V)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, spec):
        chains = lora_chain.parse_request(spec)
        lora_chain.validate(chains, self.catalog)
        return self.workflow.build({"prompt": "p", "seed": 1}, chains)

    def chain(self, graph, branch):
        return lora_chain.summary(graph, self.workflow.lora_chains)[branch]

    def test_zero_loras_leave_the_known_good_graph_unchanged(self):
        base = self.workflow.build({"prompt": "p", "seed": 1})
        self.assertEqual(self.build(None), base)
        self.assertEqual(self.build({"high": [], "low": []}), base)
        self.assertEqual(base["7"]["inputs"]["model"], ["5", 0])
        self.assertEqual(base["8"]["inputs"]["model"], ["6", 0])
        self.assertFalse(any(k.isdigit() and int(k) >= 1000 for k in base))

    def test_one_pair_goes_to_its_own_branch(self):
        g = self.build({"high": [{"name": "pair_high_noise.safetensors", "strength": 0.8}],
                        "low": [{"name": "pair_low_noise.safetensors", "strength": 0.7}]})
        self.assertEqual(g["1000"], {"class_type": "LoraLoaderModelOnly", "_meta": g["1000"]["_meta"],
                                     "inputs": {"model": ["5", 0], "lora_name": "pair_high_noise.safetensors",
                                                "strength_model": 0.8}})
        self.assertEqual(g["2000"]["inputs"], {"model": ["6", 0], "lora_name": "pair_low_noise.safetensors",
                                               "strength_model": 0.7})
        self.assertEqual(g["7"]["inputs"]["model"], ["1000", 0])
        self.assertEqual(g["8"]["inputs"]["model"], ["2000", 0])
        # the rest of the known-good graph is untouched
        self.assertEqual(g["12"]["inputs"]["model"], ["7", 0])
        self.assertEqual(g["13"]["inputs"]["model"], ["8", 0])
        self.assertEqual(g["5"]["inputs"]["model"], ["3", 0])
        high = self.chain(g, "high")
        self.assertEqual([c["lora_name"] for c in high],
                         ["Wan2.2_LightX2V_high_n54vv.safetensors", "pair_high_noise.safetensors"])
        self.assertEqual(lora_chain.summary(g, self.workflow.lora_chains)["high_model"],
                         "wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors")

    def test_multiple_pairs_keep_order_and_independent_strengths(self):
        g = self.build({"high": [{"name": "two_high_noise.safetensors", "strength": 0.5},
                                 {"name": "pair_high_noise.safetensors", "strength": 0.25}],
                        "low": [{"name": "pair_low_noise.safetensors", "strength": 1.5},
                                {"name": "two_low_noise.safetensors", "strength": 0.0}]})
        self.assertEqual([(c["lora_name"], c["strength"]) for c in self.chain(g, "high")][1:],
                         [("two_high_noise.safetensors", 0.5), ("pair_high_noise.safetensors", 0.25)])
        self.assertEqual([(c["lora_name"], c["strength"]) for c in self.chain(g, "low")][1:],
                         [("pair_low_noise.safetensors", 1.5), ("two_low_noise.safetensors", 0.0)])
        self.assertEqual(g["1001"]["inputs"]["model"], ["1000", 0])
        self.assertEqual(g["7"]["inputs"]["model"], ["1001", 0])

    def test_high_only_low_only_and_different_counts(self):
        g = self.build({"high": [{"name": "hi_only_HN.safetensors", "strength": 0.6}]})
        self.assertEqual(g["7"]["inputs"]["model"], ["1000", 0])
        self.assertEqual(g["8"]["inputs"]["model"], ["6", 0])
        g = self.build({"low": [{"name": "pair_low_noise.safetensors", "strength": 0.6}]})
        self.assertEqual(g["7"]["inputs"]["model"], ["5", 0])
        self.assertEqual(g["8"]["inputs"]["model"], ["2000", 0])
        g = self.build({"high": [{"name": "pair_high_noise.safetensors", "strength": 0.6},
                                 {"name": "two_high_noise.safetensors", "strength": 0.6}],
                        "low": [{"name": "pair_low_noise.safetensors", "strength": 0.6}]})
        self.assertEqual((len(self.chain(g, "high")), len(self.chain(g, "low"))), (3, 2))

    def test_generation_is_deterministic(self):
        spec = {"high": [{"name": "pair_high_noise.safetensors", "strength": 0.8}],
                "low": [{"name": "pair_low_noise.safetensors", "strength": 0.8}]}
        self.assertEqual(json.dumps(self.build(spec), sort_keys=True), json.dumps(self.build(spec), sort_keys=True))
        v1 = lora_chain.version(T2V, self.workflow.graph)
        self.assertEqual(v1, lora_chain.version(T2V, self.workflow.graph))
        self.assertRegex(v1, r"^gx-wan-lora/1\+wan22-t2v-a14b-uncensored@[0-9a-f]{12}$")

    def test_general_lora_on_both_branches_needs_explicit_opt_in(self):
        both = {"high": [{"name": "wan22/general/grain.safetensors", "strength": 0.4}],
                "low": [{"name": "wan22/general/grain.safetensors", "strength": 0.3}]}
        with self.assertRaises(lora_chain.LoraRequestError) as ctx:
            self.build(both)
        self.assertEqual(ctx.exception.code, "lora_shared_not_allowed")
        for branch in both.values():
            branch[0]["shared"] = True
        g = self.build(both)
        self.assertEqual(g["1000"]["inputs"]["lora_name"], g["2000"]["inputs"]["lora_name"])
        self.assertNotEqual(g["1000"]["inputs"]["model"], g["2000"]["inputs"]["model"])

    def test_refusals(self):
        cases = [
            ({"high": [{"name": "nope.safetensors", "strength": 1}]}, "lora_not_found"),
            ({"high": [{"name": "../etc/passwd.safetensors", "strength": 1}]}, "lora_not_found"),
            ({"high": [{"name": "/srv/models/x.safetensors", "strength": 1}]}, "lora_not_found"),
            ({"high": [{"name": "model.ckpt", "strength": 1}]}, "lora_unsupported_file"),
            ({"high": [{"name": "corrupt.safetensors", "strength": 1}]}, "lora_invalid_file"),
            ({"high": [{"name": "qwen.safetensors", "strength": 1}]}, "lora_incompatible"),
            ({"high": [{"name": "mystery.safetensors", "strength": 1}]}, "lora_unknown_compatibility"),
            ({"high": [{"name": "pair_low_noise.safetensors", "strength": 1}]}, "lora_branch_mismatch"),
            ({"low": [{"name": "pair_high_noise.safetensors", "strength": 1}]}, "lora_branch_mismatch"),
            ({"high": [{"name": "pair_high_noise.safetensors", "strength": 1, "shared": True}],
              "low": [{"name": "pair_high_noise.safetensors", "strength": 1, "shared": True}]},
             "lora_branch_mismatch"),
            ({"high": [{"name": "pair_high_noise.safetensors", "strength": 1.6}]}, "lora_invalid_strength"),
            ({"high": [{"name": "pair_high_noise.safetensors", "strength": -0.1}]}, "lora_invalid_strength"),
            ({"high": [{"name": "pair_high_noise.safetensors", "strength": True}]}, "lora_invalid_strength"),
            ({"high": [{"name": "pair_high_noise.safetensors", "strength": 1}] * 2}, "lora_duplicate"),
            ({"high": [{"name": "pair_high_noise.safetensors", "strength": 1}] * 9}, "lora_too_many"),
            ({"high": [{"name": "pair_high_noise.safetensors", "strength": 1, "node": "3"}]},
             "lora_invalid_request"),
            ({"middle": []}, "lora_invalid_request"),
            ([], "lora_invalid_request"),
        ]
        for spec, code in cases:
            with self.subTest(code=code, spec=str(spec)[:80]):
                with self.assertRaises(lora_chain.LoraRequestError) as ctx:
                    self.build(spec)
                self.assertEqual(ctx.exception.code, code)

    def test_unknown_compatibility_can_be_allowed_explicitly(self):
        g = self.build({"high": [{"name": "mystery.safetensors", "strength": 0.5, "allow_unknown": True}]})
        self.assertEqual(g["1000"]["inputs"]["lora_name"], "mystery.safetensors")

    def test_missing_loader_node_is_refused(self):
        self.catalog.loader_available = False
        try:
            with self.assertRaises(lora_chain.LoraRequestError) as ctx:
                self.build({"high": [{"name": "pair_high_noise.safetensors", "strength": 1}]})
            self.assertEqual(ctx.exception.code, "lora_loader_unavailable")
        finally:
            self.catalog.loader_available = True

    def test_graph_checker_catches_cross_branch_wiring(self):
        g = self.build({"high": [{"name": "pair_high_noise.safetensors", "strength": 1}],
                        "low": [{"name": "pair_low_noise.safetensors", "strength": 1}]})
        g["2000"]["inputs"]["model"] = ["1000", 0]
        with self.assertRaises(lora_chain.LoraRequestError):
            lora_chain.check_graph(g, self.workflow.lora_chains)
        g2 = self.build(None)
        g2["8"]["inputs"]["model"] = ["99", 0]
        with self.assertRaises(lora_chain.LoraRequestError):
            lora_chain.check_graph(g2, self.workflow.lora_chains)

    def test_template_chain_spec_validation(self):
        graph = self.workflow.graph
        self.assertEqual(lora_chain.parse_chain_spec(None, graph), {})
        for bad in ({"high": {"after": "5", "into": ["7.model"]}},
                    {"high": {"after": "5", "into": ["8.model"]}, "low": {"after": "6", "into": ["8.model"]}},
                    {"high": {"after": "5", "into": ["7.model"]}, "low": {"after": "5", "into": ["7.model"]}},
                    {"high": {"after": "99", "into": ["7.model"]}, "low": {"after": "6", "into": ["8.model"]}}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    lora_chain.parse_chain_spec(bad, graph)
        self.assertTrue(WorkflowRegistry(WORKFLOW_DIR).get(T2V).public()["loras"])
        self.assertFalse(WorkflowRegistry(WORKFLOW_DIR).get("wan22-i2v-a14b-uncensored").public()["loras"])


class ErrorCodeTests(unittest.TestCase):
    def test_failure_and_rejection_codes(self):
        self.assertEqual(failure_code("node 12 (KSamplerAdvanced): torch.OutOfMemoryError: CUDA out of memory"),
                         "out_of_memory")
        self.assertEqual(failure_code("node 3: RuntimeError: shape mismatch"), "execution_error")
        self.assertEqual(rejection_code({"1000": {"errors": [{"details": "lora_name: 'x' not in list"}]}}),
                         "lora_not_visible")
        self.assertEqual(rejection_code({"3": {"errors": [{"details": "unet_name: 'y' not in list"}]}}),
                         "model_unavailable")
        self.assertEqual(rejection_code({"error": {"type": "invalid_prompt",
                                                   "message": "node 1000 does not exist"}}), "node_unavailable")
        self.assertEqual(rejection_code({"12": {"errors": [{"details": "cfg too high"}]}}), "workflow_rejected")


class LoraApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "video"
        write_safetensors(root / "wan22" / "paired" / "Style_high_noise.safetensors", wan_keys(blocks=2))
        write_safetensors(root / "wan22" / "paired" / "Style_low_noise.safetensors", wan_keys(blocks=2))
        cls.cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key="test-key",
                         lora_roots=f"video={root}=/srv/models/video/loras",
                         resource_retry_seconds=2.0)
        cls.comfy = LoraComfy()
        cls.comfy.lora_names = ["wan22/paired/Style_high_noise.safetensors",
                                "wan22/paired/Style_low_noise.safetensors"]
        cls.service = MediaService(cls.cfg, cls.comfy, WorkflowRegistry(WORKFLOW_DIR))
        cls.server = build_server(cls.cfg, cls.service)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, key="test-key"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    SPEC = {"high": [{"name": "wan22/paired/Style_high_noise.safetensors", "strength": 0.8}],
            "low": [{"name": "wan22/paired/Style_low_noise.safetensors", "strength": 0.6}]}

    def wait(self, job_id, want=("completed", "failed")):
        for _ in range(100):
            status, body = self.call("GET", f"/v1/videos/{job_id}")
            if body.get("status") in want:
                return body
            time.sleep(0.1)
        self.fail(f"video {job_id} did not reach {want}")

    def test_catalogue_and_rescan_require_auth(self):
        self.assertEqual(self.call("GET", "/v1/loras", key=None)[0], 401)
        self.assertEqual(self.call("POST", "/v1/loras/rescan", key="wrong")[0], 401)
        self.assertEqual(self.call("POST", "/v1/videos/workflow", {"prompt": "x"}, key=None)[0], 401)

    def test_catalogue_lists_comfyui_names(self):
        status, body = self.call("POST", "/v1/loras/rescan")
        self.assertEqual(status, 200)
        self.assertEqual([f["name"] for f in body["data"]], self.comfy.lora_names)
        self.assertTrue(all(f["comfy_visible"] and f["usable"] for f in body["data"]))
        self.assertTrue(body["comfy"]["lora_loader_available"])
        status, listing = self.call("GET", "/v1/loras")
        self.assertEqual(status, 200)
        self.assertEqual(listing["roots"], [{"label": "video", "path": "/srv/models/video/loras"}])

    def test_rescan_records_an_unreachable_comfyui(self):
        from gx_media_router.comfy import upstream_error
        self.comfy.object_info_error = upstream_error("ComfyUI unreachable", "comfy_unavailable")
        try:
            status, body = self.call("POST", "/v1/loras/rescan")
            self.assertEqual(status, 200)
            self.assertIn("unreachable", body["comfy"]["error"])
            self.assertEqual(len(body["data"]), 2)
        finally:
            self.comfy.object_info_error = None
            self.call("POST", "/v1/loras/rescan")

    def test_preview_builds_without_running(self):
        before = len(self.comfy.submitted)
        status, body = self.call("POST", "/v1/videos/workflow",
                                 {"prompt": "a lobby", "seed": 7, "loras": self.SPEC, "shift": 6.5, "steps": 6})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.comfy.submitted), before)
        g = body["graph"]
        self.assertEqual(g["1000"]["inputs"]["lora_name"], "wan22/paired/Style_high_noise.safetensors")
        self.assertEqual(g["2000"]["inputs"]["strength_model"], 0.6)
        self.assertEqual((g["7"]["inputs"]["shift"], g["8"]["inputs"]["shift"]), (6.5, 6.5))
        self.assertEqual((g["12"]["inputs"]["steps"], g["12"]["inputs"]["end_at_step"],
                          g["13"]["inputs"]["start_at_step"], g["13"]["inputs"]["end_at_step"]), (6, 3, 3, 6))
        self.assertEqual(body["seed"], 7)
        self.assertEqual([c["lora_name"] for c in body["chains"]["low"]][-1],
                         "wan22/paired/Style_low_noise.safetensors")
        text = json.dumps(body)
        self.assertNotIn("/srv/", text)
        self.assertNotIn("test-key", text)

    def test_preview_refusals_carry_codes(self):
        status, body = self.call("POST", "/v1/videos/workflow", {
            "prompt": "x", "loras": {"high": [{"name": "wan22/paired/Style_low_noise.safetensors",
                                               "strength": 1}]}})
        self.assertEqual((status, body["error"]["code"]), (400, "lora_branch_mismatch"))
        status, body = self.call("POST", "/v1/videos/workflow", {"prompt": "x", "steps": 4, "boundary": 4})
        self.assertEqual(status, 400)

    def test_submit_runs_the_lora_graph_and_exposes_it(self):
        status, job = self.call("POST", "/v1/videos", {"prompt": "a lobby", "seconds": 1, "loras": self.SPEC})
        self.assertEqual(status, 202, job)
        self.assertEqual(job["loras"], {"high": [{"name": "wan22/paired/Style_high_noise.safetensors",
                                                  "strength": 0.8}],
                                        "low": [{"name": "wan22/paired/Style_low_noise.safetensors",
                                                 "strength": 0.6}]})
        self.assertRegex(job["workflow_version"], r"^gx-wan-lora/1\+")
        done = self.wait(job["gx_id"])
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["comfy_prompt_id"].startswith("prompt-"))
        sent = self.comfy.submitted[-1]
        self.assertEqual(sent["1000"]["inputs"]["lora_name"], "wan22/paired/Style_high_noise.safetensors")
        self.assertEqual(sent["8"]["inputs"]["model"], ["2000", 0])
        status, wf = self.call("GET", f"/v1/videos/{job['gx_id']}/workflow")
        self.assertEqual(status, 200)
        self.assertEqual(wf["graph"], sent)
        self.assertEqual(wf["comfy_prompt_id"], done["comfy_prompt_id"])
        self.assertEqual(self.call("GET", "/v1/videos/video-0000000000000000/workflow")[0], 404)

    def test_submit_refuses_unknown_lora_and_loras_on_image_to_video(self):
        status, body = self.call("POST", "/v1/videos", {
            "prompt": "x", "loras": {"high": [{"name": "missing.safetensors", "strength": 1}]}})
        self.assertEqual((status, body["error"]["code"]), (400, "lora_not_found"))
        import base64
        from tests.test_router import PNG_1x1
        status, body = self.call("POST", "/v1/videos", {"prompt": "x", "image": base64.b64encode(PNG_1x1).decode(),
                                                        "loras": self.SPEC})
        self.assertEqual(status, 400)

    def test_cancel_a_queued_video(self):
        self.comfy.delay = 1.0
        try:
            _, first = self.call("POST", "/v1/videos", {"prompt": "first", "seconds": 1})
            _, second = self.call("POST", "/v1/videos", {"prompt": "second", "seconds": 1})
            status, body = self.call("POST", f"/v1/videos/{second['id']}/cancel")
            self.assertEqual(status, 200, body)
            self.assertEqual((body["status"], body["gx_status"], body["error"]["code"]),
                             ("failed", "cancelled", "cancelled"))
            done = self.wait(first["gx_id"])
            self.assertEqual(done["status"], "completed")
            time.sleep(0.3)
            prompts = [g["9"]["inputs"]["text"] for g in self.comfy.submitted]
            self.assertNotIn("second", prompts)
            status, body = self.call("POST", f"/v1/videos/{first['gx_id']}/cancel")
            self.assertEqual((status, body["error"]["code"]), (409, "conflict"))
        finally:
            self.comfy.delay = 0.0

    def test_policy_hold_does_not_block_rescan_or_preview(self):
        class Hold:
            code, message = "maintenance", "maintenance mode is on"
        original = self.service.policy.block
        self.service.policy.block = lambda: Hold()
        try:
            self.assertEqual(self.call("POST", "/v1/loras/rescan")[0], 200)
            self.assertEqual(self.call("POST", "/v1/videos/workflow", {"prompt": "x"})[0], 200)
            self.assertEqual(self.call("POST", "/v1/videos", {"prompt": "x"})[0], 503)
        finally:
            self.service.policy.block = original

    def test_health_reports_the_catalogue(self):
        status, body = self.call("GET", "/health", key=None)
        self.assertTrue(body["loras"]["enabled"])
        self.assertEqual(body["version"], "2.5.0")


if __name__ == "__main__":
    unittest.main()
