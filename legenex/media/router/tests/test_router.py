"""Unit tests for the media router. Standard library only; no GPU, no ComfyUI.

Run:  python3 -m unittest discover -s tests -v      (from legenex/media/router)
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router.comfy import Artefact, Result  # noqa: E402
from gx_media_router.config import Config  # noqa: E402
from gx_media_router.errors import BusyError, ValidationError  # noqa: E402
from gx_media_router.jobs import GenerationSlot, JobStore  # noqa: E402
from gx_media_router.server import build_server  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router import validation as v  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry, load_workflow  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"
PNG_1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
MP4_STUB = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


class FakeComfy:
    """Stands in for ComfyUI: records submissions, returns a known artefact."""

    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self.fail_with: Exception | None = None
        self.delay = 0.0
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def submit(self, graph, client_id):
        if self.fail_with:
            raise self.fail_with
        self.submitted.append(graph)
        return f"prompt-{len(self.submitted)}"

    def wait(self, prompt_id, *, timeout, poll_interval=1.0, cancelled=None, thumbnail_node=None):
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.delay:
                import time
                time.sleep(self.delay)
            graph = self.submitted[-1] if self.submitted else {}
            if any(n.get("class_type") == "SaveVideo" for n in graph.values()):
                return Result(prompt_id, (
                    Artefact("out_00001_.mp4", "gx-video", "output", "images"),
                    Artefact("thumb_00001_.png", "gx-video", "output", "images", thumbnail=True),
                ), 0.5)
            return Result(prompt_id, (Artefact("out_00001_.png", "gx-image", "output", "images"),), 0.5)
        finally:
            with self._lock:
                self.concurrent -= 1

    def fetch(self, artefact, *, timeout=120.0):
        return MP4_STUB if artefact.filename.endswith(".mp4") else PNG_1x1

    def system_stats(self):
        return {"system": {"comfyui_version": "test", "pytorch_version": "2.14.0+cu130"},
                "devices": [{"name": "GB10", "vram_free": 1}]}

    def queue_depth(self):
        return 0

    frees = 0

    def free(self, *, unload_models=True, free_memory=True):
        self.frees += 1


class WorkflowTests(unittest.TestCase):
    def test_all_shipped_workflows_load_and_validate(self):
        registry = WorkflowRegistry(WORKFLOW_DIR)
        self.assertIn("qwen-image-2512-lightning", registry.names())
        self.assertIn("wan22-t2v-a14b-lightning", registry.names())
        for name in ("qwen-image-2512-uncensored", "qwen-image-edit-2511", "wan22-t2v-a14b-uncensored",
                     "wan22-i2v-a14b-uncensored", "wan22-v2v-a14b-uncensored", "wan22-v2v-a14b-light"):
            self.assertIn(name, registry.names())
        self.assertEqual(len(registry.of_kind("image")), 4)
        self.assertEqual(len(registry.of_kind("video")), 6)

    def test_bindings_reach_the_intended_nodes(self):
        wf = WorkflowRegistry(WORKFLOW_DIR).get("qwen-image-2512-lightning")
        graph = wf.build({"prompt": "a fox", "width": 512, "height": 768, "seed": 99})
        self.assertEqual(graph["5"]["inputs"]["text"], "a fox")
        self.assertEqual(graph["7"]["inputs"]["width"], 512)
        self.assertEqual(graph["7"]["inputs"]["height"], 768)
        self.assertEqual(graph["8"]["inputs"]["seed"], 99)
        # defaults survive when not overridden
        self.assertEqual(graph["8"]["inputs"]["steps"], 4)
        self.assertEqual(graph["8"]["inputs"]["cfg"], 1.0)

    def test_build_does_not_mutate_the_template(self):
        wf = WorkflowRegistry(WORKFLOW_DIR).get("qwen-image-2512-lightning")
        wf.build({"prompt": "first"})
        second = wf.build({"prompt": "second"})
        self.assertEqual(second["5"]["inputs"]["text"], "second")
        self.assertEqual(wf.graph["5"]["inputs"]["text"], "a photograph")

    def test_a_binding_to_a_missing_node_is_rejected_at_load(self):
        broken = json.loads((WORKFLOW_DIR / "qwen-image-2512-lightning.api.json").read_text())
        broken["_gx"]["bindings"]["prompt"] = "999.text"
        path = Path(self.enterContext(__import__("tempfile").TemporaryDirectory())) / "b.api.json"
        path.write_text(json.dumps(broken))
        with self.assertRaises(ValueError):
            load_workflow(path)

    def test_the_video_template_wires_both_experts(self):
        wf = WorkflowRegistry(WORKFLOW_DIR).get("wan22-t2v-a14b-lightning")
        graph = wf.build({"prompt": "rain"})
        self.assertEqual(graph["12"]["inputs"]["model"], ["7", 0])   # high-noise expert
        self.assertEqual(graph["13"]["inputs"]["model"], ["8", 0])   # low-noise expert
        self.assertEqual(graph["13"]["inputs"]["latent_image"], ["12", 0])


class ValidationTests(unittest.TestCase):
    cfg = Config()

    def test_size_parsing_and_bounds(self):
        self.assertEqual(v.dimensions({"size": "1024x1024"}, self.cfg, "1328x1328"), (1024, 1024))
        self.assertEqual(v.dimensions({"size": "auto"}, self.cfg, "1328x1328"), (1328, 1328))
        self.assertEqual(v.dimensions({}, self.cfg, "1328x1328"), (1328, 1328))
        for bad in ("1000x1000", "64x64", "4096x4096", "big", "1024", "-16x16"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                v.dimensions({"size": bad}, self.cfg, "1328x1328")

    def test_prompt_is_required_and_bounded(self):
        self.assertEqual(v.prompt({"prompt": "  hi  "}, self.cfg), "hi")
        for bad in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 5}, {"prompt": None}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                v.prompt(bad, self.cfg)
        with self.assertRaises(ValidationError):
            v.prompt({"prompt": "x" * (self.cfg.max_prompt_chars + 1)}, self.cfg)

    def test_n_bounds(self):
        self.assertEqual(v.count({}, self.cfg), 1)
        self.assertEqual(v.count({"n": 4}, self.cfg), 4)
        for bad in (0, 5, -1, True, "2", 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                v.count({"n": bad}, self.cfg)

    def test_seed_is_deterministic_when_given_and_random_otherwise(self):
        self.assertEqual(v.seed({"seed": 7}), 7)
        self.assertNotEqual(v.seed({}), v.seed({}))
        with self.assertRaises(ValidationError):
            v.seed({"seed": -1})

    def test_video_length_snaps_to_4k_plus_1(self):
        self.assertEqual(v.video_length({"seconds": 3}, self.cfg, 16.0), 49)   # 48 -> nearest 4k+1
        self.assertEqual(v.video_length({"length": 50}, self.cfg, 16.0), 49)
        self.assertEqual(v.video_length({"length": 10_000}, self.cfg, 16.0), 161)
        for value in range(5, 200, 7):
            self.assertEqual((v.video_length({"length": value}, self.cfg, 16.0) - 1) % 4, 0)

    def test_response_format_allowlist(self):
        self.assertEqual(v.response_format({}), "b64_json")
        self.assertEqual(v.response_format({"response_format": "url"}), "url")
        with self.assertRaises(ValidationError):
            v.response_format({"response_format": "html"})


class SlotTests(unittest.TestCase):
    def test_only_one_holder_at_a_time(self):
        slot = GenerationSlot()
        with slot.hold("a", 1.0):
            with self.assertRaises(BusyError):
                with slot.hold("b", 0.2):
                    pass
        with slot.hold("c", 1.0):
            self.assertEqual(slot.held_by()[0], "c")

    def test_slot_is_released_when_the_body_raises(self):
        slot = GenerationSlot()
        with self.assertRaises(RuntimeError):
            with slot.hold("a", 1.0):
                raise RuntimeError("boom")
        self.assertIsNone(slot.held_by()[0])

    def test_job_store_is_bounded_and_evicts_oldest(self):
        store = JobStore(capacity=3)
        ids = [store.create("image", "w", "p", {}).id for _ in range(5)]
        self.assertEqual(len(store.snapshot()), 3)
        with self.assertRaises(Exception):
            store.get(ids[0])
        self.assertIsNotNone(store.get(ids[-1]))


class HttpTests(unittest.TestCase):
    """End-to-end over real HTTP against a fake ComfyUI."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key="test-key",
                         workflow_dir=WORKFLOW_DIR, queue_wait_seconds=2)
        cls.comfy = FakeComfy()
        cls.service = MediaService(cls.cfg, cls.comfy, WorkflowRegistry(WORKFLOW_DIR))
        cls.server = build_server(cls.cfg, cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, key="test-key"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_health_needs_no_credentials(self):
        status, body, _ = self.call("GET", "/health", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["service"], "gx-media-router")

    def test_every_other_route_requires_the_key(self):
        for method, path, body in (
            ("GET", "/v1/models", None),
            ("POST", "/v1/images/generations", {"prompt": "x"}),
            ("POST", "/v1/videos", {"prompt": "x"}),
        ):
            with self.subTest(path=path):
                status, _, _ = self.call(method, path, body, key=None)
                self.assertEqual(status, 401)
                status, _, _ = self.call(method, path, body, key="wrong-key")
                self.assertEqual(status, 401)

    def test_image_generation_returns_openai_shape(self):
        status, body, _ = self.call("POST", "/v1/images/generations",
                                    {"prompt": "a fox", "size": "512x512", "seed": 3})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(base64.b64decode(payload["data"][0]["b64_json"]), PNG_1x1)
        self.assertEqual(payload["gx"]["size"], "512x512")
        self.assertEqual(payload["gx"]["seed"], 3)
        self.assertEqual(self.comfy.submitted[-1]["7"]["inputs"]["width"], 512)

    def test_url_response_format_serves_the_bytes_back(self):
        status, body, _ = self.call("POST", "/v1/images/generations",
                                    {"prompt": "a fox", "response_format": "url"})
        self.assertEqual(status, 200)
        url = json.loads(body)["data"][0]["url"]
        status, content, headers = self.call("GET", url)
        self.assertEqual(status, 200)
        self.assertEqual(content, PNG_1x1)
        self.assertEqual(headers["Content-Type"], "image/png")

    def test_quality_hd_selects_the_full_sampling_workflow(self):
        status, body, _ = self.call("POST", "/v1/images/generations",
                                    {"prompt": "a fox", "quality": "hd"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["gx"]["workflow"], "qwen-image-2512-quality")

    def test_default_generation_is_the_uncensored_template(self):
        status, body, _ = self.call("POST", "/v1/images/generations", {"prompt": "a fox"})
        self.assertEqual(status, 200)
        gx = json.loads(body)["gx"]
        self.assertEqual(gx["workflow"], "qwen-image-2512-uncensored")
        self.assertEqual(self.comfy.submitted[-1]["12"]["inputs"]["strength_model"], 0.6)
        status, body, _ = self.call("POST", "/v1/images/generations", {"prompt": "a fox", "uncensored": False})
        self.assertEqual(self.comfy.submitted[-1]["12"]["inputs"]["strength_model"], 0.0)

    def test_invalid_requests_are_rejected_with_400(self):
        for body in ({}, {"prompt": ""}, {"prompt": "x", "size": "3x3"},
                     {"prompt": "x", "n": 99}, {"prompt": "x", "response_format": "zip"},
                     {"prompt": "x", "workflow": "../../etc/passwd"}):
            with self.subTest(body=body):
                status, payload, _ = self.call("POST", "/v1/images/generations", body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(payload))

    def test_unknown_routes_are_404(self):
        status, _, _ = self.call("GET", "/v1/anything", None)
        self.assertEqual(status, 404)

    def test_video_is_accepted_asynchronously_and_polls_to_completion(self):
        status, body, headers = self.call("POST", "/v1/videos", {"prompt": "rain", "seconds": 2})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["object"], "video")
        job = json.loads(body)
        self.assertIn(job["status"], ("queued", "running", "completed"))
        self.assertTrue(headers["Location"].endswith(job["id"]))
        for _ in range(100):
            status, body, _ = self.call("GET", f"/v1/videos/{job['id']}")
            self.assertEqual(status, 200)
            if json.loads(body)["status"] in ("completed", "failed"):
                break
            __import__("time").sleep(0.05)
        self.assertEqual(json.loads(body)["status"], "completed")

    def test_unknown_job_id_is_404(self):
        status, _, _ = self.call("GET", "/v1/videos/image-deadbeef")
        self.assertEqual(status, 404)

    def test_concurrent_image_requests_never_overlap_upstream(self):
        self.comfy.delay = 0.3
        self.comfy.max_concurrent = 0
        results: list[int] = []

        def fire():
            status, _, _ = self.call("POST", "/v1/images/generations", {"prompt": "concurrency"})
            results.append(status)

        threads = [threading.Thread(target=fire) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.comfy.delay = 0.0
        self.assertEqual(results, [200, 200, 200, 200])
        self.assertEqual(self.comfy.max_concurrent, 1, "two generations ran against ComfyUI at once")


if __name__ == "__main__":
    unittest.main(verbosity=2)
