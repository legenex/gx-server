"""Hermetic tests for the D-034/D-035 backend: media library, Create jobs,
API keys, Hugging Face parsing/classification, Model Manager safety."""

from __future__ import annotations

import base64
import io
import json
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from support import TempEnv, fake_key

from gx_control_ui import auth
from gx_control_ui import server as srv
from gx_control_ui.api_keys import KeyError_, KeyManager, validate_request
from gx_control_ui.hf import HFError, classify, parse_ref
from gx_control_ui.media_jobs import JobError, MediaJobs, RouterClient, validate
from gx_control_ui.media_library import LibraryError, MediaLibrary, MediaTools, NewAsset
from gx_control_ui.model_manager import ManagerError, _pick_gguf, read_macros, set_macros, validate_target

PASSWORD = "Test-Password-For-Suite-9"


def png(w=64, h=48, colour=(200, 30, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------- library
class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.lib = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))

    def tearDown(self):
        self.env.cleanup()

    def test_add_get_search_update_delete_with_lineage(self):
        a = self.lib.add(NewAsset(type="image", ext="png", operation="generate", data=png(), prompt="a red box",
                                  model_alias="gx-image", seed=5))
        self.assertRegex(a["id"], r"^a_[0-9a-f]{24}$")
        self.assertEqual((a["width"], a["height"]), (64, 48))
        self.assertTrue(self.lib.thumb_path(a["id"]).is_file())
        b = self.lib.add(NewAsset(type="image", ext="png", operation="edit", data=png(colour=(0, 0, 255)),
                                  prompt="make it blue", parent_id=a["id"]))
        full = self.lib.get(a["id"], lineage=True)
        self.assertEqual([c["id"] for c in full["children"]], [b["id"]])
        self.assertEqual(self.lib.get(b["id"], lineage=True)["ancestors"][0]["id"], a["id"])
        self.assertEqual(self.lib.search(q="box")["total"], 1)
        self.assertEqual(self.lib.search(operation="edit")["total"], 1)
        self.assertEqual(self.lib.search(type_="video")["total"], 0)
        self.assertTrue(self.lib.update(a["id"], favourite=True)["favourite"])
        self.assertEqual(self.lib.search(favourite=True)["total"], 1)
        self.assertEqual(self.lib.update(a["id"], title="  Named ")["title"], "Named")
        res = self.lib.delete([a["id"]])
        self.assertEqual(res["deleted"], [a["id"]])
        self.assertFalse(self.lib.file_path(full).exists())
        child = self.lib.get(b["id"], lineage=True)
        self.assertTrue(child["parent_deleted"])
        self.assertTrue(child["ancestors"][0]["deleted"])

    def test_rejections(self):
        with self.assertRaises(LibraryError):
            self.lib.add(NewAsset(type="image", ext="mp4", operation="generate", data=b"x"))
        with self.assertRaises(LibraryError):
            self.lib.add(NewAsset(type="image", ext="png", operation="hack", data=png()))
        with self.assertRaises(LibraryError):
            self.lib.add(NewAsset(type="image", ext="png", operation="edit", data=png(), parent_id="a_" + "0" * 24))
        for bad in ("../etc/passwd", "a_xyz", "", None):
            with self.assertRaises(LibraryError):
                self.lib.get(bad)  # type: ignore[arg-type]
        with self.assertRaises(LibraryError):
            self.lib.search(sort="id; DROP TABLE assets")
        with self.assertRaises(LibraryError):
            self.lib.update("a_" + "1" * 24, title="x")
        self.assertEqual(self.lib.search(q="%' OR 1=1 --")["total"], 0)

    def test_zip_contains_only_the_selection(self):
        ids = [self.lib.add(NewAsset(type="image", ext="png", operation="generate", data=png(), prompt=f"p{i}"))["id"]
               for i in range(3)]
        path, count = self.lib.build_zip(ids[:2])
        names = zipfile.ZipFile(path).namelist()
        self.assertEqual(count, 2)
        self.assertEqual(len(names), 3)
        self.assertIn("gx-media-manifest.json", names)
        self.assertTrue(all("/" not in n and ".." not in n for n in names))
        with self.assertRaises(LibraryError):
            self.lib.build_zip([])
        with self.assertRaises(LibraryError):
            self.lib.build_zip(["a_" + "2" * 24])

    def test_schema_version(self):
        self.assertEqual(self.lib.schema_version, 1)
        MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))  # re-open: migrations idempotent


# --------------------------------------------------------------- jobs
class FakeRouter(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        FakeRouter.calls.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"), data))
        if self.path in ("/v1/images/generations", "/v1/images/edits", "/v1/images/variations"):
            self._json(200, {"data": [{"b64_json": base64.b64encode(png(1024, 1024)).decode()}],
                             "gx": {"id": "image-1", "workflow": "qwen-image-2512-uncensored", "size": "1024x1024",
                                    "seed": 7}})
        else:
            self._json(200, {"id": "video_abc", "status": "queued", "phase": "queued", "workflow": "wan22-t2v-a14b-uncensored"})

    def do_GET(self):
        if self.path.endswith("/content"):
            body = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(200, {"id": "video_abc", "status": "completed", "phase": "ready", "size": "640x640",
                             "workflow": "wan22-t2v-a14b-uncensored", "seed": 3, "elapsed_seconds": 1.0})


class MediaJobTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.lib = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))
        FakeRouter.calls = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.key = fake_key("gxm-")
        router = RouterClient(f"http://127.0.0.1:{self.httpd.server_address[1]}", lambda: self.key)
        self.jobs = MediaJobs(self.lib, router, model_identity=lambda wf: {"repository": "org/repo", "revision": "a" * 40},
                              poll_interval=0.05)

    def tearDown(self):
        self.httpd.shutdown()
        self.env.cleanup()

    def wait(self, job_id):
        for _ in range(200):
            j = self.jobs.get(job_id)
            if j["phase"] in ("ready", "failed"):
                return j
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_generate_then_edit_keeps_the_original(self):
        j = self.wait(self.jobs.submit({"kind": "t2i", "prompt": "a cat", "size": "1024x1024"}, user="u")["id"])
        self.assertEqual(j["phase"], "ready", j)
        src = self.lib.get(j["assets"][0])
        self.assertEqual(src["model_repo"], "org/repo")
        self.assertEqual(FakeRouter.calls[0][1], f"Bearer {self.key}")
        e = self.wait(self.jobs.submit({"kind": "edit", "prompt": "make it blue", "source_id": src["id"]}, user="u")["id"])
        self.assertEqual(e["phase"], "ready", e)
        edited = self.lib.get(e["assets"][0])
        self.assertEqual(edited["parent_id"], src["id"])
        self.assertEqual(self.lib.get(src["id"])["sha256"], src["sha256"])
        path, ctype, body = FakeRouter.calls[-1][0], FakeRouter.calls[-1][2], FakeRouter.calls[-1][3]
        self.assertEqual(path, "/v1/images/edits")
        self.assertTrue(ctype.startswith("multipart/form-data"))
        self.assertIn(b"make it blue", body)

    def test_video_generation_is_stored_as_generate(self):
        j = self.wait(self.jobs.submit({"kind": "t2v", "prompt": "waves", "seconds": 2}, user="u")["id"])
        self.assertEqual(j["phase"], "ready", j)
        v = self.lib.get(j["assets"][0])
        self.assertEqual((v["type"], v["operation"], v["ext"]), ("video", "generate", "mp4"))

    def test_validation(self):
        for body in ({"kind": "nope"}, {"kind": "t2i"}, {"kind": "t2i", "prompt": "x", "size": "99x99"},
                     {"kind": "edit", "prompt": "x"}, {"kind": "t2v", "prompt": "x", "seconds": 99},
                     {"kind": "t2i", "prompt": "x", "uncensored": "yes"}, {"kind": "t2i", "prompt": "x" * 5000}):
            with self.subTest(body=str(body)[:40]), self.assertRaises(JobError):
                validate(str(body.get("kind")), body)
        self.assertEqual(validate("variation", {"source_id": "a_" + "1" * 24})["kind"], "variation")

    def test_source_type_must_match(self):
        a = self.lib.add(NewAsset(type="image", ext="png", operation="upload", data=png()))
        with self.assertRaises(JobError):
            self.jobs.submit({"kind": "v2v", "prompt": "night", "source_id": a["id"]}, user="u")


# --------------------------------------------------------------- keys
class FakeLiteLLM(BaseHTTPRequestHandler):
    keys: dict = {}
    auth = ""

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {FakeLiteLLM.auth}":
            return self._json(401, {"error": {"message": "no"}})
        if "size=200" in self.path:
            return self._json(422, {"detail": "size too large"})
        self._json(200, {"keys": list(FakeLiteLLM.keys.values())})

    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {FakeLiteLLM.auth}":
            return self._json(401, {"error": {"message": "no"}})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/key/generate":
            token = f"{len(FakeLiteLLM.keys):064x}"
            FakeLiteLLM.keys[token] = {"token": token, "key_alias": body["key_alias"], "key_name": "sk-...zzzz",
                                       "models": body["models"], "metadata": body["metadata"], "expires": None,
                                       "created_at": "2026-09-17T00:00:00Z"}
            return self._json(200, {"key": fake_key(), "token": token, "expires": None})
        if self.path == "/key/delete":
            for k in body["keys"]:
                FakeLiteLLM.keys.pop(k, None)
            return self._json(200, {"deleted_keys": body["keys"]})
        self._json(404, {})


class KeyTests(unittest.TestCase):
    def setUp(self):
        FakeLiteLLM.keys = {}
        FakeLiteLLM.auth = fake_key()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLiteLLM)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.km = KeyManager(f"http://127.0.0.1:{self.httpd.server_address[1]}", lambda: FakeLiteLLM.auth)

    def tearDown(self):
        self.httpd.shutdown()

    def test_create_list_replace_revoke(self):
        created = self.km.create({"name": "kilo", "models": ["gx-fast", "gx-mini"]}, user="admin")
        self.assertTrue(created["secret"].startswith("sk-"))
        self.assertEqual(created["models"], ["gx-mini", "gx-fast"])
        listing = self.km.list()
        self.assertEqual(len(listing), 1)
        self.assertNotIn(created["secret"], json.dumps(listing))
        self.assertTrue(listing[0]["managed_by_ui"])
        new = self.km.replace(listing[0]["id"], user="admin")
        self.assertEqual(new["replaced"], listing[0]["id"])
        self.assertEqual(len(self.km.list()), 1)
        self.km.revoke(self.km.list()[0]["id"])
        self.assertEqual(self.km.list(), [])
        with self.assertRaises(KeyError_):
            self.km.revoke("f" * 64)

    def test_validation_and_missing_master(self):
        for body in ({"name": "", "models": ["gx-mini"]}, {"name": "x", "models": []},
                     {"name": "x", "models": ["gpt-4"]}, {"name": "x", "models": ["gx-mini"], "expiry": "forever"},
                     {"name": "x", "models": ["gx-mini"], "rpm_limit": 0}, {"name": "<script>", "models": ["gx-mini"]}):
            with self.subTest(body=body), self.assertRaises(KeyError_):
                validate_request(body)
        with self.assertRaises(KeyError_):
            KeyManager(self.km.base, lambda: None).list()


# --------------------------------------------------------------- HF + manager
class HFTests(unittest.TestCase):
    def test_parse_ref(self):
        self.assertEqual(parse_ref("org/name"), ("org/name", None))
        self.assertEqual(parse_ref("org/name@abc123"), ("org/name", "abc123"))
        self.assertEqual(parse_ref("https://huggingface.co/org/name/tree/main"), ("org/name", "main"))
        self.assertEqual(parse_ref("https://hf.co/org/name/blob/deadbeef/config.json"), ("org/name", "deadbeef"))
        for bad in ("https://evil.example/org/name", "https://huggingface.co/datasets/x/y", "org", "../x/y",
                    "org/na me", "org/name@../../x", "https://huggingface.co/spaces/a/b"):
            with self.subTest(bad=bad), self.assertRaises(HFError):
                parse_ref(bad)

    def test_classify(self):
        gguf = classify("a/b", ["gguf"], [{"path": "m-Q4_K_M.gguf", "size": 1}, {"path": "mmproj-F16.gguf", "size": 1}], None, {})
        self.assertEqual((gguf["kind"], gguf["vision"]), ("checkpoint", True))
        self.assertIn("gx-mini", gguf["candidate_aliases"])
        overlay = classify("a/b", ["refusal-direction"], [{"path": "refusal_dirs.safetensors", "size": 1}], None, {})
        self.assertEqual(overlay["kind"], "not_a_checkpoint")
        lora = classify("a/b", ["lora"], [{"path": "x.safetensors", "size": 1}], None, {})
        self.assertEqual(lora["kind"], "lora")
        self.assertTrue(lora["warnings"])
        remote = classify("a/b", [], [{"path": "model.safetensors", "size": 1}, {"path": "config.json", "size": 1}],
                          {"architectures": ["X"], "auto_map": {"AutoModel": "m.X"}}, {})
        self.assertTrue(remote["trust_remote_code"])


class ManagerTests(unittest.TestCase):
    def test_target_confinement(self):
        self.assertEqual(str(validate_target("gguf", "abc")), "/srv/models/gguf/abc")
        for cat, name in (("etc", "x"), ("gguf", "../x"), ("gguf", ""), ("vllm", "a/b"), ("gguf", ".hidden")):
            with self.subTest(cat=cat, name=name), self.assertRaises(ManagerError):
                validate_target(cat, name)

    def test_macro_editing_keeps_everything_else(self):
        text = 'macros:\n  gx_fast_model_dir: "/models/vllm/A"  # note\n  other: "x"\n'
        out = set_macros(text, {"gx_fast_model_dir": "/models/vllm/B"})
        self.assertEqual(out, 'macros:\n  gx_fast_model_dir: "/models/vllm/B"  # note\n  other: "x"\n')
        conf = 'GXMAX_MODEL_DIR="${GXMAX_MODEL_DIR:-/srv/models/deepseek/A}"\n'
        self.assertEqual(read_macros(set_macros(conf, {"GXMAX_MODEL_DIR": "/srv/x"}))["GXMAX_MODEL_DIR"], "/srv/x")
        with self.assertRaises(ManagerError):
            set_macros(text, {"missing": "x"})

    def test_gguf_pick(self):
        self.assertEqual(_pick_gguf(["m-00001-of-00002.gguf", "m-00002-of-00002.gguf"], 1)["model"], "m-00001-of-00002.gguf")
        picked = _pick_gguf(["a/mmproj-BF16.gguf", "a/x-Q8_0.gguf", "a/x-Q4_K_M.gguf"], 1)
        self.assertEqual((picked["model"], picked["mmproj"]), ("a/x-Q4_K_M.gguf", "a/mmproj-BF16.gguf"))


# --------------------------------------------------------------- routes
class RouteSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.app, servers = srv.build(self.env.cfg)
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.cleanup()

    def req(self, method, path, body=None, headers=None, raw=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def login(self):
        status, headers, body = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]

    def test_new_routes_require_a_session(self):
        for method, path in (("GET", "/api/media/assets"), ("GET", "/api/media/jobs"), ("GET", "/api/keys"),
                             ("GET", "/api/manager/inventory"), ("GET", "/api/manager/hf-token"),
                             ("POST", "/api/media/jobs"), ("POST", "/api/keys"), ("POST", "/api/manager/delete"),
                             ("POST", "/api/media/delete"), ("GET", "/api/media/assets/a_" + "0" * 24 + "/file")):
            with self.subTest(path=path):
                self.assertEqual(self.req(method, path, {} if method == "POST" else None)[0], 401)

    def test_writes_require_csrf_and_confirmation(self):
        cookie, csrf = self.login()
        base = {"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.req("POST", "/api/media/delete", {"ids": []}, base)[0], 403)
        self.assertEqual(self.req("POST", "/api/media/delete", {"ids": ["a_" + "0" * 24]},
                                  {**base, "X-CSRF-Token": csrf})[0], 400)
        self.assertEqual(self.req("POST", "/api/keys/" + "a" * 64 + "/revoke", {}, {**base, "X-CSRF-Token": csrf})[0], 400)

    def test_upload_and_serve_with_range(self):
        cookie, csrf = self.login()
        h = {"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.port}", "X-CSRF-Token": csrf,
             "Content-Type": "image/png", "X-Title": "hello%20there"}
        status, _, body = self.req("POST", "/api/media/upload", raw=png(), headers=h)
        self.assertEqual(status, 200, body)
        asset = json.loads(body)
        self.assertEqual((asset["operation"], asset["title"]), ("upload", "hello there"))
        status, headers, part = self.req("GET", asset["url"], headers={"Cookie": cookie, "Range": "bytes=0-7"})
        self.assertEqual((status, part), (206, b"\x89PNG\r\n\x1a\n"))
        bad = {**h, "Content-Type": "text/html"}
        self.assertEqual(self.req("POST", "/api/media/upload", raw=b"<script>", headers=bad)[0], 400)
        fake = {**h, "Content-Type": "image/png"}
        self.assertEqual(self.req("POST", "/api/media/upload", raw=b"GIF89a....", headers=fake)[0], 400)
        status, _, body = self.req("POST", "/api/media/zip", {"ids": [asset["id"]]},
                                   {"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.port}", "X-CSRF-Token": csrf})
        url = json.loads(body)["url"]
        status, headers, blob = self.req("GET", url, headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(blob)).namelist()), 2)
        self.assertEqual(self.req("GET", url, headers={"Cookie": cookie})[0], 404)


if __name__ == "__main__":
    unittest.main()
