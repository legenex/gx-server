"""Build V3 (IMG): gx-image model catalogue, edit masks and Create-job validation."""

from __future__ import annotations

import base64
import json
import struct
import time
import unittest
import zlib
from pathlib import Path

from support import TempEnv

from gx_control_ui.image_catalog import CatalogError, ImageCatalog, parse_mask, png_mask_stats
from gx_control_ui.media_jobs import JobError, MediaJobs, validate
from gx_control_ui.media_library import MediaLibrary, MediaTools, NewAsset

REPO = Path(__file__).resolve().parents[3]


def mask_png(width: int, height: int, box: tuple[int, int, int, int] | None, *, channels: int = 4,
             filt: int = 0) -> bytes:
    """A mask PNG: white inside `box` (x0, y0, x1, y1), black elsewhere."""
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    rows = bytearray()
    prev = bytes(width * channels)
    for y in range(height):
        line = bytearray()
        for x in range(width):
            on = box is not None and box[0] <= x < box[2] and box[1] <= y < box[3]
            px = [255 if on else 0] * channels
            if channels in (2, 4):
                px[-1] = 255
            line += bytes(px)
        if filt == 2:  # "up" filter, like a browser may choose
            enc = bytes((line[i] - prev[i]) & 0xFF for i in range(len(line)))
        elif filt == 1:  # "sub"
            enc = bytes((line[i] - (line[i - channels] if i >= channels else 0)) & 0xFF for i in range(len(line)))
        else:
            enc = bytes(line)
        rows += bytes([filt]) + enc
        prev = bytes(line)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b""))


def data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.cat = ImageCatalog(REPO)

    def test_models_come_from_the_router_module(self):
        ids = [m["id"] for m in self.cat.options()["models"]]
        self.assertEqual(ids, ["qwen-image-2512", "qwen-image-edit-2511", "visionmaster-pro-v3"])
        self.assertEqual(self.cat.models["visionmaster-pro-v3"]["label"], "VisionmasterPro_V3")
        self.assertNotIn("pornmaster", json.dumps(self.cat.options()).lower())
        self.assertEqual(self.cat.default_for("t2i"), "qwen-image-2512")
        self.assertEqual(self.cat.default_for("edit"), "qwen-image-edit-2511")
        self.assertEqual(self.cat.family("visionmaster-pro-v3"), "sdxl")
        self.assertEqual(self.cat.model_for_workflow("sdxl-visionmaster-pro-v3-img2img"), "visionmaster-pro-v3")

    def test_operation_checks(self):
        with self.assertRaises(CatalogError):
            self.cat.model("edit", "qwen-image-2512")
        with self.assertRaises(CatalogError):
            self.cat.model("variation", "visionmaster-pro-v3")
        with self.assertRaises(CatalogError):
            self.cat.model("t2i", "sd15")
        self.assertEqual(self.cat.model("edit", None)["id"], "qwen-image-edit-2511")


class MaskTests(unittest.TestCase):
    def test_coverage_for_every_channel_layout_and_filter(self):
        for channels in (1, 2, 3, 4):
            for filt in (0, 1, 2):
                with self.subTest(channels=channels, filt=filt):
                    w, h, cov = png_mask_stats(mask_png(40, 20, (0, 0, 10, 20), channels=channels, filt=filt))
                    self.assertEqual((w, h), (40, 20))
                    self.assertAlmostEqual(cov, 0.25)

    def test_parse_mask_metadata_and_checks(self):
        raw = mask_png(64, 48, (8, 8, 40, 30))
        data, meta = parse_mask({"mask": data_url(raw), "mask_source": "rectangles",
                                 "mask_rects": [{"x": 0.125, "y": 0.1667, "w": 0.5, "h": 0.4583}]},
                                {"width": 1024, "height": 768})
        self.assertEqual(data, raw)
        self.assertEqual(meta["source"], "rectangles")
        self.assertEqual(len(meta["sha256"]), 64)
        self.assertAlmostEqual(meta["coverage"], 32 * 22 / (64 * 48), places=3)
        self.assertIsNone(parse_mask({}, None))
        bad = [
            ({"mask": data_url(mask_png(64, 48, None))}, "empty"),
            ({"mask": data_url(mask_png(64, 64, (0, 0, 9, 9)))}, "redraw it"),
            ({"mask": "data:image/jpeg;base64,AAAA"}, "data:image/png"),
            ({"mask": data_url(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)}, "PNG"),
            ({"mask": "x" * 60_000}, "48 KB"),
            ({"mask": data_url(raw), "mask_source": "magic"}, "mask_source"),
            ({"mask": data_url(raw), "mask_rects": [{"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}]}, "outside"),
            ({"mask": data_url(raw), "mask_rects": [{"x": True, "y": 0, "w": 0.5, "h": 0.5}]}, "fractions"),
            ({"mask_rects": [{"x": 0, "y": 0, "w": 1, "h": 1}]}, "send the mask image"),
            ({"mask": data_url(mask_png(2000, 10, (0, 0, 5, 5)))}, "pixels per side"),
        ]
        for body, want in bad:
            with self.subTest(want=want):
                with self.assertRaises(CatalogError) as ctx:
                    parse_mask(body, {"width": 1024, "height": 768})
                self.assertIn(want, str(ctx.exception))

    def test_decompression_bomb_is_bounded(self):
        # a 64x64 header followed by far more data than it declares
        huge = zlib.compress(b"\x00" * 50_000_000)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

        raw = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 0, 0, 0, 0))
               + chunk(b"IDAT", huge) + chunk(b"IEND", b""))
        t0 = time.monotonic()
        with self.assertRaises(CatalogError):
            png_mask_stats(raw)
        self.assertLess(time.monotonic() - t0, 2.0)


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.cat = ImageCatalog(REPO)

    def test_generate_defaults_and_model_specific_sizes(self):
        p = validate("t2i", {"prompt": "x"}, self.cat)
        self.assertEqual((p["image_model"], p["size"], p["quality"]), ("qwen-image-2512", "1328x1328", "standard"))
        p = validate("t2i", {"prompt": "x", "image_model": "visionmaster-pro-v3", "quality_tags": False}, self.cat)
        self.assertEqual((p["size"], p["quality_tags"]), ("832x1216", False))
        self.assertNotIn("quality", p)
        with self.assertRaises(JobError):
            validate("t2i", {"prompt": "x", "image_model": "visionmaster-pro-v3", "size": "1328x1328"}, self.cat)
        with self.assertRaises(JobError):
            validate("t2i", {"prompt": "x", "image_model": "visionmaster-pro-v3", "quality": "hd"}, self.cat)
        with self.assertRaises(JobError):
            validate("t2i", {"prompt": "x", "quality_tags": "yes"}, self.cat)

    def test_edit_modes_strength_and_masks(self):
        base = {"prompt": "x", "source_id": "a_" + "0" * 24}
        p = validate("edit", {**base, "strength": 0.6}, self.cat)
        # regression: a Qwen instruction edit drops the strength that caused near-copies
        self.assertEqual((p["image_model"], p["edit_mode"]), ("qwen-image-edit-2511", "change"))
        self.assertNotIn("strength", p)
        p = validate("edit", {**base, "strength": 0.3, "edit_mode": "transform", "edit_quality": "quality"}, self.cat)
        self.assertEqual((p["strength"], p["edit_quality"]), (0.3, "quality"))
        p = validate("edit", {**base, "image_model": "visionmaster-pro-v3", "strength": 0.4}, self.cat)
        self.assertEqual((p["edit_mode"], p["strength"]), ("restyle", 0.4))
        for body, want in (
            ({"image_model": "visionmaster-pro-v3", "edit_mode": "change"}, "needs a mask"),
            ({"image_model": "visionmaster-pro-v3", "edit_quality": "quality"}, "edit_quality"),
            ({"edit_mode": "transform", "mask": "data:image/png;base64,AA=="}, "Full transformation"),
            ({"edit_mode": "explode"}, "edit_mode"),
            ({"edit_quality": "ultra"}, "edit_quality"),
        ):
            with self.subTest(want=want):
                with self.assertRaises(JobError) as ctx:
                    validate("edit", {**base, **body}, self.cat)
                self.assertIn(want, str(ctx.exception))


class FakeRouter:
    def __init__(self):
        self.calls = []

    def get_json(self, path, timeout=30):
        return {"resident_alias": None}

    def post_json(self, path, body, timeout):
        self.calls.append((path, body, None))
        return self._result(body.get("image_model"))

    def post_multipart(self, path, ctype, data, timeout):
        self.calls.append((path, ctype, data))
        edit = {"edit_mode": "background", "denoise": 1.0, "strength_applied": None, "masked": True,
                "workflow": "qwen-image-edit-2511-masked"}
        return {**self._result("qwen-image-edit-2511"),
                "gx": {"workflow": "qwen-image-edit-2511-masked", "seed": 3, "size": "64x48", "strength": None,
                       "image_model": "qwen-image-edit-2511", "edit": edit}}

    @staticmethod
    def _result(model):
        png = mask_png(64, 48, (0, 0, 64, 48), channels=3) + b"\x00" * 1200
        return {"data": [{"b64_json": base64.b64encode(png).decode()}],
                "gx": {"workflow": "sdxl-visionmaster-pro-v3" if model == "visionmaster-pro-v3"
                       else "qwen-image-2512-uncensored", "seed": 1, "size": "832x1216", "image_model": model}}


class JobFlowTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.lib = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))
        self.router = FakeRouter()
        self.jobs = MediaJobs(self.lib, self.router, model_identity=lambda wf: {
            "repository": "votepurchase/pornmasterPro_noobV3VAE" if wf.startswith("sdxl") else "Qwen",
            "revision": "75f59d1" if wf.startswith("sdxl") else "r", "components": []}, poll_interval=0.01)

    def tearDown(self):
        self.env.cleanup()

    def wait(self, job_id):
        for _ in range(300):
            job = self.jobs.get(job_id)
            if job["phase"] in ("ready", "failed", "cancelled"):
                return job
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_visionmaster_generation_records_the_model(self):
        job = self.jobs.submit({"kind": "t2i", "prompt": "a lighthouse", "image_model": "visionmaster-pro-v3",
                                "uncensored": True}, user="t")
        done = self.wait(job["id"])
        self.assertEqual(done["phase"], "ready", done)
        _, body, _ = self.router.calls[-1]
        self.assertEqual(body["image_model"], "visionmaster-pro-v3")
        self.assertNotIn("uncensored", body)
        self.assertNotIn("quality", body)
        asset = self.lib.get(done["assets"][0])
        self.assertEqual((asset["model_alias"], asset["model_repo"], asset["workflow"]),
                         ("gx-image", "votepurchase/pornmasterPro_noobV3VAE", "sdxl-visionmaster-pro-v3"))
        self.assertEqual(asset["settings"]["image_model"], "visionmaster-pro-v3")
        self.assertEqual(asset["settings"]["image_model_label"], "VisionmasterPro_V3")
        self.assertEqual(asset["settings"]["image_model_family"], "sdxl")
        self.assertEqual(self.jobs._jobs[job["id"]].variant, "sdxl")

    def test_masked_edit_sends_the_mask_and_stores_only_metadata(self):
        src = self.lib.add(NewAsset(type="image", ext="png", operation="upload",
                                    data=mask_png(64, 48, (0, 0, 64, 48), channels=3) + b"\x00" * 1200,
                                    width=64, height=48))
        mask = mask_png(64, 48, (0, 24, 64, 48))
        job = self.jobs.submit({"kind": "edit", "prompt": "a beach", "source_id": src["id"],
                                "edit_mode": "background", "mask": data_url(mask), "strength": 0.9}, user="t")
        self.assertNotIn("base64", json.dumps(job))
        self.assertEqual(job["params"]["mask"]["coverage"], 0.5)
        done = self.wait(job["id"])
        self.assertEqual(done["phase"], "ready", done)
        path, ctype, data = self.router.calls[-1]
        self.assertEqual(path, "/v1/images/edits")
        self.assertIn(b'name="mask"; filename="mask.png"', data)
        self.assertIn(mask, data)
        self.assertIn(b'name="edit_mode"\r\n\r\nbackground', data)
        self.assertNotIn(b'name="strength"', data, "an instruction edit must not send a denoise strength")
        asset = self.lib.get(done["assets"][0])
        self.assertIsNone(asset["strength"])
        self.assertEqual(asset["parent_id"], src["id"])
        self.assertEqual(asset["settings"]["edit"]["edit_mode"], "background")
        self.assertEqual(asset["settings"]["mask"]["source"], "painted")
        self.assertEqual(asset["settings"]["image_model"], "qwen-image-edit-2511")

    def test_mask_is_refused_for_other_kinds_and_mismatched_sources(self):
        src = self.lib.add(NewAsset(type="image", ext="png", operation="upload",
                                    data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000, width=100, height=100))
        with self.assertRaises(JobError):
            self.jobs.submit({"kind": "t2i", "prompt": "x", "mask": data_url(mask_png(16, 16, (0, 0, 8, 8)))},
                             user="t")
        with self.assertRaises(JobError) as ctx:
            self.jobs.submit({"kind": "edit", "prompt": "x", "source_id": src["id"],
                              "mask": data_url(mask_png(64, 32, (0, 0, 8, 8)))}, user="t")
        self.assertIn("redraw it", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
