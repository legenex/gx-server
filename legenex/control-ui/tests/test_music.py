"""Hermetic tests for gx-music on gx10-01 (music.py) against the node-2
supervisor stub (e2e/music_stub.py) on 127.0.0.1."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
import unittest
from unittest import mock

from support import UI_DIR, TempEnv

from gx_control_ui.media_library import LibraryError, MediaLibrary, MediaTools, NewAsset
from gx_control_ui.music import (MAX_BODY_KEYS, MusicClient, MusicError, MusicJobs, clean_request)

sys.path.insert(0, str(UI_DIR / "e2e"))
import music_stub  # noqa: E402
from music_stub import MusicStub, sine_wav  # noqa: E402

#: The stub renders a 2 s sine chord in pure Python per instance (~0.5 s);
#: render a short one once and reuse it.
_WAV = sine_wav(0.25)


def make_stub(key: str) -> MusicStub:
    with mock.patch.object(music_stub, "sine_wav", lambda *a, **kw: _WAV):
        return MusicStub(key, polls_to_finish=3)


def job_id() -> str:
    return "mus-" + secrets.token_hex(16)


class CleanRequestTests(unittest.TestCase):
    def test_accepts_known_fields(self):
        body = {"prompt": "lo-fi", "style_tags": ["chill"], "duration": 30, "source_asset_id": "a_" + "1" * 24}
        out = clean_request("generate", body)
        self.assertEqual(out, body)
        self.assertIsNot(out, body)

    def test_rejections(self):
        with self.assertRaises(MusicError) as cm:
            clean_request("extract", {})
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "not_found"))
        for bad in ([], "x", None):
            with self.assertRaises(MusicError) as cm:
                clean_request("generate", bad)
            self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(MusicError) as cm:
            clean_request("generate", {"prompt": "x", "engine_url": "http://evil", "model_path": "/srv"})
        self.assertIn("engine_url", str(cm.exception))
        too_many = {f"k{i}": 1 for i in range(MAX_BODY_KEYS + 1)}
        with self.assertRaises(MusicError) as cm:
            clean_request("generate", too_many)
        self.assertIn("too many", str(cm.exception))


class StubBase(unittest.TestCase):
    # One stub per class: shutting a stdlib server down waits for its 0.5 s poll.
    @classmethod
    def setUpClass(cls):
        cls.key = secrets.token_hex(24)
        cls.stub = make_stub(cls.key)

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()
        cls.stub.server.server_close()

    def setUp(self):
        self.env = TempEnv()
        with self.stub._lock:
            self.stub.jobs.clear()
            self.stub.uploads.clear()
            self.stub.calls.clear()
            self.stub.engine = "unloaded"
        self.key_file = self.env.root / "secrets" / "music-key"
        self.key_file.write_text(self.key + "\n")
        self.client = MusicClient(self.stub.url + "/", self.key_file)
        self.library = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))
        self.audits: list[dict] = []
        self.results = Results()
        self.explained: list[str] = []
        self.jobs = MusicJobs(self.client, self.library, self.env.cfg.state_dir / "music-jobs.json",
                              audit=lambda **kw: self.audits.append(kw), results=self.results,
                              explain=self.explain, start_worker=False)

    def explain(self, alias):
        self.explained.append(alias)
        return {"code": "insufficient_memory", "reason": "Waiting for gx-video to unload"}

    def tearDown(self):
        self.env.cleanup()

    def finish(self, jid):
        for _ in range(10):
            job = self.jobs.get(jid)
            if job["status"] == "completed":
                return job
        self.fail("stub job never completed")

    def audio_asset(self, **settings):
        return self.library.add(NewAsset(type="audio", ext="wav", operation="generate", data=sine_wav(0.1),
                                         title="My Track", settings=settings))


class Results:
    def __init__(self):
        self.records: list[tuple] = []

    def record(self, alias, kind, ok, detail="", **kw):
        self.records.append((alias, kind, ok, detail))


class ClientTests(StubBase):
    def test_missing_and_short_key(self):
        self.key_file.unlink()
        with self.assertRaises(MusicError) as cm:
            self.client.call("GET", "/v1/music/model")
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "not_configured"))
        self.key_file.write_text("short")
        with self.assertRaises(MusicError) as cm:
            self.client.call("GET", "/v1/music/model")
        self.assertEqual(cm.exception.code, "not_configured")
        self.assertNotIn("short", str(cm.exception))

    def test_wrong_key_is_an_upstream_error(self):
        self.key_file.write_text(secrets.token_hex(24))
        with self.assertRaises(MusicError) as cm:
            self.client.call("GET", "/v1/music/model")
        self.assertEqual((cm.exception.status, cm.exception.code), (502, "unauthorized"))

    def test_upstream_errors_keep_safe_statuses(self):
        with self.assertRaises(MusicError) as cm:
            self.client.call("GET", f"/v1/music/{job_id()}")
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "not_found"))
        with self.assertRaises(MusicError) as cm:
            self.client.call("POST", "/v1/music/remix", body={"prompt": "x"})
        self.assertEqual(cm.exception.status, 400)

    def test_unreachable(self):
        client = MusicClient("http://127.0.0.1:9", self.key_file)
        with self.assertRaises(MusicError) as cm:
            client.call("GET", "/v1/music/model")
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "node_unavailable"))
        self.assertNotIn("127.0.0.1", str(cm.exception))
        self.assertEqual(client.health(), {"ok": False})

    def test_key_is_sent_as_bearer_and_health_is_public(self):
        self.assertEqual(self.client.call("GET", "/v1/music/model")["alias"], "gx-music")
        health = self.client.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "gx-music")

    def test_download(self):
        jid = self.jobs.submit("generate", {"prompt": "x"}, user="admin")["id"]
        dest = self.library.tmp_file(".wav")
        size = self.client.download(f"/v1/music/{jid}/content?index=0&format=wav", dest)
        self.assertEqual(size, len(self.stub.wav))
        bad = self.library.tmp_file(".wav")
        with self.assertRaises(MusicError) as cm:
            self.client.download(f"/v1/music/{job_id()}/content", bad)
        self.assertEqual(cm.exception.code, "download_failed")
        self.assertFalse(bad.exists())


class SubmitAndImportTests(StubBase):
    def test_generate_lifecycle_and_import(self):
        job = self.jobs.submit("generate", {"prompt": "warm lo-fi", "style_tags": ["lo-fi", "chill"],
                                            "lyrics": "[Verse] hello", "batch_size": 2}, user="admin", ip="1.2.3.4")
        jid = job["id"]
        self.assertRegex(jid, r"^mus-[0-9a-f]{32}$")
        self.assertNotIn("links", job)
        self.assertEqual((job["imported"], job["submitted_via"], job["library_assets"]), (False, "ui", []))
        self.assertTrue(self.jobs.known(jid))
        state = json.loads((self.env.cfg.state_dir / "music-jobs.json").read_text())
        self.assertEqual(state[jid]["user"], "admin")
        self.assertEqual(self.audits[-1]["action"], "music.generate")
        self.assertEqual(self.jobs.pending(), [jid])

        done = self.finish(jid)
        self.assertEqual(done["phase"], "saving")
        for track in done["tracks"]:
            self.assertNotIn("url", track["files"]["wav"])

        self.jobs.sweep()
        self.assertEqual(self.jobs.pending(), [])
        assets = self.library.find_by_job(jid)
        self.assertEqual(len(assets), 2)
        first = assets[0]
        self.assertEqual((first["type"], first["ext"], first["operation"]), ("audio", "wav", "generate"))
        self.assertEqual(first["model_alias"], "gx-music")
        self.assertEqual(first["model_repo"], "ACE-Step/acestep-v15-xl-turbo")
        self.assertEqual(first["tags"], ["lo-fi", "chill"])
        self.assertEqual(first["lyrics"], "[Verse] hello")
        self.assertEqual(first["time_signature"], "4/4")
        self.assertEqual(first["bpm"], 96.0)
        self.assertEqual(first["settings"]["music_job_id"], jid)
        self.assertEqual({a["settings"]["track_index"] for a in assets}, {0, 1})
        self.assertEqual({a["title"] for a in assets}, {"warm lo-fi (1)", "warm lo-fi (2)"})
        self.assertEqual(first["variants"]["wav"]["sha256"], hashlib.sha256(self.stub.wav).hexdigest())
        self.assertTrue(self.library.file_path(first).is_file())
        self.assertEqual(self.results.records[-1][:3], ("gx-music", "inference", True))

        view = self.jobs.get(jid)
        self.assertEqual((view["imported"], view["phase"]), (True, "completed"))
        self.assertEqual(sorted(view["library_assets"]), sorted(a["id"] for a in assets))
        # idempotent
        again = self.jobs.import_job(self.stub.view(self.stub.jobs[jid]))
        self.assertEqual(sorted(again), sorted(a["id"] for a in assets))
        self.assertEqual(len(self.library.find_by_job(jid)), 2)
        self.assertEqual(list(self.library.root.joinpath("tmp").iterdir()), [])

    def test_api_remix_of_a_job_links_to_the_parent_track(self):
        parent = self.jobs.submit("generate", {"prompt": "source"}, user="key:app", via="api")["id"]
        self.finish(parent)
        child = self.jobs.submit("remix", {"source": {"job_id": parent, "index": 0}, "prompt": "cover"},
                                 user="key:app", via="api")["id"]
        self.finish(child)
        raw = self.stub.view(self.stub.jobs[child])
        self.assertEqual(raw["parent_job_id"], parent)
        # the parent is not saved yet: the child waits instead of losing its lineage
        self.assertEqual(self.jobs.import_job(raw), [])
        self.assertEqual(self.library.find_by_job(child), [])
        self.jobs.sweep()
        self.jobs.sweep()
        parent_asset = self.library.find_by_job(parent)[0]
        child_asset = self.library.find_by_job(child)[0]
        self.assertEqual(child_asset["parent_id"], parent_asset["id"])
        self.assertEqual(self.jobs.pending(), [])

    def test_state_survives_a_restart(self):
        jid = self.jobs.submit("generate", {"prompt": "x"}, user="admin")["id"]
        again = MusicJobs(self.client, self.library, self.env.cfg.state_dir / "music-jobs.json", start_worker=False)
        self.assertTrue(again.known(jid))
        (self.env.cfg.state_dir / "music-jobs.json").write_text("[1, 2]")
        self.assertFalse(MusicJobs(self.client, self.library, self.env.cfg.state_dir / "music-jobs.json",
                                   start_worker=False).known(jid))

    def test_import_keeps_format_variants(self):
        jid = self.jobs.submit("generate", {"prompt": "variants"}, user="admin")["id"]
        job = self.finish(jid)
        raw = self.stub.view(self.stub.jobs[jid])
        meta = raw["tracks"][0]["files"]["wav"]
        raw["tracks"][0]["files"] = {"wav": meta, "flac": dict(meta), "mp3": dict(meta)}
        self.assertEqual(job["status"], "completed")
        ids = self.jobs.import_job(raw)
        asset = self.library.get(ids[0])
        self.assertEqual(set(asset["variants"]), {"wav", "flac", "mp3"})
        self.assertEqual(asset["stream_url"], f"/api/media/assets/{ids[0]}/file?format=mp3")
        for fmt in ("wav", "flac", "mp3"):
            self.assertTrue(self.library.file_path(asset, fmt).is_file(), fmt)
        downloads = [c for c in self.stub.calls if c[1].endswith("/content")]
        self.assertEqual(len(downloads), 3)

    def test_checksum_mismatch_is_refused_and_cleaned_up(self):
        jid = self.jobs.submit("generate", {"prompt": "bad"}, user="admin")["id"]
        self.finish(jid)
        raw = self.stub.view(self.stub.jobs[jid])
        raw["tracks"][0]["files"]["wav"]["sha256"] = "0" * 64
        with self.assertRaises(MusicError) as cm:
            self.jobs.import_job(raw)
        self.assertEqual(cm.exception.code, "checksum")
        self.assertEqual(self.library.find_by_job(jid), [])
        self.assertIn("SHA-256", self.jobs.decorate(raw)["import_error"])
        meta = self.stub.view(self.stub.jobs[jid])["tracks"][0]["files"]["wav"]
        raw["tracks"][0]["files"] = {"wav": dict(meta, bytes=meta["bytes"] + 1)}
        with self.assertRaises(MusicError):
            self.jobs.import_job(raw)

    # Fixed (was a bug): music.MusicJobs.import_job() unlinks only the files already stored in
    # `paths`; the download that FAILS the SHA-256/size check is raised before
    # `paths[fmt] = dest`, so it stays in <library>/tmp (and nothing in the app
    # calls MediaLibrary.cleanup_tmp()).
    def test_failed_checksum_download_is_removed(self):
        jid = self.jobs.submit("generate", {"prompt": "bad"}, user="admin")["id"]
        self.finish(jid)
        raw = self.stub.view(self.stub.jobs[jid])
        raw["tracks"][0]["files"]["wav"]["sha256"] = "0" * 64
        with self.assertRaises(MusicError):
            self.jobs.import_job(raw)
        self.assertEqual(list(self.library.root.joinpath("tmp").iterdir()), [])

    def test_track_without_audio(self):
        jid = self.jobs.submit("generate", {"prompt": "x"}, user="admin")["id"]
        self.finish(jid)
        raw = self.stub.view(self.stub.jobs[jid])
        raw["tracks"][0]["files"] = {}
        with self.assertRaises(MusicError) as cm:
            self.jobs.import_job(raw)
        self.assertEqual(cm.exception.code, "no_audio")

    def test_sweep_finalises_failed_and_vanished_jobs(self):
        failed = self.jobs.submit("generate", {"prompt": "x"}, user="admin")["id"]
        self.stub.jobs[failed].update(status="failed", error={"message": "engine crashed"})
        ghost = job_id()
        with self.jobs._lock:
            self.jobs._jobs[ghost] = {"user": "admin", "submitted_at": time.time(), "imported": False}
        self.jobs.sweep()
        self.assertEqual(self.jobs.pending(), [])
        self.assertIn("no longer exists", self.jobs._jobs[ghost]["import_error"])
        self.assertEqual(self.results.records[-1][:4], ("gx-music", "inference", False, "engine crashed"))

    def test_sweep_propagates_outages(self):
        self.jobs.submit("generate", {"prompt": "x"}, user="admin")
        self.jobs.client = MusicClient("http://127.0.0.1:9", self.key_file)
        with self.assertRaises(MusicError):
            self.jobs.sweep()
        self.assertEqual(len(self.jobs.pending()), 1)

    def test_decorate_waiting_and_unknown_jobs(self):
        out = self.jobs.decorate({"id": job_id(), "status": "waiting_for_resource", "links": {"x": 1},
                                  "detail": "memory"})
        self.assertEqual(out["waiting"]["reason"], "Waiting for gx-video to unload")
        self.assertEqual(self.explained, ["gx-music"])
        self.assertNotIn("links", out)
        self.assertEqual((out["phase"], out["phase_detail"]), ("waiting_for_resource", "memory"))
        # completed but not ours: nothing to save
        self.assertEqual(self.jobs.decorate({"id": job_id(), "status": "completed"})["phase"], "completed")

    def test_invalid_ids(self):
        for bad in ("mus-123", "../x", "MUS-" + "a" * 32):
            for fn in (self.jobs.get, self.jobs.lineage):
                with self.assertRaises(MusicError) as cm:
                    fn(bad)
                self.assertEqual(cm.exception.status, 404)
            with self.assertRaises(MusicError):
                self.jobs.cancel(bad, user="admin")
        with self.assertRaises(MusicError):
            self.jobs.lifecycle("restart", user="admin")

    def test_list_cancel_lineage_lifecycle_tags_model(self):
        mine = self.jobs.submit("generate", {"prompt": "x"}, user="admin")["id"]
        self.stub.route("POST", "/v1/music/generations", "", b'{"prompt": "other"}', {})
        self.assertEqual(len(self.jobs.list()), 2)
        self.assertEqual([j["id"] for j in self.jobs.list(mine_only=True)], [mine])
        self.assertEqual(self.jobs.cancel(mine, user="admin")["status"], "cancelled")
        self.assertEqual(self.jobs.lineage(mine)["job"]["id"], mine)
        self.assertEqual(self.jobs.lifecycle("load", user="admin"), {"state": "ready"})
        self.assertEqual(self.audits[-1]["action"], "music.load")
        self.assertIn("synthwave", self.jobs.tags("syn", 5)["suggestions"])
        first = self.jobs.model()
        calls = len(self.stub.calls)
        self.assertIs(self.jobs.model(), first)  # cached for 5 s
        self.assertEqual(len(self.stub.calls), calls)

    def test_edit_needs_a_source(self):
        with self.assertRaises(MusicError) as cm:
            self.jobs.submit("edit", {"prompt": "x"}, user="admin")
        self.assertIn("source", str(cm.exception))
        with self.assertRaises(MusicError) as cm:
            self.jobs.submit("remix", {"source": {"job_id": job_id(), "index": 0}}, user="admin")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(self.jobs.pending(), [])

    def test_remix_from_a_job_links_the_parent_asset(self):
        parent_job = self.jobs.submit("generate", {"prompt": "x"}, user="admin")["id"]
        self.finish(parent_job)
        self.jobs.sweep()
        parent = self.library.find_by_job(parent_job)[0]
        child = self.jobs.submit("remix", {"source": {"job_id": parent_job, "index": 0}, "strength": 0.4},
                                 user="admin")
        self.assertEqual(child["parent_asset_id"], parent["id"])
        self.assertEqual(self.stub.jobs[child["id"]]["request"]["source"], {"job_id": parent_job, "index": 0})
        self.finish(child["id"])
        self.jobs.sweep()
        remixed = self.library.find_by_job(child["id"])[0]
        self.assertEqual((remixed["operation"], remixed["parent_id"]), ("remix", parent["id"]))
        self.assertEqual(self.library.get(parent["id"], lineage=True)["children"][0]["id"], remixed["id"])


class ResolveRefTests(StubBase):
    def test_shape_rejections(self):
        for ref in ("x", None, {}, {"job_id": "nope"}, {"job_id": job_id(), "index": True},
                    {"job_id": job_id(), "index": 8}, {"job_id": job_id(), "index": -1},
                    {"job_id": job_id(), "index": "0"}, {"upload_id": "upl-xyz"}, {"path": "/srv/models"}):
            with self.assertRaises(MusicError, msg=repr(ref)):
                self.jobs._resolve_ref(ref, "source")

    def test_direct_references(self):
        uid = "upl-" + secrets.token_hex(16)
        self.assertEqual(self.jobs._resolve_ref({"upload_id": uid}, "reference"), ({"upload_id": uid}, None))
        jid = job_id()
        self.assertEqual(self.jobs._resolve_ref({"job_id": jid}, "source"), ({"job_id": jid, "index": 0}, None))

    def test_asset_must_be_audio(self):
        image = self.library.add(NewAsset(type="image", ext="png", operation="upload", data=b"\x89PNG" + b"0" * 64))
        with self.assertRaises(MusicError) as cm:
            self.jobs._resolve_ref({"asset_id": image["id"]}, "source")
        self.assertIn("not audio", str(cm.exception))
        with self.assertRaises(LibraryError) as cm:
            self.jobs._resolve_ref({"asset_id": "a_" + "9" * 24}, "source")
        self.assertEqual(cm.exception.status, 404)

    def test_asset_with_a_live_node2_job(self):
        jid = self.jobs.submit("generate", {"prompt": "x"}, user="admin")["id"]
        self.finish(jid)
        asset = self.audio_asset(music_job_id=jid, track_index=0)
        self.assertEqual(self.jobs._resolve_ref({"asset_id": asset["id"]}, "source"),
                         ({"job_id": jid, "index": 0}, asset["id"]))
        uploads = [c for c in self.stub.calls if c == ("POST", "/v1/music/uploads")]
        self.assertEqual(uploads, [])

    def test_asset_is_uploaded_once_when_node2_forgot_the_job(self):
        asset = self.audio_asset(music_job_id=job_id(), track_index=0)
        ref, parent = self.jobs._resolve_ref({"asset_id": asset["id"]}, "source")
        self.assertRegex(ref["upload_id"], r"^upl-[0-9a-f]{32}$")
        self.assertEqual(parent, asset["id"])
        # remembered server-side, never shown
        self.assertNotIn("node2_upload_id", self.library.get(asset["id"])["settings"])
        again, _ = self.jobs._resolve_ref({"asset_id": asset["id"]}, "source")
        self.assertEqual(again, ref)
        uploads = [c for c in self.stub.calls if c == ("POST", "/v1/music/uploads")]
        self.assertEqual(len(uploads), 1)
        # node 2 lost the upload: upload again
        self.stub.uploads.clear()
        third, _ = self.jobs._resolve_ref({"asset_id": asset["id"]}, "source")
        self.assertNotEqual(third, ref)
        self.assertEqual(len([c for c in self.stub.calls if c == ("POST", "/v1/music/uploads")]), 2)

    def test_missing_library_file(self):
        asset = self.audio_asset()
        self.library.file_path(asset).unlink()
        with self.assertRaises(MusicError) as cm:
            self.jobs._resolve_ref({"asset_id": asset["id"]}, "source")
        self.assertEqual(cm.exception.status, 404)

    def test_submit_with_source_asset_id(self):
        asset = self.audio_asset()
        job = self.jobs.submit("extend", {"source_asset_id": asset["id"], "seconds": 10,
                                          "reference_asset_id": asset["id"]}, user="admin")
        self.assertEqual((job["parent_asset_id"], job["reference_asset_id"]), (asset["id"], asset["id"]))
        sent = self.stub.jobs[job["id"]]["request"]
        self.assertIn("upload_id", sent["source"])
        self.assertIn("upload_id", sent["reference"])
        self.assertNotIn("source_asset_id", sent)

    def test_user_upload_lands_in_the_library(self):
        asset = self.jobs.upload(sine_wav(0.1), "take one.wav", "audio/wav; codecs=1", title=None, user="admin")
        self.assertEqual((asset["type"], asset["operation"], asset["title"]), ("audio", "upload", "take one.wav"))
        self.assertEqual(asset["settings"]["uploaded_by"], "admin")
        self.assertNotIn("node2_upload_id", asset["settings"])
        for ctype, data in (("video/mp4", b"x"), ("audio/wav", b"")):
            with self.assertRaises(MusicError):
                self.jobs.upload(data, "x", ctype, title=None, user="admin")


if __name__ == "__main__":
    unittest.main()
