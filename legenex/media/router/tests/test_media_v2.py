"""Tests for the D-031 media API: edits, variations, image-to-video, video edits.

Standard library only; no GPU, no ComfyUI. The staged-input directory is a
temporary directory so the tests can assert that sources are cleaned up.
"""

from __future__ import annotations

import dataclasses
import json
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router import uploads  # noqa: E402
from gx_media_router.config import Config  # noqa: E402
from gx_media_router.errors import ValidationError  # noqa: E402
from gx_media_router.server import build_server, edit_start_step  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.uploads import InputStore  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

from tests.test_router import MP4_STUB, PNG_1x1, FakeComfy  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk
            + struct.pack(">I", zlib.crc32(chunk)) + b"\x00" * 16)


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def webp_vp8x(width: int, height: int) -> bytes:
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") \
        + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    out = b""
    for k, val in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{val}\r\n").encode()
    for field, filename, ctype, data in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", out


class SniffTests(unittest.TestCase):
    def test_images_are_identified_with_dimensions(self):
        for data, ext, size in ((png(640, 480), "png", (640, 480)), (jpeg(800, 600), "jpg", (800, 600)),
                                (webp_vp8x(1000, 700), "webp", (1000, 700))):
            with self.subTest(ext=ext):
                info = uploads.sniff(data)
                self.assertEqual((info.kind, info.ext), ("image", ext))
                self.assertEqual((info.width, info.height), size)

    def test_videos_are_identified(self):
        self.assertEqual(uploads.sniff(MP4_STUB).ext, "mp4")
        self.assertEqual(uploads.sniff(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8).ext, "mov")
        self.assertEqual(uploads.sniff(b"\x1a\x45\xdf\xa3" + b"\x00" * 16).ext, "webm")

    def test_unsupported_content_is_rejected(self):
        for data in (b"GIF89a....", b"<html><script>", b"\x00\x00\x00\x18ftypavif" + b"\x00" * 8, b"", b"%PDF-1.7"):
            with self.subTest(data=data[:8]):
                with self.assertRaises(ValidationError):
                    uploads.validate(data, expect="image", max_bytes=10**6, max_pixels=10**8, max_side=8192)

    def test_kind_mismatch_is_rejected(self):
        with self.assertRaises(ValidationError):
            uploads.validate(MP4_STUB, expect="image", max_bytes=10**6, max_pixels=10**8, max_side=8192)
        with self.assertRaises(ValidationError):
            uploads.validate(png(64, 64), expect="video", max_bytes=10**6, max_pixels=10**8, max_side=8192)

    def test_limits(self):
        with self.assertRaises(ValidationError):
            uploads.validate(png(9000, 10), expect="image", max_bytes=10**6, max_pixels=10**9, max_side=8192)
        with self.assertRaises(ValidationError):
            uploads.validate(png(4000, 4000), expect="image", max_bytes=10**6, max_pixels=10**7, max_side=8192)
        with self.assertRaises(ValidationError):
            uploads.validate(png(8, 8), expect="image", max_bytes=10**6, max_pixels=10**7, max_side=8192)
        with self.assertRaises(ValidationError):
            uploads.validate(png(64, 64), expect="image", max_bytes=10, max_pixels=10**7, max_side=8192)

    def test_data_url_and_bare_base64(self):
        import base64
        b64 = base64.b64encode(PNG_1x1).decode()
        self.assertEqual(uploads.decode_data_url(f"data:image/png;base64,{b64}", "image"), PNG_1x1)
        self.assertEqual(uploads.decode_data_url(b64, "image"), PNG_1x1)
        with self.assertRaises(ValidationError):
            uploads.decode_data_url("not base64 !!", "image")

    def test_multipart_parsing(self):
        ctype, body = multipart({"prompt": "make it red", "strength": "0.5", "n": "2", "uncensored": "true"},
                                [("image[]", "../../etc/passwd", "image/png", png(32, 32))])
        fields, files = uploads.parse_multipart(ctype, body)
        coerced = uploads.coerce_form(fields)
        self.assertEqual(coerced, {"prompt": "make it red", "strength": 0.5, "n": 2, "uncensored": True})
        self.assertEqual(files[0].field, "image[]")
        self.assertEqual(files[0].data[:8], b"\x89PNG\r\n\x1a\n")

    def test_fit_to_pixels(self):
        w, h = uploads.fit_to_pixels(4000, 3000, 1024 * 1024)
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertAlmostEqual(w / h, 4 / 3, delta=0.05)
        self.assertLess(abs(w * h - 1024 * 1024) / (1024 * 1024), 0.05)
        w, h = uploads.fit_to_pixels(100, 5000, 1024 * 1024, max_side=2048)
        self.assertLessEqual(max(w, h), 2048)

    def test_edit_strength_mapping(self):
        self.assertEqual(edit_start_step(1.0), ("wan22-v2v-keyframe-edit", 0))
        self.assertEqual(edit_start_step(0.85), ("wan22-v2v-keyframe-edit", 0))
        self.assertEqual(edit_start_step(0.6), ("wan22-v2v-keyframe-edit", 1))
        self.assertEqual(edit_start_step(0.4), ("wan22-v2v-a14b-light", 2))
        self.assertEqual(edit_start_step(0.2), ("wan22-v2v-a14b-light", 3))


class InputStoreTests(unittest.TestCase):
    def test_put_remove_and_path_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = InputStore(Path(tmp))
            name = store.put(png(16, 16), uploads.sniff(png(16, 16)))
            self.assertRegex(name, r"^gx-in/[0-9a-f]{32}\.png$")
            self.assertTrue((Path(tmp) / name).is_file())
            store.remove("../../etc/passwd")
            store.remove("gx-in/../x.png")
            store.remove(name)
            self.assertFalse((Path(tmp) / name).exists())

    def test_purge_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = InputStore(Path(tmp), ttl_seconds=0)
            store.put(png(16, 16), uploads.sniff(png(16, 16)))
            time.sleep(0.01)
            self.assertEqual(store.purge_stale(), 1)

    def test_unavailable_directory_refuses_cleanly(self):
        store = InputStore(Path("/nonexistent/gx-test"))
        with self.assertRaises(ValidationError):
            store.put(png(16, 16), uploads.sniff(png(16, 16)))


class MediaApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key="test-key")
        cls.comfy = FakeComfy()
        cls.store = InputStore(Path(cls.tmp.name))
        cls.service = MediaService(cls.cfg, cls.comfy, WorkflowRegistry(WORKFLOW_DIR), inputs=cls.store)
        cls.server = build_server(cls.cfg, cls.service)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, *, ctype="application/json", key="test-key"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        if data is not None:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def staged_files(self):
        d = Path(self.tmp.name) / "gx-in"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def wait_video(self, job_id):
        for _ in range(200):
            status, body = self.call("GET", f"/v1/videos/{job_id}")
            if body.get("status") in ("completed", "failed"):
                return body
            time.sleep(0.02)
        self.fail("video job did not finish")

    # --- image edits ---------------------------------------------------
    def test_multipart_edit_runs_the_edit_template_and_cleans_up(self):
        ctype, body = multipart({"prompt": "make the shirt black", "model": "gx-image", "strength": "0.8"},
                                [("image", "photo.png", "image/png", png(1600, 1200))])
        status, payload = self.call("POST", "/v1/images/edits", body, ctype=ctype)
        self.assertEqual(status, 200, payload)
        graph = self.comfy.submitted[-1]
        self.assertEqual(graph["5"]["class_type"], "TextEncodeQwenImageEditPlus")
        self.assertEqual(graph["5"]["inputs"]["prompt"], "make the shirt black")
        self.assertRegex(graph["20"]["inputs"]["image"], r"^gx-in/[0-9a-f]{32}\.png$")
        self.assertEqual(graph["8"]["inputs"]["denoise"], 0.8)
        w, h = graph["21"]["inputs"]["width"], graph["21"]["inputs"]["height"]
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertAlmostEqual(w / h, 4 / 3, delta=0.05)
        self.assertEqual(payload["gx"]["operation"], "edit")
        self.assertEqual(payload["gx"]["source"]["width"], 1600)
        self.assertEqual(self.staged_files(), [], "the staged source was not removed")

    def test_json_edit_with_data_url_and_uncensored_flag(self):
        import base64
        b64 = base64.b64encode(jpeg(512, 512)).decode()
        status, payload = self.call("POST", "/v1/images/edits",
                                    {"prompt": "sunset beach background", "image": f"data:image/jpeg;base64,{b64}",
                                     "uncensored": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.comfy.submitted[-1]["12"]["inputs"]["strength_model"], 0.8)
        self.assertTrue(self.comfy.submitted[-1]["20"]["inputs"]["image"].endswith(".jpg"))

    def test_edit_rejects_missing_or_wrong_sources(self):
        status, _ = self.call("POST", "/v1/images/edits", {"prompt": "x"})
        self.assertEqual(status, 400)
        ctype, body = multipart({"prompt": "x"}, [("image", "a.png", "image/png", MP4_STUB)])
        status, payload = self.call("POST", "/v1/images/edits", body, ctype=ctype)
        self.assertEqual(status, 400)
        self.assertIn("unsupported image format", payload["error"]["message"])
        ctype, body = multipart({}, [("image", "a.png", "image/png", png(64, 64))])
        status, _ = self.call("POST", "/v1/images/edits", body, ctype=ctype)
        self.assertEqual(status, 400, "an edit without an instruction must be refused")

    def test_edit_endpoint_refuses_non_edit_workflows(self):
        ctype, body = multipart({"prompt": "x", "workflow": "qwen-image-2512-lightning"},
                                [("image", "a.png", "image/png", png(64, 64))])
        status, payload = self.call("POST", "/v1/images/edits", body, ctype=ctype)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["param"], "workflow")
        self.assertEqual(self.staged_files(), [])

    def test_variation_uses_default_prompt_and_partial_denoise(self):
        ctype, body = multipart({}, [("image", "a.png", "image/png", png(512, 768))])
        status, payload = self.call("POST", "/v1/images/variations", body, ctype=ctype)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["gx"]["operation"], "variation")
        self.assertEqual(self.comfy.submitted[-1]["8"]["inputs"]["denoise"], 0.75)
        self.assertIn("variation", self.comfy.submitted[-1]["5"]["inputs"]["prompt"])

    def test_failed_edit_still_removes_the_source(self):
        from gx_media_router.errors import UpstreamError
        self.comfy.fail_with = UpstreamError("boom")
        try:
            ctype, body = multipart({"prompt": "x"}, [("image", "a.png", "image/png", png(64, 64))])
            status, _ = self.call("POST", "/v1/images/edits", body, ctype=ctype)
        finally:
            self.comfy.fail_with = None
        self.assertEqual(status, 502)
        self.assertEqual(self.staged_files(), [])

    # --- videos ---------------------------------------------------------
    def test_openai_multipart_video_with_input_reference_is_image_to_video(self):
        ctype, body = multipart({"prompt": "the camera slowly orbits", "model": "gx-video", "seconds": "3",
                                 "size": "640x480"},
                                [("input_reference", "start.png", "image/png", png(640, 480))])
        status, payload = self.call("POST", "/v1/videos", body, ctype=ctype)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["object"], "video")
        self.assertIn(payload["status"], ("queued", "in_progress", "completed"))
        self.assertEqual(payload["operation"], "i2v")
        done = self.wait_video(payload["id"])
        self.assertEqual(done["status"], "completed", done)
        graph = self.comfy.submitted[-1]
        self.assertEqual(graph["11"]["class_type"], "WanImageToVideo")
        self.assertEqual(graph["11"]["inputs"]["start_image"], ["20", 0])
        self.assertEqual(graph["11"]["inputs"]["length"], 49)
        self.assertIn("thumbnail_url", done)
        self.assertEqual(self.staged_files(), [])
        status, content = self.call("GET", f"/v1/videos/{payload['id']}/content")
        self.assertEqual((status, content), (200, MP4_STUB))

    def test_json_text_to_video_uses_the_uncensored_template(self):
        status, payload = self.call("POST", "/v1/videos", {"prompt": "rain", "seconds": 2})
        self.assertEqual(status, 202)
        self.assertEqual(payload["workflow"], "wan22-t2v-a14b-uncensored")
        done = self.wait_video(payload["id"])
        self.assertEqual(self.comfy.submitted[-1]["5"]["inputs"]["lora_name"], "Wan2.2_LightX2V_high_n54vv.safetensors")
        self.assertEqual(done["seconds"], "2.06")

    def test_video_edit_from_upload_light_and_strong(self):
        for strength, workflow, start_node, start in ((0.2, "wan22-v2v-a14b-light", "13", 3),
                                                      (0.9, "wan22-v2v-keyframe-edit", "12", 0),
                                                      (0.6, "wan22-v2v-keyframe-edit", "12", 1)):
            with self.subTest(strength=strength):
                ctype, body = multipart({"prompt": "make it night", "strength": str(strength)},
                                        [("video", "clip.mp4", "video/mp4", MP4_STUB)])
                status, payload = self.call("POST", "/v1/videos/edits", body, ctype=ctype)
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["workflow"], workflow)
                done = self.wait_video(payload["id"])
                self.assertEqual(done["status"], "completed", done)
                graph = self.comfy.submitted[-1]
                self.assertEqual(graph[start_node]["inputs"]["start_at_step"], start)
                self.assertRegex(graph["20"]["inputs"]["file"], r"^gx-in/[0-9a-f]{32}\.mp4$")
                self.assertEqual(graph["15"]["inputs"]["fps"], ["22", 2], "the source frame rate must be kept")
                if workflow == "wan22-v2v-keyframe-edit":
                    self.assertEqual(graph["37"]["inputs"]["prompt"], "make it night")
                    self.assertEqual(graph["11"]["inputs"]["length"], graph["23"]["inputs"]["length"])
                    self.assertEqual(graph["12"]["inputs"]["latent_image"], ["25", 0])
        self.assertEqual(self.staged_files(), [])

    def test_video_edit_rejects_an_image(self):
        ctype, body = multipart({"prompt": "x"}, [("video", "clip.mp4", "video/mp4", png(64, 64))])
        status, _ = self.call("POST", "/v1/videos/edits", body, ctype=ctype)
        self.assertEqual(status, 400)

    def test_remix_edits_an_earlier_job_and_records_lineage(self):
        status, first = self.call("POST", "/v1/videos", {"prompt": "a red car"})
        self.wait_video(first["id"])
        status, remix = self.call("POST", f"/v1/videos/{first['id']}/remix", {"prompt": "make it night"})
        self.assertEqual(status, 202, remix)
        self.assertEqual(remix["remixed_from_video_id"], first["gx_id"])
        done = self.wait_video(remix["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["operation"], "v2v")
        status, by_ref = self.call("POST", "/v1/videos/edits", {"prompt": "turn the sky stormy",
                                                                "video": {"id": first["id"]}})
        self.assertEqual(status, 202, by_ref)
        self.assertEqual(by_ref["remixed_from_video_id"], first["gx_id"])
        self.wait_video(by_ref["id"])
        self.assertEqual(self.staged_files(), [])

    def test_remix_of_unknown_or_image_job(self):
        status, _ = self.call("POST", "/v1/videos/video-0000000000000000/remix", {"prompt": "x"})
        self.assertEqual(status, 404)
        ctype, body = multipart({"prompt": "x"}, [("image", "a.png", "image/png", png(64, 64))])
        status, edited = self.call("POST", "/v1/images/edits", {"prompt": "x", "response_format": "url",
                                                                "image": __import__("base64").b64encode(png(64, 64)).decode()})
        self.assertEqual(status, 200)
        image_job = edited["gx"]["id"]
        status, payload = self.call("POST", f"/v1/videos/{image_job}/remix", {"prompt": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(self.staged_files(), [])

    def test_models_are_freed_only_when_the_model_set_changes(self):
        self.call("POST", "/v1/images/generations", {"prompt": "a"})
        before = self.comfy.frees
        self.call("POST", "/v1/images/generations", {"prompt": "b"})
        self.assertEqual(self.comfy.frees, before, "same model set must not reload")
        status, payload = self.call("POST", "/v1/videos", {"prompt": "c"})
        self.wait_video(payload["id"])
        self.assertEqual(self.comfy.frees, before + 1, "switching to video must free the image weights")

    def test_video_ids_are_gateway_encoded_and_accepted_in_both_forms(self):
        from gx_media_router.jobs import gateway_video_id, plain_job_id
        status, created = self.call("POST", "/v1/videos", {"prompt": "encoded ids"})
        self.assertTrue(created["id"].startswith("video_"))
        self.assertNotIn("/", created["id"])
        self.assertNotIn("+", created["id"])
        plain = created["gx_id"]
        self.assertEqual(plain_job_id(created["id"]), plain)
        self.assertEqual(gateway_video_id(plain), created["id"])
        for ident in (created["id"], plain, urllib.parse.quote(created["id"], safe="")):
            with self.subTest(ident=ident[:12]):
                status, body = self.call("GET", f"/v1/videos/{ident}")
                self.assertEqual(status, 200)
                self.assertEqual(body["gx_id"], plain)
        self.wait_video(created["id"])
        status, remix = self.call("POST", f"/v1/videos/{created['id']}/remix", {"prompt": "x"})
        self.assertEqual(status, 202)
        self.assertEqual(remix["remixed_from_video_id"], plain)
        self.wait_video(remix["id"])

    def test_video_content_is_the_video_even_when_an_image_output_comes_first(self):
        from gx_media_router.comfy import Artefact, Result
        original = self.comfy.wait

        def wait(prompt_id, **kw):
            return Result(prompt_id, (
                Artefact("keyframe_00001_.png", "gx-video", "output", "images"),
                Artefact("out_00001_.mp4", "gx-video", "output", "images"),
                Artefact("thumb_00001_.png", "gx-video", "output", "images", thumbnail=True),
            ), 0.1)
        self.comfy.wait = wait
        try:
            status, created = self.call("POST", "/v1/videos", {"prompt": "order"})
            self.wait_video(created["id"])
            status, content = self.call("GET", f"/v1/videos/{created['id']}/content")
        finally:
            self.comfy.wait = original
        self.assertEqual((status, content), (200, MP4_STUB))

    def test_idle_free_hands_the_node_back(self):
        self.call("POST", "/v1/images/generations", {"prompt": "idle"})
        before = self.comfy.frees
        svc = self.service
        self.assertFalse(svc.free_if_idle(), "must not free right after a job")
        svc._last_activity -= svc.cfg.idle_free_seconds + 1
        self.assertTrue(svc.free_if_idle())
        self.assertEqual(self.comfy.frees, before + 1)
        self.assertFalse(svc.free_if_idle(), "nothing left to free")

    def test_free_request_hands_the_node_to_gx_reason(self):
        status, _ = self.call("POST", "/v1/admin/free", {}, key="wrong")
        self.assertEqual(status, 401)
        self.call("POST", "/v1/images/generations", {"prompt": "before reason"})
        before = self.comfy.frees
        status, body = self.call("POST", "/v1/admin/free", {})
        self.assertEqual((status, body["freed"]), (200, True))
        self.assertTrue(body["models"])
        self.assertEqual(self.comfy.frees, before + 1)
        # never while a generation holds the slot
        self.assertTrue(self.service.slot.acquire("test-job", 1.0))
        try:
            status, body = self.call("POST", "/v1/admin/free", {})
        finally:
            self.service.slot.release()
        self.assertEqual((status, body["freed"]), (409, False))
        self.assertIn("test-job", body["reason"])
        self.assertEqual(self.comfy.frees, before + 1)

    def test_memory_admission_refuses_instead_of_swapping(self):
        svc = self.service
        meminfo = Path(self.tmp.name) / "meminfo"

        def set_avail(gib):
            meminfo.write_text(f"MemTotal: 127535600 kB\nMemAvailable: {int(gib * 1024 * 1024)} kB\n")

        original_cfg = svc.cfg
        svc.cfg = dataclasses.replace(original_cfg, meminfo_path=str(meminfo))
        try:
            # gx-reason loaded, nothing of ours resident: an image needs 60 GiB -> refused, nothing freed
            svc.comfy.free(unload_models=True, free_memory=True)
            svc._resident_models = frozenset()
            before = self.comfy.frees
            set_avail(20)
            status, body = self.call("POST", "/v1/images/generations", {"prompt": "no room"})
            self.assertEqual(status, 503)
            self.assertEqual(body["error"]["code"], "insufficient_memory")
            self.assertIn("gx-reason", body["error"]["message"])
            self.assertEqual(self.comfy.frees, before)
            self.assertEqual(svc._resident_models, frozenset(), "refused weights are not resident")
            # enough memory -> runs
            set_avail(70)
            status, _ = self.call("POST", "/v1/images/generations", {"prompt": "room"})
            self.assertEqual(status, 200)
            # same weights already loaded: only the warm need applies
            set_avail(10)
            status, _ = self.call("POST", "/v1/images/generations", {"prompt": "warm"})
            self.assertEqual(status, 200)
            # a video job while memory is short fails its job, with the reason
            set_avail(70)  # what is left with gx-reason loaded
            status, created = self.call("POST", "/v1/videos", {"prompt": "no room for video"})
            self.assertEqual(status, 202)
            job = self.wait_video(created["id"])
            self.assertEqual(job["status"], "failed")
            self.assertIn("needs about 76 GiB", json.dumps(job))
            # unreadable meminfo never blocks generation
            svc.cfg = dataclasses.replace(original_cfg, meminfo_path=str(Path(self.tmp.name) / "missing"))
            status, _ = self.call("POST", "/v1/images/generations", {"prompt": "no meminfo"})
            self.assertEqual(status, 200)
        finally:
            svc.cfg = original_cfg

    def test_listing_and_workflows(self):
        status, body = self.call("GET", "/v1/videos")
        self.assertEqual(status, 200)
        self.assertTrue(all(v["object"] == "video" for v in body["data"]))
        status, body = self.call("GET", "/v1/workflows")
        ops = {w["name"]: w["operation"] for w in body["data"]}
        self.assertEqual(ops["qwen-image-edit-2511"], "edit")
        self.assertEqual(ops["wan22-i2v-a14b-uncensored"], "i2v")

    def test_new_routes_require_the_key(self):
        for path in ("/v1/images/edits", "/v1/images/variations", "/v1/videos/edits",
                     "/v1/videos/video-x/remix"):
            with self.subTest(path=path):
                status, _ = self.call("POST", path, {"prompt": "x"}, key=None)
                self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
