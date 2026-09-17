"""Build V3 WAN: Wan 2.2 LoRA library, pairing, presets, workflow generation,
generation history and workflow export (D-040).

Hermetic: the real Control Center server (offline mode) in front of the REAL
media router code (e2e/wan_router_stub.py: LoRA catalogue, generator, video
worker) with a fake ComfyUI and synthetic LoRA headers. Nothing reaches a node.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
import unittest

from support import UI_DIR, TempEnv

from gx_control_ui import auth
from gx_control_ui import server as srv
from gx_control_ui.wan_video import (DEFAULTS, build_library, entry_id, human_error, sanitize_graph,
                                     snap_frames, summarize_chains)

sys.path.insert(0, str(UI_DIR / "e2e"))
from wan_router_stub import WanRouterStub  # noqa: E402

PASSWORD = "Wan-Test-Password-2026"
BUILTIN = {"wp_5c1e7a2b90d34f01": "Cinematic Realism", "wp_7f3a9c4d12e84b02": "High Detail",
           "wp_2b8d6e1f47a54c03": "Character Consistency", "wp_9e4c0b7a35f14d04": "Motion Style",
           "wp_1a6f8e2c59b74e05": "Custom 1"}


def f(name, noise, pair_key="", compat="compatible", valid=True, usable=True, **kw):
    return {"name": name, "noise": noise, "pair_key": pair_key, "compatibility": compat, "valid": valid,
            "usable": usable, "size": 10, "comfy_visible": True, "shadowed_by": None, **kw}


class LibraryModelTests(unittest.TestCase):
    """build_library is pure: grouping, pairing precedence and apply rules."""

    def lib(self, files, pairs=(), unpaired=(), settings=None):
        return build_library(list(files), list(pairs), set(unpaired), settings or {}, {})

    def by_kind(self, lib):
        return {(e["kind"], e["high_file"], e["low_file"], e["file"]): e for e in lib["entries"]}

    def test_auto_pairs_need_exactly_one_high_and_one_low(self):
        lib = self.lib([f("a_high.safetensors", "high", "::a"), f("a_low.safetensors", "low", "::a"),
                        f("b_high.safetensors", "high", "::b"), f("b2_high.safetensors", "high", "::b"),
                        f("b_low.safetensors", "low", "::b")])
        pairs = [e for e in lib["entries"] if e["kind"] == "pair"]
        self.assertEqual([(p["high_file"], p["low_file"], p["pair_source"]) for p in pairs],
                         [("a_high.safetensors", "a_low.safetensors", "auto")])
        singles = sorted(e["pair_state"] for e in lib["entries"] if e["kind"] != "pair")
        self.assertEqual(singles, ["high_only", "high_only", "low_only"])
        self.assertEqual(pairs[0]["apply_options"], ["pair"])
        self.assertEqual(pairs[0]["id"], entry_id(["a_high.safetensors", "a_low.safetensors"]))

    def test_manual_pairs_win_and_unpaired_files_stay_apart(self):
        files = [f("a_high.safetensors", "high", "::a"), f("a_low.safetensors", "low", "::a"),
                 f("x_high.safetensors", "high", "::x")]
        lib = self.lib(files, pairs=[{"id": "p_1", "high_name": "x_high.safetensors",
                                      "low_name": "a_low.safetensors"}])
        kinds = self.by_kind(lib)
        manual = kinds[("pair", "x_high.safetensors", "a_low.safetensors", None)]
        self.assertEqual((manual["pair_source"], manual["pair_id"]), ("manual", "p_1"))
        self.assertEqual(kinds[("high", "a_high.safetensors", None, "a_high.safetensors")]["pair_state"], "high_only")
        lib = self.lib(files[:2], unpaired={"a_high.safetensors", "a_low.safetensors"})
        self.assertEqual(sorted(e["kind"] for e in lib["entries"]), ["high", "low"])

    def test_broken_manual_pair_is_unresolved_and_limited(self):
        lib = self.lib([f("x_high.safetensors", "high")],
                       pairs=[{"id": "p_1", "high_name": "x_high.safetensors", "low_name": "gone_low.safetensors"}])
        e = lib["entries"][0]
        self.assertEqual((e["pair_state"], e["unresolved"], e["usable"]), ("broken_low", True, False))
        self.assertEqual(e["apply_options"], ["high"])
        self.assertTrue(any("low-noise file missing" in p for p in e["problems"]))

    def test_general_and_unknown_files_need_an_explicit_branch(self):
        lib = self.lib([f("grain.safetensors", "general"), f("odd.safetensors", "unknown", compat="unknown")])
        for e in lib["entries"]:
            self.assertEqual(e["apply_options"], ["high", "low", "both"])
        odd = next(e for e in lib["entries"] if e["kind"] == "unknown")
        self.assertEqual((odd["unresolved"], odd["usable"]), (True, False))
        lib = self.lib([f("odd.safetensors", "unknown", compat="unknown")],
                       settings={entry_id(["odd.safetensors"]): {"allow_unknown": 1}})
        self.assertTrue(lib["entries"][0]["usable"])

    def test_invalid_incompatible_and_shadowed(self):
        lib = self.lib([f("bad.safetensors", "unknown", compat="incompatible", valid=False, usable=False,
                          error="truncated"),
                        f("dup.safetensors", "high", shadowed_by="abc")])
        self.assertEqual(len(lib["entries"]), 1)
        self.assertEqual(lib["entries"][0]["compatibility"], "incompatible")
        self.assertEqual(len(lib["shadowed_files"]), 1)

    def test_settings_order_and_names(self):
        a = [f("Style_high_noise.safetensors", "high", "::s"), f("Style_low_noise.safetensors", "low", "::s"),
             f("zeta.safetensors", "general")]
        pid = entry_id(["Style_high_noise.safetensors", "Style_low_noise.safetensors"])
        lib = self.lib(a, settings={pid: {"position": 5, "display_name": None, "tags": ["x"]},
                                    entry_id(["zeta.safetensors"]): {"position": 1, "display_name": "Zeta grain"}})
        self.assertEqual([e["display_name"] for e in lib["entries"]], ["Zeta grain", "Style"])
        self.assertEqual(lib["entries"][1]["tags"], ["x"])


class HelperTests(unittest.TestCase):
    def test_snap_frames_matches_the_router(self):
        self.assertEqual(snap_frames(3.0, 16), 49)
        self.assertEqual(snap_frames(0.5, 8), 5)
        self.assertEqual(snap_frames(10.0, 24), 161)
        self.assertEqual((snap_frames(2.0, 12) - 1) % 4, 0)

    def test_sanitize_graph_removes_paths_addresses_and_credentials(self):
        key = "sk-" + secrets.token_hex(16)
        graph = {"1": {"class_type": "X", "inputs": {
            "a": "/srv/models/video/loras/wan22/x.safetensors", "b": "http://192.168.100.11:18800/v1",
            "c": f"Bearer {key}", "d": "gx10-02 said hi", "e": "wan22/paired/x.safetensors",
            "f": ["1000", 0], "g": 0.8}}}
        out = json.dumps(sanitize_graph(graph))
        for bad in ("/srv/", "192.168.", key, "gx10-02"):
            self.assertNotIn(bad, out)
        clean = sanitize_graph(graph)["1"]["inputs"]
        self.assertEqual(clean["a"], "x.safetensors")
        self.assertEqual((clean["e"], clean["f"], clean["g"]), ("wan22/paired/x.safetensors", ["1000", 0], 0.8))

    def test_human_errors_are_never_generic(self):
        self.assertIn("memory", human_error("out_of_memory", "raw"))
        self.assertEqual(human_error(None, "  specific reason "), "specific reason")
        self.assertNotEqual(human_error(None, None), "Generation failed")

    def test_chain_summary(self):
        graph = {"3": {"class_type": "UNETLoader", "inputs": {"unet_name": "hi.safetensors"}},
                 "5": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["3", 0], "lora_name": "base",
                                                                        "strength_model": 1.0}},
                 "1000": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["5", 0], "lora_name": "u",
                                                                           "strength_model": 0.5}},
                 "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1000", 0]}},
                 "12": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["7", 0]}}}
        s = summarize_chains(graph)
        self.assertEqual([(c["lora_name"], c["base"]) for c in s["high"]], [("base", True), ("u", False)])
        self.assertEqual((s["high_model"], s["low"], s["low_model"]), ("hi.safetensors", [], None))


class WanApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_key = secrets.token_hex(24)
        cls.router = WanRouterStub(cls.media_key)

    @classmethod
    def tearDownClass(cls):
        cls.router.close()

    def setUp(self):
        self.router.comfy.delay = 0.2
        self.env = TempEnv(media_base=self.router.url)
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.start()
        self.cookie = None
        self.csrf = None
        self.login()

    def start(self):
        self.app, servers = srv.build(self.env.cfg)
        self.app.media.router._key = lambda: self.media_key
        self.app.media.poll_interval = 0.1
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def restart(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.start()
        self.login()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.cleanup()

    def req(self, method, path, body=None, headers=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, dict(resp.getheaders()), (json.loads(data) if data and "json" in ctype else data)

    def login(self):
        status, hdrs, body = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD},
                                      cookie=False)
        self.assertEqual(status, 200)
        self.cookie = hdrs["Set-Cookie"].split(";")[0]
        self.csrf = body["csrf"]

    def get(self, path):
        status, _, body = self.req("GET", path)
        return status, body

    def post(self, path, body=None, csrf=True):
        hdrs = {"Origin": f"http://127.0.0.1:{self.port}"}
        if csrf:
            hdrs["X-CSRF-Token"] = self.csrf
        status, _, out = self.req("POST", path, body if body is not None else {}, hdrs)
        return status, out

    def entries(self, refresh=False):
        status, lib = self.get("/api/video/loras" + ("?refresh=1" if refresh else ""))
        self.assertEqual(status, 200, lib)
        return lib

    def entry(self, name_part, kind=None):
        for e in self.entries()["entries"]:
            names = " ".join(n for n in (e["high_file"], e["low_file"], e["file"]) if n)
            if name_part in names and (kind is None or e["kind"] == kind):
                return e
        self.fail(f"no entry with {name_part}")

    def wait_job(self, job_id, want=("ready", "failed", "cancelled"), timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, job = self.get(f"/api/media/jobs/{job_id}")
            if job["phase"] in want:
                time.sleep(0.2)  # observers run right after the phase flips
                return job
            time.sleep(0.1)
        self.fail(f"job {job_id} stuck in {job['phase']}")


class LibraryApiTests(WanApiBase):
    def test_requires_session_and_csrf(self):
        self.cookie = None
        self.assertEqual(self.get("/api/video/loras")[0], 401)
        self.login()
        for path in ("/api/video/loras/rescan", "/api/video/presets", "/api/video/generate", "/api/video/pairs"):
            status, body = self.post(path, {}, csrf=False)
            self.assertEqual((status, body["error"]["code"]), (403, "csrf"), path)

    def test_migration_is_recorded(self):
        names = [m["name"] for m in self.app.library.migrations()]
        self.assertIn("020_wan_loras.sql", names)

    def test_discovery_classification_and_pairing(self):
        lib = self.entries()
        kinds = {(e["kind"], e["pair_state"], e["display_name"]) for e in lib["entries"]}
        self.assertIn(("pair", "paired", "CinematicGlow"), kinds)
        self.assertIn(("pair", "paired", "DetailBoost"), kinds)
        self.assertIn(("high", "high_only", "SoloMotion"), kinds)
        self.assertIn(("general", "general", "FilmGrain"), kinds)
        glow = self.entry("CinematicGlow")
        self.assertEqual(glow["high_file"], "wan22/paired/CinematicGlow_high_noise.safetensors")
        self.assertEqual(glow["files"][0]["path"],
                         "/srv/models/video/loras/wan22/paired/CinematicGlow_high_noise.safetensors")
        self.assertIsNotNone(glow["discovered_at"])
        self.assertEqual(self.entry("qwen_portrait")["compatibility"], "incompatible")
        self.assertEqual(self.entry("wan5b_style")["compatibility"], "incompatible")
        self.assertEqual(self.entry("mystery_style")["compatibility"], "unknown")
        corrupt = self.entry("corrupt_style")
        self.assertEqual((corrupt["compatibility"], corrupt["usable"]), ("incompatible", False))
        self.assertTrue(corrupt["problems"])
        self.assertEqual(lib["defaults"]["strength_high"], 0.8)
        self.assertEqual({f["name"] for f in lib["unpaired_files"]} >= {"orphan_high_noise.safetensors",
                                                                        "stray_low_noise.safetensors"}, True)

    def test_manual_pair_persists_and_unpair(self):
        status, e = self.post("/api/video/pairs", {"high_file": "orphan_high_noise.safetensors",
                                                   "low_file": "stray_low_noise.safetensors"})
        self.assertEqual(status, 200, e)
        self.assertEqual((e["kind"], e["pair_source"]), ("pair", "manual"))
        status, dup = self.post("/api/video/pairs", {"high_file": "orphan_high_noise.safetensors",
                                                     "low_file": "stray_low_noise.safetensors"})
        self.assertEqual((status, dup["error"]["code"]), (409, "pair_conflict"))
        self.restart()
        self.assertEqual(self.entry("orphan_high_noise", "pair")["pair_source"], "manual")
        status, _ = self.post("/api/video/pairs/remove", {"entry_id": e["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.entry("orphan_high_noise")["kind"], "high")

    def test_pair_refusals(self):
        cases = [({"high_file": "stray_low_noise.safetensors", "low_file": "orphan_high_noise.safetensors"},
                  "lora_branch_mismatch"),
                 ({"high_file": "orphan_high_noise.safetensors", "low_file": "../../etc/passwd"}, "lora_not_found"),
                 ({"high_file": "wan22/general/FilmGrain.safetensors", "low_file": "stray_low_noise.safetensors"},
                  "lora_branch_mismatch"),
                 ({"high_file": "x", "low_file": "x"}, "invalid_request")]
        for body, code in cases:
            with self.subTest(code=code):
                status, out = self.post("/api/video/pairs", body)
                self.assertEqual(out["error"]["code"], code)
                self.assertIn(status, (400, 404))

    def test_unpair_an_auto_pair_and_restore_it(self):
        pair = self.entry("DetailBoost", "pair")
        self.assertEqual(pair["pair_source"], "auto")
        self.assertEqual(self.post("/api/video/pairs/remove", {"entry_id": pair["id"]})[0], 200)
        self.restart()
        self.assertEqual(self.entry("DetailBoost-HN")["kind"], "high")
        status, _ = self.post("/api/video/pairs/restore", {"name": "DetailBoost-HN.safetensors"})
        self.assertEqual(status, 200)
        self.post("/api/video/pairs/restore", {"name": "DetailBoost-LN.safetensors"})
        self.assertEqual(self.entry("DetailBoost", "pair")["pair_source"], "auto")
        status, out = self.post("/api/video/pairs/remove", {"entry_id": self.entry("SoloMotion")["id"]})
        self.assertEqual((status, out["error"]["code"]), (409, "not_paired"))

    def test_entry_settings_and_order(self):
        glow = self.entry("CinematicGlow")
        status, e = self.post(f"/api/video/loras/{glow['id']}", {
            "display_name": "Glow", "tags": ["light", "light", "film"], "default_high": 0.65, "default_low": 0.55,
            "description": "soft glow"})
        self.assertEqual(status, 200, e)
        self.assertEqual((e["display_name"], e["tags"], e["default_high"], e["default_low"]),
                         ("Glow", ["light", "film"], 0.65, 0.55))
        for bad in ({"default_high": 1.6}, {"default_low": -0.1}, {"enabled": "yes"}, {"tags": "x"}, {},
                    {"display_name": "x" * 121}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post(f"/api/video/loras/{glow['id']}", bad)[0], 400)
        self.assertEqual(self.get("/api/video/loras/l_0000000000000000")[0], 404)
        self.assertEqual(self.get("/api/video/loras/..%2F..%2Fetc")[0], 404)
        solo = self.entry("SoloMotion")
        status, lib = self.post("/api/video/loras/order", {"ids": [solo["id"], glow["id"]]})
        self.assertEqual(status, 200)
        self.assertEqual([e["id"] for e in lib["entries"][:2]], [solo["id"], glow["id"]])
        self.assertEqual(self.post("/api/video/loras/order", {"ids": ["../x"]})[0], 400)

    def test_rescan_discovers_new_files_and_marks_missing(self):
        self.router.add_file("wan22/paired/NewLook_high_noise.safetensors")
        self.router.add_file("wan22/paired/NewLook_low_noise.safetensors")
        try:
            status, lib = self.post("/api/video/loras/rescan")
            self.assertEqual(status, 200)
            self.assertIn("NewLook", [e["display_name"] for e in lib["entries"]])
        finally:
            self.router.remove_file("wan22/paired/NewLook_high_noise.safetensors")
            self.router.remove_file("wan22/paired/NewLook_low_noise.safetensors")
        status, lib = self.post("/api/video/loras/rescan")
        self.assertEqual([m["name"] for m in lib["missing_files"]],
                         ["wan22/paired/NewLook_high_noise.safetensors", "wan22/paired/NewLook_low_noise.safetensors"])
        self.assertTrue(any("rescan" in e[1] for e in [(r["action"], r["action"]) for r in self.audit()]))

    def audit(self):
        path = self.env.cfg.log_dir / "audit.log"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def test_router_unavailable_is_explained(self):
        self.app.media.router.base = "http://127.0.0.1:9"
        self.app.wan._catalogue = None
        status, body = self.get("/api/video/loras")
        self.assertEqual((status, body["error"]["code"]), (502, "router_unavailable"))
        self.assertIn("not reachable", body["error"]["hint"])


class PresetApiTests(WanApiBase):
    def test_builtin_presets_are_honest(self):
        status, body = self.get("/api/video/presets")
        self.assertEqual(status, 200)
        builtin = {p["id"]: p for p in body["presets"] if p["builtin"]}
        self.assertEqual({k: v["name"] for k, v in builtin.items()}, BUILTIN)
        for p in builtin.values():
            self.assertEqual(p["data"]["loras"], [])

    def test_crud_rename_duplicate_delete(self):
        glow = self.entry("CinematicGlow")
        data = {"loras": [{"entry_id": glow["id"], "strength_high": 0.7, "strength_low": 0.6, "enabled": True,
                           "high_file": glow["high_file"], "low_file": glow["low_file"]}],
                "size": "832x480", "seconds": 2.5, "fps": 16, "seed_mode": "fixed", "seed": 7,
                "prompt_suffix": "golden hour", "negative_prompt": "blur", "negative_mode": "append",
                "advanced": {"shift": 6.0, "steps": 6, "sampler_name": "euler", "scheduler": "simple", "cfg": 1.0}}
        status, p = self.post("/api/video/presets", {"name": "Glow look", "description": "d", "data": data})
        self.assertEqual(status, 200, p)
        self.assertRegex(p["id"], r"^wp_[0-9a-f]{16}$")
        self.assertEqual((p["data"]["frames"], p["data"]["aspect_ratio"], p["data"]["advanced"]["boundary"]),
                         (41, "16:9 (approx.)", 3))
        self.assertEqual(p["data"]["loras"][0]["strength_low"], 0.6)
        self.assertEqual(self.post("/api/video/presets", {"name": "glow LOOK", "data": data})[1]["error"]["code"],
                         "preset_exists")
        status, renamed = self.post(f"/api/video/presets/{p['id']}", {"name": "Glow look 2"})
        self.assertEqual((status, renamed["name"]), (200, "Glow look 2"))
        status, copy = self.post(f"/api/video/presets/{p['id']}/duplicate", {})
        self.assertEqual((status, copy["name"]), (200, "Glow look 2 copy"))
        self.assertEqual(copy["data"]["loras"], renamed["data"]["loras"])
        self.restart()
        self.assertEqual(self.get(f"/api/video/presets/{p['id']}")[1]["name"], "Glow look 2")
        self.assertEqual(self.post(f"/api/video/presets/{copy['id']}/delete", {})[0], 400)
        self.assertEqual(self.post(f"/api/video/presets/{copy['id']}/delete", {"confirm": True})[0], 200)
        self.assertEqual(self.get(f"/api/video/presets/{copy['id']}")[0], 404)

    def test_invalid_preset_data(self):
        for data in ({"size": "999x999"}, {"seconds": 99}, {"fps": 7.5}, {"seed_mode": "fixed"},
                     {"advanced": {"steps": 4, "boundary": 4}}, {"advanced": {"node": 1}},
                     {"loras": [{"entry_id": "../../x"}]}, {"loras": "x"}, "nope",
                     {"loras": [{"entry_id": "l_" + "0" * 16, "strength_high": 2}]}):
            with self.subTest(data=data):
                status, body = self.post("/api/video/presets", {"name": f"bad {secrets.token_hex(3)}", "data": data})
                self.assertEqual(status, 400, body)

    def test_resolve_for_creative_flows(self):
        status, out = self.post("/api/video/presets/wp_5c1e7a2b90d34f01/resolve",
                                {"overrides": {"prompt": "a quiet harbour", "seed": 11, "flow_id": "flow_1"}})
        self.assertEqual(status, 200, out)
        body = out["body"]
        self.assertTrue(out["valid"], out)
        self.assertEqual(body["prompt"],
                         "a quiet harbour, cinematic lighting, shallow depth of field, natural film grain, "
                         "realistic skin texture")
        self.assertEqual((body["size"], body["seed"], body["preset_id"], body["flow_id"]),
                         ("832x480", 11, "wp_5c1e7a2b90d34f01", "flow_1"))
        status, job = self.post("/api/video/generate", body)
        self.assertEqual(status, 202, job)
        self.assertEqual(self.wait_job(job["id"])["phase"], "ready")
        gen = self.get(f"/api/video/generations/{job['id']}")[1]
        self.assertEqual((gen["preset_id"], gen["flow_id"]), ("wp_5c1e7a2b90d34f01", "flow_1"))
        status, out = self.post("/api/video/presets/wp_2b8d6e1f47a54c03/resolve", {"overrides": {}})
        self.assertFalse(out["valid"])
        self.assertEqual(out["body"]["seed"], 424242)
        self.assertEqual(self.post("/api/video/presets/wp_5c1e7a2b90d34f01/resolve",
                                   {"overrides": {"graph": {}}})[0], 400)


class GenerationApiTests(WanApiBase):
    def preview(self, loras, **extra):
        return self.post("/api/video/workflow", {"prompt": "a lobby", "seed": 5, "loras": loras, **extra})

    def test_zero_loras_is_the_known_good_graph(self):
        status, out = self.preview([])
        self.assertEqual(status, 200, out)
        g = out["graph"]
        self.assertEqual((g["7"]["inputs"]["model"], g["8"]["inputs"]["model"]), (["5", 0], ["6", 0]))
        self.assertFalse(any(k.isdigit() and int(k) >= 1000 for k in g))
        self.assertEqual([c["lora_name"] for c in out["chains"]["high"]], ["Wan2.2_LightX2V_high_n54vv.safetensors"])
        self.assertRegex(out["workflow_version"], r"^gx-wan-lora/1\+wan22-t2v-a14b-uncensored@")

    def test_pairs_defaults_order_and_independent_strengths(self):
        glow, detail = self.entry("CinematicGlow"), self.entry("DetailBoost", "pair")
        status, out = self.preview([
            {"entry_id": detail["id"], "strength_high": 0.3, "strength_low": 1.2},
            {"entry_id": glow["id"]},
        ])
        self.assertEqual(status, 200, out)
        high = [(c["lora_name"], c["strength"]) for c in out["chains"]["high"] if not c["base"]]
        low = [(c["lora_name"], c["strength"]) for c in out["chains"]["low"] if not c["base"]]
        self.assertEqual(high, [("DetailBoost-HN.safetensors", 0.3),
                                ("wan22/paired/CinematicGlow_high_noise.safetensors", DEFAULTS["strength_high"])])
        self.assertEqual(low, [("DetailBoost-LN.safetensors", 1.2),
                               ("wan22/paired/CinematicGlow_low_noise.safetensors", DEFAULTS["strength_low"])])
        # an entry default replaces the configured default, never a chosen value
        self.post(f"/api/video/loras/{glow['id']}", {"default_high": 0.4})
        status, out = self.preview([{"entry_id": glow["id"], "strength_low": 0.9}])
        self.assertEqual([c["strength"] for c in out["chains"]["high"]][-1], 0.4)
        self.assertEqual([c["strength"] for c in out["chains"]["low"]][-1], 0.9)

    def test_high_only_low_only_general_and_disabled(self):
        solo, grain = self.entry("SoloMotion"), self.entry("FilmGrain")
        status, out = self.preview([{"entry_id": solo["id"], "strength_high": 0.5}])
        self.assertEqual(status, 200, out)
        self.assertEqual((len(out["chains"]["high"]), len(out["chains"]["low"])), (2, 1))
        status, out = self.preview([{"entry_id": grain["id"]}])
        self.assertEqual((status, out["error"]["code"]), (400, "lora_apply_required"))
        status, out = self.preview([{"entry_id": grain["id"], "apply": "low", "strength_low": 0.2}])
        self.assertEqual((len(out["chains"]["high"]), out["chains"]["low"][-1]["strength"]), (1, 0.2))
        status, out = self.preview([{"entry_id": grain["id"], "apply": "both", "strength_high": 0.3,
                                     "strength_low": 0.4}])
        self.assertEqual(status, 200, out)
        self.assertEqual((out["chains"]["high"][-1]["lora_name"], out["chains"]["low"][-1]["lora_name"]),
                         ("wan22/general/FilmGrain.safetensors",) * 2)
        status, out = self.preview([{"entry_id": solo["id"], "apply": "both"}])
        self.assertEqual((status, out["error"]["code"]), (400, "lora_branch_mismatch"))
        status, out = self.preview([{"entry_id": solo["id"], "enabled": False}])
        self.assertEqual((status, len(out["chains"]["high"])), (200, 1))
        self.assertFalse(out["loras"][0]["enabled"])

    def test_compatibility_gates(self):
        cases = [("mystery_style", "lora_unknown_compatibility", {"apply": "high"}),
                 ("qwen_portrait", "lora_incompatible", {"apply": "high"}),
                 ("wan5b_style", "lora_incompatible", {}),
                 ("corrupt_style", "lora_invalid_file", {"apply": "high"})]
        for part, code, extra in cases:
            with self.subTest(code=code):
                status, out = self.preview([{"entry_id": self.entry(part)["id"], **extra}])
                self.assertEqual((status, out["error"]["code"]), (400, code))
        mystery = self.entry("mystery_style")
        self.post(f"/api/video/loras/{mystery['id']}", {"allow_unknown": True})
        status, out = self.preview([{"entry_id": mystery["id"], "apply": "high"}])
        self.assertEqual(status, 200, out)
        self.post(f"/api/video/loras/{mystery['id']}", {"enabled": False})
        status, out = self.preview([{"entry_id": mystery["id"], "apply": "high"}])
        self.assertEqual(out["error"]["code"], "lora_disabled")

    def test_request_validation_and_path_traversal(self):
        glow = self.entry("CinematicGlow")
        cases = [({"prompt": ""}, "prompt_required"),
                 ({"prompt": "x", "loras": [{"entry_id": "../../etc/passwd"}]}, "lora_not_found"),
                 ({"prompt": "x", "loras": [{"entry_id": "l_" + "f" * 16}]}, "lora_not_found"),
                 ({"prompt": "x", "loras": [{"entry_id": glow["id"], "strength_high": 1.51}]}, "invalid_request"),
                 ({"prompt": "x", "loras": [{"entry_id": glow["id"], "name": "/etc/passwd"}]}, "invalid_request"),
                 ({"prompt": "x", "loras": [{"entry_id": glow["id"]}] * 9}, "lora_duplicate"),
                 ({"prompt": "x", "loras": [{"entry_id": glow["id"]}] * 17}, "lora_too_many"),
                 ({"prompt": "x", "size": "4096x4096"}, "invalid_request"),
                 ({"prompt": "x", "advanced": {"sampler_name": "evil"}}, "invalid_request"),
                 ({"prompt": "x", "flow_id": "../x"}, "invalid_request")]
        for body, code in cases:
            with self.subTest(code=code, body=str(body)[:60]):
                status, out = self.post("/api/video/workflow", body)
                self.assertEqual((status, out["error"]["code"]), (400, code), out)
        # file names in a request are ignored: the catalogue decides
        status, out = self.preview([{"entry_id": glow["id"], "high_file": "/etc/passwd.safetensors",
                                     "low_file": "../x.safetensors"}])
        self.assertEqual(status, 200)
        self.assertNotIn("passwd", json.dumps(out))

    def test_generate_records_history_workflow_and_asset(self):
        glow = self.entry("CinematicGlow")
        status, job = self.post("/api/video/generate", {
            "prompt": "two adults walking through a hotel lobby", "negative_prompt": "blur", "seed": 1234,
            "size": "832x480", "seconds": 2, "fps": 16, "title": "Lobby",
            "loras": [{"entry_id": glow["id"], "strength_high": 0.7, "strength_low": 0.5}],
            "advanced": {"shift": 5.5}})
        self.assertEqual(status, 202, job)
        self.assertEqual(job["params"]["wan"]["router"]["loras"]["high"][0]["strength"], 0.7)
        status, early = self.get(f"/api/video/generations/{job['id']}")
        self.assertEqual(status, 200)
        done = self.wait_job(job["id"])
        self.assertEqual(done["phase"], "ready", done)
        status, gen = self.get(f"/api/video/generations/{job['id']}")
        self.assertEqual(gen["status"], "ready")
        self.assertEqual((gen["prompt"], gen["negative_prompt"], gen["seed"], gen["size"], gen["frames"], gen["fps"]),
                         ("two adults walking through a hotel lobby", "blur", 1234, "832x480", 33, 16.0))
        self.assertEqual((gen["high_model"], gen["low_model"]),
                         ("wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
                          "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors"))
        self.assertEqual([(r["display_name"], r["strength_high"], r["strength_low"]) for r in gen["loras"]],
                         [("CinematicGlow", 0.7, 0.5)])
        self.assertEqual(gen["chains"]["high"][-1]["lora_name"], glow["high_file"])
        self.assertEqual(gen["settings"]["shift"], 5.5)
        self.assertRegex(gen["workflow_version"], r"^gx-wan-lora/1\+")
        self.assertTrue(gen["comfy_prompt_id"].startswith("e2e-prompt-"))
        self.assertEqual(gen["workflow"]["1000"]["inputs"]["strength_model"], 0.7)
        self.assertRegex(gen["asset_id"], r"^a_[0-9a-f]{24}$")
        self.assertEqual(gen["output_path"], f"videos/{gen['asset_id']}.mp4")
        self.assertEqual(gen["flows_url"], f"#/flows?asset={gen['asset_id']}")
        self.assertIsNotNone(gen["duration_seconds"])
        self.assertEqual(gen["request"]["loras"][0]["entry_id"], glow["id"])
        asset = gen["asset"]
        self.assertEqual(asset["settings"]["wan"]["generation_id"], job["id"])
        self.assertEqual(asset["settings"]["wan"]["loras"][0]["strength_low"], 0.5)
        status, listing = self.get("/api/video/generations?q=lobby")
        self.assertEqual([i["id"] for i in listing["items"]], [job["id"]])
        self.assertNotIn("workflow", listing["items"][0])
        status, hdrs, data = self.req("GET", f"/api/video/generations/{job['id']}/workflow")
        self.assertEqual(status, 200)
        self.assertIn("attachment", hdrs["Content-Disposition"])
        text = data.decode() if isinstance(data, bytes) else json.dumps(data)
        for bad in ("/srv/", "192.168.", self.media_key, "gx10-0", "Bearer"):
            self.assertNotIn(bad, text)
        doc = json.loads(text)
        self.assertEqual(doc["prompt"]["1000"]["inputs"]["lora_name"], glow["high_file"])
        _, _, again = self.req("GET", f"/api/video/generations/{job['id']}/workflow")
        self.assertEqual(again, data)

    def test_failures_are_explained(self):
        status, job = self.post("/api/video/generate", {"prompt": "e2e-oom scene", "seed": 1})
        self.assertEqual(self.wait_job(job["id"])["phase"], "failed")
        gen = self.get(f"/api/video/generations/{job['id']}")[1]
        self.assertEqual(gen["error_code"], "out_of_memory")
        self.assertIn("ran out of memory", gen["error_message"])
        self.assertIn("OutOfMemoryError", gen["error_detail"])
        self.assertTrue(gen["has_workflow"])
        status, job2 = self.post("/api/video/generate", {"prompt": "e2e-reject scene", "seed": 2})
        self.wait_job(job2["id"])
        gen2 = self.get(f"/api/video/generations/{job2['id']}")[1]
        self.assertEqual(gen2["error_code"], "lora_not_visible")
        errors = self.get("/api/video/errors")[1]["items"]
        self.assertEqual({e["id"] for e in errors} >= {job["id"], job2["id"]}, True)
        self.assertEqual(self.get("/api/video/generations?status=failed")[1]["total"], 2)

    def test_cancel_a_queued_generation(self):
        self.router.comfy.delay = 1.5
        _, first = self.post("/api/video/generate", {"prompt": "first", "seed": 1})
        _, second = self.post("/api/video/generate", {"prompt": "second", "seed": 2})
        status, out = self.post(f"/api/video/jobs/{second['id']}/cancel")
        self.assertEqual((status, out["phase"]), (200, "cancelled"))
        self.assertEqual(self.wait_job(first["id"])["phase"], "ready")
        gen = self.get(f"/api/video/generations/{second['id']}")[1]
        self.assertEqual((gen["status"], gen["error_code"]), ("cancelled", "cancelled"))
        status, out = self.post(f"/api/video/jobs/{first['id']}/cancel")
        self.assertEqual(status, 409)
        self.assertEqual(self.get("/api/video/generations/zzzz")[0], 404)

    def test_cancel_while_the_router_waits_for_memory(self):
        service = self.router.service
        original = service.cfg
        meminfo = self.env.root / "meminfo"
        meminfo.write_text("MemTotal: 127000000 kB\nMemAvailable: 20000000 kB\n")
        service.cfg = dataclasses.replace(original, meminfo_path=str(meminfo), resource_retry_seconds=1.0)
        try:
            _, job = self.post("/api/video/generate", {"prompt": "waits for memory", "seed": 3})
            deadline = time.time() + 15
            while time.time() < deadline:
                live = self.get(f"/api/media/jobs/{job['id']}")[1]
                if live["phase"] == "waiting" and live.get("waiting"):
                    break
                time.sleep(0.1)
            self.assertEqual(live["phase"], "waiting", live)
            self.assertIn("memory", live["waiting"]["reason"])
            status, out = self.post(f"/api/video/jobs/{job['id']}/cancel")
            self.assertEqual(status, 200, out)
            self.assertEqual(self.wait_job(job["id"])["phase"], "cancelled")
            gen = self.get(f"/api/video/generations/{job['id']}")[1]
            self.assertEqual((gen["status"], gen["error_code"]), ("cancelled", "cancelled"))
            prompts = [g["9"]["inputs"]["text"] for g in self.router.comfy.submitted]
            self.assertNotIn("waits for memory", prompts)
        finally:
            service.cfg = original

    def test_plain_media_jobs_ignore_wan_fields_from_the_browser(self):
        status, job = self.post("/api/media/jobs", {"kind": "t2v", "prompt": "plain", "wan": {"router": {
            "loras": {"high": [{"name": "../../etc/passwd", "strength": 1}]}}}})
        self.assertEqual(status, 202)
        self.assertNotIn("wan", job["params"])
        self.assertEqual(self.wait_job(job["id"])["phase"], "ready")
        self.assertEqual(self.get(f"/api/video/generations/{job['id']}")[0], 404)


class MigrationSqlTests(unittest.TestCase):
    def test_presets_json_is_valid_and_ids_stable(self):
        env = TempEnv()
        try:
            from gx_control_ui.media_library import MediaLibrary, MediaTools
            lib = MediaLibrary(env.cfg.media_dir, MediaTools(enabled=False))
            with lib.connect() as con:
                rows = con.execute("SELECT id, name, data FROM wan_presets").fetchall()
                self.assertEqual({r["id"]: r["name"] for r in rows}, BUILTIN)
                for r in rows:
                    self.assertEqual(json.loads(r["data"])["loras"], [])
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute("INSERT INTO wan_lora_pairs VALUES ('p', 'a', 'a', 0, 'x')")
            self.assertTrue(all(re.match(r"^wp_[0-9a-f]{16}$", k) for k in BUILTIN))
        finally:
            env.cleanup()


if __name__ == "__main__":
    unittest.main()
