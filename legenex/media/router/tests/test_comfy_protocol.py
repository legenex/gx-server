"""Integration tests against a faithful stub of ComfyUI's HTTP protocol.

These exercise the REAL ComfyClient and the REAL router over real sockets. The
only thing replaced is the GPU: the stub answers /prompt, /history, /view,
/system_stats and /queue exactly as ComfyUI does, including its two failure
shapes (a graph rejected at submit, and a node that raises during execution).

Everything here is deterministic, hermetic and safe to rerun. Run:
    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router.comfy import ComfyClient  # noqa: E402
from gx_media_router.config import Config  # noqa: E402
from gx_media_router.errors import TimeoutError_, UpstreamError  # noqa: E402
from gx_media_router.server import build_server  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"
PNG_1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
MP4_STUB = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


class ComfyStubHandler(BaseHTTPRequestHandler):
    """Speaks ComfyUI's wire protocol. Behaviour is driven by ``server.mode``."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _reply(self, status, payload, content_type="application/json"):
        body = json.dumps(payload).encode() if content_type == "application/json" else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        srv = self.server
        if parsed.path == "/system_stats":
            self._reply(200, {"system": {"comfyui_version": "0.3.stub",
                                         "pytorch_version": "2.14.0+cu130"},
                              "devices": [{"name": "NVIDIA GB10", "vram_free": 100 << 30}]})
        elif parsed.path == "/queue":
            self._reply(200, {"queue_running": [], "queue_pending": []})
        elif parsed.path.startswith("/history/"):
            prompt_id = parsed.path.rsplit("/", 1)[-1]
            srv.history_polls += 1
            if srv.mode == "never_finishes":
                self._reply(200, {})
                return
            if srv.history_polls < srv.polls_before_done:
                self._reply(200, {})   # ComfyUI returns {} until the job lands
                return
            if srv.mode == "exec_error":
                self._reply(200, {prompt_id: {
                    "status": {"status_str": "error", "completed": False, "messages": [
                        ["execution_error", {"node_id": "8", "node_type": "KSampler",
                                             "exception_type": "torch.OutOfMemoryError",
                                             "exception_message": "CUDA out of memory"}]]}}})
                return
            if srv.mode == "no_outputs":
                self._reply(200, {prompt_id: {"status": {"status_str": "success", "completed": True},
                                              "outputs": {}}})
                return
            graph = (srv.submissions[-1] or {}).get("prompt", {}) if srv.submissions else {}
            if any(n.get("class_type") == "SaveVideo" for n in graph.values()):
                # ComfyUI's SaveVideo reports its file under "images" (animated).
                self._reply(200, {prompt_id: {
                    "status": {"status_str": "success", "completed": True, "messages": []},
                    "outputs": {
                        "16": {"images": [{"filename": "wan22_00001_.mp4", "subfolder": "gx-video",
                                           "type": "output"}], "animated": [True]},
                        "18": {"images": [{"filename": "wan22-thumb_00001_.png", "subfolder": "gx-video",
                                           "type": "output"}]}}}})
                return
            self._reply(200, {prompt_id: {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {srv.output_node: {"images": [
                    {"filename": "qwen2512-lightning_00001_.png",
                     "subfolder": "gx-image", "type": "output"}]}}}})
        elif parsed.path == "/view":
            query = urllib.parse.parse_qs(parsed.query)
            srv.view_requests.append(query)
            name = query.get("filename", [""])[0]
            if name == "wan22_00001_.mp4":
                self._reply(200, MP4_STUB, "video/mp4")
                return
            if name not in ("qwen2512-lightning_00001_.png", "wan22-thumb_00001_.png"):
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, PNG_1x1, "image/png")
        else:
            self._reply(404, {"error": "no such route"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if parsed.path == "/prompt":
            srv.submissions.append(body)
            if srv.mode == "reject":
                self._reply(200, {"error": {"type": "prompt_outputs_failed_validation"},
                                  "node_errors": {"8": {"errors": [
                                      {"type": "value_not_in_list",
                                       "message": "Value not in list: sampler_name"}]}}})
                return
            srv.history_polls = 0
            self._reply(200, {"prompt_id": f"stub-{len(srv.submissions)}", "number": 1,
                              "node_errors": {}})
        elif parsed.path == "/free":
            self._reply(200, {})
        else:
            self._reply(404, {"error": "no such route"})


class ComfyStub(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ComfyStubHandler)
        self.mode = "ok"
        self.polls_before_done = 2
        self.history_polls = 0
        self.output_node = "10"
        self.submissions: list[dict] = []
        self.view_requests: list[dict] = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def reset(self, mode="ok"):
        self.mode = mode
        self.history_polls = 0
        self.polls_before_done = 2
        self.submissions.clear()
        self.view_requests.clear()


class ComfyClientProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = ComfyStub()
        cls.client = ComfyClient(cls.stub.url)

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        self.stub.reset()

    def test_submit_poll_fetch_round_trip(self):
        prompt_id = self.client.submit({"10": {"class_type": "SaveImage", "inputs": {}}}, "cid")
        self.assertEqual(prompt_id, "stub-1")
        result = self.client.wait(prompt_id, timeout=10, poll_interval=0.01)
        self.assertEqual(len(result.artefacts), 1)
        self.assertEqual(result.artefacts[0].filename, "qwen2512-lightning_00001_.png")
        self.assertEqual(result.artefacts[0].media_type, "image/png")
        self.assertEqual(self.client.fetch(result.artefacts[0]), PNG_1x1)
        # /view arguments come from ComfyUI's own history, never from a caller
        self.assertEqual(self.stub.view_requests[-1]["subfolder"], ["gx-image"])
        self.assertEqual(self.stub.view_requests[-1]["type"], ["output"])

    def test_history_returning_empty_is_polled_not_treated_as_failure(self):
        self.stub.polls_before_done = 5
        prompt_id = self.client.submit({}, "cid")
        result = self.client.wait(prompt_id, timeout=10, poll_interval=0.01)
        self.assertGreaterEqual(self.stub.history_polls, 5)
        self.assertEqual(len(result.artefacts), 1)

    def test_a_rejected_graph_raises_upstream_error_with_the_node_detail(self):
        self.stub.reset("reject")
        with self.assertRaises(UpstreamError) as ctx:
            self.client.submit({}, "cid")
        self.assertIn("value_not_in_list", ctx.exception.message)

    def test_an_execution_error_surfaces_the_failing_node(self):
        self.stub.reset("exec_error")
        prompt_id = self.client.submit({}, "cid")
        with self.assertRaises(UpstreamError) as ctx:
            self.client.wait(prompt_id, timeout=10, poll_interval=0.01)
        self.assertIn("KSampler", ctx.exception.message)
        self.assertIn("CUDA out of memory", ctx.exception.message)

    def test_completion_with_no_output_file_is_an_error_not_a_success(self):
        self.stub.reset("no_outputs")
        prompt_id = self.client.submit({}, "cid")
        with self.assertRaises(UpstreamError):
            self.client.wait(prompt_id, timeout=10, poll_interval=0.01)

    def test_a_job_that_never_finishes_times_out(self):
        self.stub.reset("never_finishes")
        prompt_id = self.client.submit({}, "cid")
        started = time.monotonic()
        with self.assertRaises(TimeoutError_):
            self.client.wait(prompt_id, timeout=0.5, poll_interval=0.05)
        self.assertLess(time.monotonic() - started, 5)

    def test_an_unreachable_comfyui_is_a_502_not_a_crash(self):
        dead = ComfyClient("http://127.0.0.1:1")
        with self.assertRaises(UpstreamError):
            dead.system_stats()


class FullStackTests(unittest.TestCase):
    """Router -> real ComfyClient -> ComfyUI protocol stub, over real sockets."""

    @classmethod
    def setUpClass(cls):
        cls.stub = ComfyStub()
        cls.cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key="secret",
                         comfy_url=cls.stub.url, workflow_dir=WORKFLOW_DIR,
                         queue_wait_seconds=10)
        cls.service = MediaService(cls.cfg, ComfyClient(cls.stub.url),
                                   WorkflowRegistry(WORKFLOW_DIR))
        cls.server = build_server(cls.cfg, cls.service)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.stub.shutdown()
        cls.stub.server_close()

    def call(self, method, path, body=None, key="secret"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_the_graph_actually_sent_to_comfyui_is_the_vetted_one(self):
        self.stub.reset()
        status, body = self.call("POST", "/v1/images/generations",
                                 {"prompt": "a hummingbird", "size": "1024x1024", "seed": 11})
        self.assertEqual(status, 200)
        graph = self.stub.submissions[-1]["prompt"]
        self.assertNotIn("_gx", graph)
        self.assertEqual(graph["1"]["inputs"]["unet_name"], "qwen_image_2512_fp8_e4m3fn.safetensors")
        self.assertEqual(graph["11"]["inputs"]["lora_name"],
                         "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors")
        self.assertEqual(graph["5"]["inputs"]["text"], "a hummingbird")
        self.assertEqual(graph["7"]["inputs"], {"width": 1024, "height": 1024, "batch_size": 1})
        self.assertEqual(graph["8"]["inputs"]["seed"], 11)
        self.assertEqual(graph["8"]["inputs"]["steps"], 4)     # Lightning: locked at 4
        self.assertEqual(graph["8"]["inputs"]["cfg"], 1.0)     # Lightning: locked at 1.0
        # and the caller got real bytes back
        self.assertEqual(base64.b64decode(json.loads(body)["data"][0]["b64_json"]), PNG_1x1)

    def test_an_upstream_execution_failure_becomes_a_502_with_the_reason(self):
        self.stub.reset("exec_error")
        status, body = self.call("POST", "/v1/images/generations", {"prompt": "boom"})
        self.assertEqual(status, 502)
        self.assertIn("CUDA out of memory", json.loads(body)["error"]["message"])

    def test_the_slot_is_released_after_an_upstream_failure(self):
        self.stub.reset("exec_error")
        self.call("POST", "/v1/images/generations", {"prompt": "boom"})
        self.stub.reset("ok")
        status, _ = self.call("POST", "/v1/images/generations", {"prompt": "recovered"})
        self.assertEqual(status, 200, "a failed generation left the global slot held")

    def test_health_reports_the_upstream_it_can_actually_see(self):
        self.stub.reset()
        status, body = self.call("GET", "/health", key=None)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["comfyui"]["reachable"])
        self.assertEqual(payload["comfyui"]["device"], "NVIDIA GB10")
        self.assertFalse(payload["busy"])

    def test_a_video_job_runs_through_the_async_path_and_serves_its_content(self):
        self.stub.reset()
        status, body = self.call("POST", "/v1/videos", {"prompt": "rain on a window", "seconds": 2})
        self.assertEqual(status, 202)
        job_id = json.loads(body)["id"]
        for _ in range(200):
            status, body = self.call("GET", f"/v1/videos/{job_id}")
            payload = json.loads(body)
            if payload["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(payload["status"], "completed", payload.get("error"))
        graph = self.stub.submissions[-1]["prompt"]
        self.assertEqual(graph["11"]["inputs"]["length"], 33)   # 32 frames -> nearest 4k+1
        self.assertEqual(graph["12"]["inputs"]["noise_seed"], graph["13"]["inputs"]["noise_seed"])
        self.assertEqual(payload["object"], "video")
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(graph["16"]["inputs"]["filename_prefix"], f"gx-video/{job_id}")
        status, content = self.call("GET", f"/v1/videos/{job_id}/content")
        self.assertEqual(status, 200)
        self.assertEqual(content, MP4_STUB)   # the primary output is the mp4, not the thumbnail
        status, content = self.call("GET", f"/v1/videos/{job_id}/content?variant=thumbnail")
        self.assertEqual(status, 200)
        self.assertEqual(content, PNG_1x1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
