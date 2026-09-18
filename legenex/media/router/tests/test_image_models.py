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

    def test_masked_edits_use_the_true_cfg_schedule_not_the_4_step_lightning_one(self):
        """Four distilled steps cannot repaint a masked region.

        Measured on edit_masked_lower with the same source, mask and seed: 4 Lightning
        steps left the masked area at MAD 3.1 (Qwen's ~2.0 "preserving this" floor),
        while 20 steps at cfg 4.0 gave MAD 24.3 and ssim_masked 0.47 with the area
        outside the mask still bit-identical. The reference latent keeps showing the
        model the original, so there has to be enough schedule left to denoise past it.
        """
        masked = im.plan_edit(self.qwen, "change", "tall green grass", None, has_mask=True)
        self.assertEqual(masked.params["steps"], 20)
        self.assertEqual(masked.params["cfg"], 4.0)
        self.assertEqual(masked.params["lightning_strength"], 0.0)
        # An unmasked edit is unaffected: it keeps the fast Lightning schedule.
        plain = im.plan_edit(self.qwen, "change", "tall green grass", None, has_mask=False)
        self.assertNotIn("steps", plain.params)
        # ...and an explicit "fast" still wins for a caller who wants speed.
        fast = im.plan_edit(self.qwen, "change", "tall green grass", None,
                            has_mask=True, quality="fast")
        self.assertNotIn("steps", fast.params)

    def test_masked_edits_never_ask_the_model_to_preserve_the_picture(self):
        """The mask preserves; the prompt paints.

        A masked Qwen edit still shows the model the whole clean source as a reference
        latent, so a "keep everything else exactly as it is" clause is obeyed inside the
        mask as well and the edit returns the source (edit_masked_lower: ssim 0.987,
        phash 0, MAD 2.9 inside the mask). The masked prompt must describe the content.
        """
        keep_words = ("Keep everything else", "exactly as it is", "Keep the background",
                      "Keep the main subject", "Keep the same subject", "composition unchanged")
        for mode in im.UI_EDIT_MODES:
            if im.EDIT_MODES[mode].qwen_reference == "none":
                continue  # transform refuses a mask outright
            with self.subTest(mode=mode):
                masked = im.plan_edit(self.qwen, mode, "tall green grass.", None, has_mask=True)
                self.assertTrue(masked.masked)
                for word in keep_words:
                    self.assertNotIn(word, masked.prompt)
                self.assertIn("tall green grass", masked.prompt)
        # the plain "change" mode sends the bare content description, like the inpaint
        # path that always worked; without a mask it keeps its preservation clause
        self.assertEqual(im.plan_edit(self.qwen, "change", "tall green grass.", None, has_mask=True).prompt,
                         "tall green grass")
        self.assertIn("Keep everything else",
                      im.plan_edit(self.qwen, "change", "tall green grass.", None, has_mask=False).prompt)

    def test_masked_remove_still_names_the_thing_to_remove(self):
        """`remove`'s instruction names what goes away, so its masked prompt keeps the verb."""
        plan = im.plan_edit(self.qwen, "remove", "the bicycle.", None, has_mask=True)
        self.assertTrue(plan.prompt.startswith("Remove the following from the image: the bicycle."))
        self.assertIn("Fill the area it occupied", plan.prompt)
        self.assertNotIn("Keep everything else", plan.prompt)

    def test_masked_edits_run_the_full_denoise_and_report_it_honestly(self):
        """No masked denoise override: int(steps/denoise) makes 0.88 and 1.0 the same 4 sigmas."""
        for mode in ("change", "add", "remove", "background", "subject"):
            with self.subTest(mode=mode):
                plan = im.plan_edit(self.qwen, mode, "x", None, has_mask=True)
                self.assertEqual(plan.params["denoise"], 1.0)
                self.assertEqual(plan.public()["denoise"], plan.params["denoise"])

    def test_public_reports_the_denoise_the_graph_was_given(self):
        plan = im.EditPlan("w", "change", "p", 1.0, {"denoise": 0.5}, None, True)
        self.assertEqual(plan.public()["denoise"], 0.5, "public() must not report a denoise that never ran")

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


class MaskedAcceptanceTests(unittest.TestCase):
    """The acceptance harness must judge a masked case inside the mask.

    Whole-image SSIM measures the mask's size, not the edit: half the frame replaced
    perfectly still scores about 0.5, and a small mask repainted completely still scores
    about 1.0. Only `image_accept`'s pure verdict function is exercised here (stdlib only,
    no numpy, no router).
    """

    accept = None

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = Path(__file__).resolve().parents[2] / "tools" / "image_accept.py"
        spec = importlib.util.spec_from_file_location("image_accept", path)
        cls.accept = importlib.util.module_from_spec(spec)
        sys.modules["image_accept"] = cls.accept  # @dataclass resolves its module by name
        spec.loader.exec_module(cls.accept)

    def case(self):
        return self.accept.Case("edit_masked_lower", "masked", "edit", mask="lower")

    def metrics(self, **over):
        m = {"ssim": 0.51, "phash_dist": 24, "hist_corr": 0.7, "mad": 48.0, "near_duplicate": False,
             "mask_white_fraction": 0.5, "ssim_masked": 0.04, "ssim_unmasked": 0.99,
             "mad_masked_white": 95.9, "mad_masked_black": 0.1, "phash_masked_dist": 20,
             "masked_near_duplicate": False}
        m.update(over)
        return m

    def test_a_real_masked_edit_passes(self):
        self.assertEqual(self.accept.masked_verdict(self.case(), self.metrics())[0], "PASS")

    def test_a_masked_region_that_came_back_unchanged_fails(self):
        # the measured failure: the masked half was repainted as the source
        status, detail = self.accept.masked_verdict(
            self.case(), self.metrics(ssim=0.987, phash_dist=0, mad=1.5, near_duplicate=True,
                                      ssim_masked=0.974, mad_masked_white=2.92, mad_masked_black=0.03,
                                      phash_masked_dist=0, masked_near_duplicate=True))
        self.assertEqual(status, "FAIL")
        self.assertIn("copy of the source", detail)

    def test_a_mask_floor_change_is_not_a_change(self):
        """MAD ~2 inside the mask is Qwen's preservation floor, whatever SSIM says."""
        status, _ = self.accept.masked_verdict(self.case(),
                                               self.metrics(ssim_masked=0.5, mad_masked_white=2.9))
        self.assertEqual(status, "FAIL")

    def test_an_edit_that_leaks_outside_the_mask_fails(self):
        status, detail = self.accept.masked_verdict(self.case(), self.metrics(mad_masked_black=95.9))
        self.assertEqual(status, "FAIL")
        self.assertIn("mask protects", detail)

    def test_a_barely_changed_mask_is_weak(self):
        status, _ = self.accept.masked_verdict(self.case(),
                                               self.metrics(ssim_masked=0.95, phash_masked_dist=9,
                                                            mad_masked_white=12.0))
        self.assertEqual(status, "WEAK")

    def test_unmasked_thresholds_are_untouched(self):
        """The masked path must never relax the whole-image verdict for unmasked cases."""
        self.assertEqual((self.accept.NEAR_DUP_SSIM, self.accept.NEAR_DUP_PHASH), (0.90, 8))
        self.assertEqual((self.accept.GOOD_SSIM, self.accept.GOOD_PHASH), (0.88, 10))
        plain = self.accept.Case("edit_add", "add", "edit")
        payload = {"metrics": {"ssim": 0.95, "phash_dist": 2, "hist_corr": 1.0, "mad": 3.0,
                               "near_duplicate": True}}
        out = Path(__file__)  # any existing, non-empty file stands in for the PNG
        self.assertEqual(self.accept.verdict(plain, payload, out)[0], "FAIL")

    def test_a_masked_case_without_masked_metrics_falls_back_to_the_whole_image(self):
        payload = {"metrics": {"ssim": 0.99, "phash_dist": 0, "hist_corr": 1.0, "mad": 0.5,
                               "near_duplicate": True}}
        self.assertEqual(self.accept.verdict(self.case(), payload, Path(__file__))[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
