"""Build V3 (IMG): gx-image model selection, edit planning, masks, SDXL admission.

Standard library only; no GPU, no ComfyUI.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router import image_models as im  # noqa: E402
from gx_media_router.config import Config  # noqa: E402
from gx_media_router.errors import ValidationError  # noqa: E402
from gx_media_router.server import build_server  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.uploads import InputStore  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

from tests.test_media_v2 import jpeg, multipart, png  # noqa: E402
from tests.test_router import FakeComfy  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"


class PlanTests(unittest.TestCase):
    qwen = im.IMAGE_MODELS[im.QWEN_EDIT]
    sdxl = im.IMAGE_MODELS[im.VISIONMASTER]

    def test_regression_strength_never_becomes_a_partial_qwen_denoise(self):
        """The bug: strength 0.6 was bound to KSampler.denoise and returned the source."""
        for mode in ("instruct", "change", "add", "remove", "restyle", "background", "subject"):
            for strength in (None, 0.0, 0.3, 0.6, 1.0):
                with self.subTest(mode=mode, strength=strength):
                    plan = im.plan_edit(self.qwen, mode, "make it red", strength, has_mask=False)
                    self.assertEqual(plan.denoise, 1.0)
                    self.assertEqual(plan.params["denoise"], 1.0)
                    self.assertIsNone(plan.strength)
                    self.assertEqual(plan.params["reference_method"], "index_timestep_zero")
                    self.assertEqual(plan.workflow, im.QWEN_EDIT_WORKFLOW)

    def test_modes_change_the_instruction(self):
        prompts = {m: im.plan_edit(self.qwen, m, "a red kite.", None, has_mask=False).prompt
                   for m in im.UI_EDIT_MODES}
        self.assertEqual(len(set(prompts.values())), len(prompts), "every mode must send a different prompt")
        self.assertTrue(prompts["add"].startswith("Add the following to the image: a red kite."))
        self.assertIn("Remove the following", prompts["remove"])
        self.assertIn("Replace the background with: a red kite", prompts["background"])
        self.assertEqual(im.plan_edit(self.qwen, "instruct", "as typed.", None, has_mask=False).prompt, "as typed.")
        self.assertEqual(im.plan_edit(self.qwen, None, "default", None, has_mask=False).mode, "instruct")

    def test_transform_drops_the_reference_latent_and_maps_strength(self):
        low = im.plan_edit(self.qwen, "transform", "x", 0.0, has_mask=False)
        high = im.plan_edit(self.qwen, "transform", "x", 1.0, has_mask=False)
        self.assertEqual(low.workflow, im.QWEN_EDIT_TRANSFORM_WORKFLOW)
        self.assertNotIn("reference_method", low.params)
        self.assertEqual((low.denoise, high.denoise), (0.8, 1.0))
        self.assertEqual(low.strength, 0.0)
        with self.assertRaises(ValidationError):
            im.plan_edit(self.qwen, "transform", "x", 0.5, has_mask=True)

    def test_mask_selects_the_masked_template(self):
        plan = im.plan_edit(self.qwen, "remove", "the bicycle", None, has_mask=True)
        self.assertEqual(plan.workflow, im.QWEN_EDIT_MASKED_WORKFLOW)
        self.assertEqual(plan.params["mask_grow"], 16)
        self.assertTrue(plan.masked)

    def test_quality_path_disables_lightning(self):
        plan = im.plan_edit(self.qwen, "change", "x", None, has_mask=False, quality="quality")
        self.assertEqual((plan.params["lightning_strength"], plan.params["steps"], plan.params["cfg"]), (0.0, 20, 4.0))
        self.assertNotIn("lightning_strength", im.plan_edit(self.qwen, "change", "x", None, has_mask=False).params)
        with self.assertRaises(ValidationError):
            im.plan_edit(self.qwen, "change", "x", None, has_mask=False, quality="ultra")

    def test_sdxl_edits_are_img2img_or_inpaint_with_real_strength(self):
        restyle = im.plan_edit(self.sdxl, "restyle", "oil painting", 0.0, has_mask=False)
        self.assertEqual((restyle.workflow, restyle.denoise), (im.SDXL_IMG2IMG_WORKFLOW, 0.45))
        self.assertEqual(im.plan_edit(self.sdxl, "restyle", "x", 1.0, has_mask=False).denoise, 0.85)
        inpaint = im.plan_edit(self.sdxl, "change", "red jacket", 0.5, has_mask=True)
        self.assertEqual((inpaint.workflow, inpaint.denoise), (im.SDXL_INPAINT_WORKFLOW, 0.8))
        self.assertEqual(inpaint.prompt, "red jacket", "SDXL gets a description, not an instruction template")
        with self.assertRaises(ValidationError) as ctx:
            im.plan_edit(self.sdxl, "change", "x", 0.5, has_mask=False)
        self.assertIn("needs a mask", ctx.exception.message)
        self.assertEqual(im.plan_edit(self.sdxl, None, "x", None, has_mask=False).mode, "restyle")

    def test_variation_strength(self):
        self.assertEqual(im.plan_variation(0.2, "v").workflow, im.QWEN_EDIT_WORKFLOW)
        strong = im.plan_variation(None, "v")
        self.assertEqual(strong.workflow, im.QWEN_EDIT_TRANSFORM_WORKFLOW)
        self.assertGreaterEqual(strong.denoise, 0.85)
        self.assertEqual(im.plan_variation(1.0, "v").denoise, 1.0)

    def test_resolve(self):
        self.assertEqual(im.resolve(None, "generate").id, im.QWEN_GENERATE)
        self.assertEqual(im.resolve("", "edit").id, im.QWEN_EDIT)
        self.assertEqual(im.resolve("visionmaster-pro-v3", "generate").label, "VisionmasterPro_V3")
        for value, op in (("qwen-image-2512", "edit"), ("visionmaster-pro-v3", "variation"), ("nope", "generate"),
                          (7, "generate")):
            with self.subTest(value=value, op=op), self.assertRaises(ValidationError):
                im.resolve(value, op)
        self.assertEqual(im.model_for_workflow("sdxl-visionmaster-pro-v3-inpaint"), im.VISIONMASTER)
        self.assertEqual(im.model_for_workflow("qwen-image-2512-quality"), im.QWEN_GENERATE)
        self.assertIsNone(im.model_for_workflow("wan22-t2v-a14b-uncensored"))

    def test_options_never_show_the_checkpoint_filename(self):
        text = json.dumps(im.options())
        self.assertNotIn("pornmaster", text.lower())
        labels = [m["label"] for m in im.options()["models"]]
        self.assertEqual(labels, ["Qwen Image 2512", "Qwen Image Edit 2511", "VisionmasterPro_V3"])

    def test_every_planned_workflow_exists_with_its_bindings(self):
        registry = WorkflowRegistry(WORKFLOW_DIR)
        for model in (self.qwen, self.sdxl):
            for mode in model.edit_modes:
                for mask in (False, True):
                    try:
                        plan = im.plan_edit(model, mode, "x", 0.5, has_mask=mask)
                    except ValidationError:
                        continue
                    wf = registry.get(plan.workflow)
                    with self.subTest(model=model.id, mode=mode, mask=mask):
                        self.assertEqual(wf.image_model, model.id)
                        self.assertEqual(dict(wf.inputs).get("mask") is not None, mask)
                        for key in plan.params:
                            self.assertIn(key, wf.bindings, f"{plan.workflow} lacks binding {key}")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key="k")
        cls.comfy = FakeComfy()
        cls.service = MediaService(cls.cfg, cls.comfy, WorkflowRegistry(WORKFLOW_DIR),
                                   inputs=InputStore(Path(cls.tmp.name)))
        cls.server = build_server(cls.cfg, cls.service)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", data=data,
                                     method=method, headers={"Authorization": "Bearer k", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def staged(self):
        d = Path(self.tmp.name) / "gx-in"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def test_image_models_endpoint(self):
        status, body = self.call("GET", "/v1/image-models")
        self.assertEqual(status, 200)
        self.assertEqual(body["default_generate"], "qwen-image-2512")
        sdxl = next(m for m in body["models"] if m["id"] == "visionmaster-pro-v3")
        self.assertEqual(sdxl["sizes"][0], "1024x1024")
        self.assertTrue(next(e for e in sdxl["edit_modes"] if e["id"] == "change")["requires_mask"])

    def test_generate_defaults_to_qwen(self):
        status, body = self.call("POST", "/v1/images/generations", {"prompt": "a fox", "size": "512x512"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["gx"]["image_model"], "qwen-image-2512")
        self.assertEqual(body["gx"]["workflow"], "qwen-image-2512-uncensored")

    def test_generate_with_visionmaster(self):
        status, body = self.call("POST", "/v1/images/generations",
                                 {"prompt": "a lighthouse", "image_model": "visionmaster-pro-v3", "seed": 5})
        self.assertEqual(status, 200, body)
        graph = self.comfy.submitted[-1]
        self.assertEqual(graph["1"]["inputs"]["unet_name"], "pornmasterPro_noobV3VAE/unet.safetensors")
        self.assertEqual(graph["2"]["inputs"]["type"], "sdxl")
        self.assertEqual((graph["7"]["inputs"]["width"], graph["7"]["inputs"]["height"]), (832, 1216))
        self.assertTrue(graph["5"]["inputs"]["text"].startswith("a lighthouse, masterpiece"))
        self.assertIn("worst quality", graph["6"]["inputs"]["text"])
        self.assertEqual((graph["8"]["inputs"]["steps"], graph["8"]["inputs"]["cfg"]), (28, 5.0))
        self.assertEqual(body["gx"]["image_model_label"], "VisionmasterPro_V3")
        self.assertEqual(body["gx"]["workflow"], "sdxl-visionmaster-pro-v3")
        self.assertIsNone(body["gx"]["adapter_strength"])

    def test_nested_gx_field_and_opt_out_of_quality_tags(self):
        status, body = self.call("POST", "/v1/images/generations",
                                 {"prompt": "plain", "gx": {"image_model": "visionmaster-pro-v3"},
                                  "quality_tags": False, "size": "1024x1024", "negative_prompt": "cats"})
        self.assertEqual(status, 200, body)
        graph = self.comfy.submitted[-1]
        self.assertEqual((graph["5"]["inputs"]["text"], graph["6"]["inputs"]["text"]), ("plain", "cats"))
        self.assertIsNone(body["gx"]["prompt_suffix"])

    def test_sdxl_refuses_oversized_canvases(self):
        status, body = self.call("POST", "/v1/images/generations",
                                 {"prompt": "x", "image_model": "visionmaster-pro-v3", "size": "1664x1664"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["param"], "size")

    def test_masked_qwen_edit_stages_and_removes_both_files(self):
        ctype, data = multipart({"prompt": "remove the kite", "edit_mode": "remove", "strength": "0.3"},
                                [("image", "a.png", "image/png", png(800, 600)),
                                 ("mask", "m.png", "image/png", png(400, 300))])
        status, body = self.call("POST", "/v1/images/edits", data, ctype)
        self.assertEqual(status, 200, body)
        graph = self.comfy.submitted[-1]
        self.assertEqual(body["gx"]["workflow"], "qwen-image-edit-2511-masked")
        self.assertRegex(graph["30"]["inputs"]["image"], r"^gx-in/[0-9a-f]{32}\.png$")
        self.assertNotEqual(graph["30"]["inputs"]["image"], graph["20"]["inputs"]["image"])
        self.assertEqual(graph["31"]["inputs"]["width"], graph["21"]["inputs"]["width"])
        self.assertEqual(graph["34"]["inputs"]["expand"], 16)
        self.assertEqual(graph["8"]["inputs"]["denoise"], 1.0)
        self.assertEqual(graph["39"]["inputs"]["mask"], ["37", 0])
        self.assertEqual(body["gx"]["edit"], {"edit_mode": "remove", "denoise": 1.0, "strength_applied": None,
                                              "masked": True, "workflow": "qwen-image-edit-2511-masked"})
        self.assertTrue(body["gx"]["prompt_sent"].startswith("Remove the following from the image: remove the kite"))
        self.assertEqual(self.staged(), [])

    def test_mask_must_match_the_source_and_be_png(self):
        for mask, want in ((png(300, 300), "aspect ratio"), (jpeg(400, 300), "must be a PNG"),
                           (b"GIF89a" + b"\x00" * 40, "unsupported")):
            ctype, data = multipart({"prompt": "x", "edit_mode": "change"},
                                    [("image", "a.png", "image/png", png(800, 600)),
                                     ("mask", "m.png", "image/png", mask)])
            status, body = self.call("POST", "/v1/images/edits", data, ctype)
            with self.subTest(want=want):
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["param"], "mask")
                self.assertIn(want, body["error"]["message"])
        self.assertEqual(self.staged(), [])

    def test_sdxl_img2img_json_edit(self):
        src = base64.b64encode(png(1024, 768)).decode()
        status, body = self.call("POST", "/v1/images/edits",
                                 {"prompt": "watercolour", "image": src, "image_model": "visionmaster-pro-v3",
                                  "edit_mode": "restyle", "strength": 0.5, "uncensored": True})
        self.assertEqual(status, 200, body)
        graph = self.comfy.submitted[-1]
        self.assertEqual(body["gx"]["workflow"], "sdxl-visionmaster-pro-v3-img2img")
        self.assertEqual(graph["8"]["inputs"]["denoise"], 0.65)
        self.assertEqual(body["gx"]["strength"], 0.5)
        self.assertIsNone(body["gx"]["adapter_strength"], "the Qwen adapter never applies to SDXL")

    def test_sdxl_change_without_mask_is_refused_before_staging(self):
        src = base64.b64encode(png(512, 512)).decode()
        status, body = self.call("POST", "/v1/images/edits",
                                 {"prompt": "x", "image": src, "image_model": "visionmaster-pro-v3",
                                  "edit_mode": "change"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["param"], "mask")
        self.assertEqual(self.staged(), [])

    def test_variation_refuses_sdxl_and_masks_are_ignored_for_it(self):
        ctype, data = multipart({"image_model": "visionmaster-pro-v3"}, [("image", "a.png", "image/png", png(64, 64))])
        status, body = self.call("POST", "/v1/images/variations", data, ctype)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["param"], "image_model")

    def test_unknown_edit_mode(self):
        ctype, data = multipart({"prompt": "x", "edit_mode": "explode"}, [("image", "a.png", "image/png", png(64, 64))])
        status, body = self.call("POST", "/v1/images/edits", data, ctype)
        self.assertEqual((status, body["error"]["param"]), (400, "edit_mode"))
        self.assertEqual(self.staged(), [])


class AdmissionTests(unittest.TestCase):
    def test_sdxl_workflows_use_their_own_footprint(self):
        cfg = Config(footprint_image_gib=57.0, footprint_sdxl_gib=64.0)
        service = MediaService(cfg, FakeComfy(), WorkflowRegistry(WORKFLOW_DIR),
                               inputs=InputStore(Path(tempfile.mkdtemp())))
        self.assertEqual(service._footprint_gib("sdxl-visionmaster-pro-v3"), 64.0)
        self.assertEqual(service._footprint_gib("sdxl-visionmaster-pro-v3-inpaint"), 64.0)
        self.assertEqual(service._footprint_gib("qwen-image-edit-2511-masked"), 57.0)
        service._resident_models = frozenset(service.workflows.get("sdxl-visionmaster-pro-v3").models)
        service._resident_kind = "gx-image"
        self.assertEqual(service._resident_footprint(), 64.0)
        service._resident_models = frozenset(service.workflows.get("qwen-image-edit-2511").models)
        self.assertEqual(service._resident_footprint(), 57.0)
        self.assertEqual(service._memory_view()["footprint_gib"]["image_sdxl"], 64.0)


if __name__ == "__main__":
    unittest.main()
