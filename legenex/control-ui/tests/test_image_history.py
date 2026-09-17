"""Build V3 (IMG): gx-image history, per-model provenance and edit lineage.

Covers migration 070_images.sql and ``image_catalog.ImageHistory``: what the
observer records for a generation, an edit and a failure, the perceptual
distance that tells a real edit from a returned copy, and the read APIs.
"""

from __future__ import annotations

import base64
import struct
import threading
import unittest
import zlib
from pathlib import Path

from support import TempEnv

from gx_control_ui.image_catalog import (
    NEAR_DUPLICATE_BITS, CatalogError, ImageCatalog, ImageHistory, dhash, hamming,
)
from gx_control_ui.media_jobs import MediaJobs
from gx_control_ui.media_library import MediaLibrary, MediaTools, NewAsset

REPO = Path(__file__).resolve().parents[3]


def png(width: int, height: int, pixel) -> bytes:
    """An 8-bit RGB PNG whose pixels come from `pixel(x, y) -> (r, g, b)`."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b"") + b"\x00" * 1200)


def ramp(width: int = 64, height: int = 48, *, flip: bool = False) -> bytes:
    """A horizontal luminance ramp; `flip` mirrors it, which changes every bit."""
    def pixel(x, y):
        v = int(255 * (x / (width - 1)))
        return (255 - v, 255 - v, 255 - v) if flip else (v, v, v)
    return png(width, height, pixel)


class HashTests(unittest.TestCase):
    def test_same_picture_has_distance_zero_and_a_mirror_is_far(self):
        a, b = ramp(), ramp()
        self.assertEqual(dhash_bytes(a), dhash_bytes(b))
        self.assertEqual(hamming(dhash_bytes(a), dhash_bytes(b)), 0)
        far = hamming(dhash_bytes(a), dhash_bytes(ramp(flip=True)))
        self.assertGreater(far, NEAR_DUPLICATE_BITS)

    def test_unreadable_input_returns_none(self):
        self.assertIsNone(dhash_bytes(b"not a png at all"))
        self.assertIsNone(hamming(None, "0" * 16))
        self.assertIsNone(hamming("zz", "0" * 16))

    def test_hash_is_sixteen_hex_characters(self):
        value = dhash_bytes(ramp())
        assert value is not None
        self.assertRegex(value, r"^[0-9a-f]{16}$")


def dhash_bytes(data: bytes) -> str | None:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(data)
        path = Path(fh.name)
    try:
        return dhash(path)
    finally:
        path.unlink(missing_ok=True)


class FakeRouter:
    """The media router, returning one image whose content the test chooses."""

    def __init__(self, image: bytes, *, fail: bool = False):
        self.image = image
        self.fail = fail
        self.calls: list = []

    def get_json(self, path, timeout=30):
        return {"resident_alias": None}

    def _payload(self, model, workflow, gx_extra=None):
        if self.fail:
            from gx_control_ui.media_jobs import JobError
            raise JobError("gx10-02 ran out of memory", 502, "out_of_memory")
        return {"data": [{"b64_json": base64.b64encode(self.image).decode()}],
                "gx": {"workflow": workflow, "seed": 7, "size": "64x48", "image_model": model,
                       "adapter_strength": 0.0, "prompt_suffix": None, **(gx_extra or {})}}

    def post_json(self, path, body, timeout):
        self.calls.append((path, body))
        model = body.get("image_model") or "qwen-image-2512"
        workflow = ("sdxl-visionmaster-pro-v3" if model == "visionmaster-pro-v3"
                    else "qwen-image-2512-uncensored")
        return self._payload(model, workflow)

    def post_multipart(self, path, ctype, data, timeout):
        self.calls.append((path, ctype))
        return self._payload("qwen-image-edit-2511", "qwen-image-edit-2511",
                             {"strength": None, "prompt_sent": "a beach. Change only what this instruction asks for.",
                              "edit": {"edit_mode": "change", "denoise": 1.0, "strength_applied": None,
                                       "masked": False, "workflow": "qwen-image-edit-2511"}})


class HistoryTests(unittest.TestCase):
    image = ramp()

    def setUp(self):
        self.env = TempEnv()
        self.lib = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))
        self.router = FakeRouter(self.image)
        self.catalog = ImageCatalog(REPO)
        self.jobs = MediaJobs(self.lib, self.router, model_identity=self.identity, poll_interval=0.01)
        self.jobs.catalog = self.catalog
        self.history = ImageHistory(self.lib, self.jobs, self.catalog)
        # Appended after ImageHistory, so it fires once the history row is written.
        self.recorded: dict[str, threading.Event] = {}
        self.jobs.observers.append(self._mark)

    def tearDown(self):
        self.env.cleanup()

    def _mark(self, job, event) -> None:
        if event in ("ready", "failed", "cancelled"):
            self.recorded.setdefault(job.id, threading.Event()).set()

    @staticmethod
    def identity(workflow: str) -> dict:
        if workflow.startswith("sdxl"):
            return {"repository": "votepurchase/pornmasterPro_noobV3VAE",
                    "revision": "75f59d136b165d48f3e678bb057af99f7cf1a71e", "components": []}
        return {"repository": "Comfy-Org/Qwen-Image_ComfyUI", "revision": "r1", "components": []}

    def wait(self, job_id):
        """Wait for the job AND for the observer that records its history.

        MediaJobs sets the final phase before it notifies its observers, so a
        test that only polled the job could read a half-written row. The probe
        observer is registered after ImageHistory, so its event means the
        history row is complete."""
        done = self.recorded.setdefault(job_id, threading.Event())
        if not done.wait(60):
            self.fail(f"job {job_id} did not finish: {self.jobs.get(job_id)}")
        return self.jobs.get(job_id)

    def test_migration_created_the_tables(self):
        with self.lib.connect() as con:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'img_%'")}
            applied = {r[0] for r in con.execute("SELECT name FROM schema_migrations")}
        self.assertEqual(names, {"img_generations", "img_outputs", "img_checkpoints"})
        self.assertIn("070_images.sql", applied)

    def test_generation_records_model_provenance_and_output(self):
        job = self.jobs.submit({"kind": "t2i", "prompt": "a lighthouse",
                                "image_model": "visionmaster-pro-v3", "size": "832x1216",
                                "quality_tags": False}, user="tester")
        done = self.wait(job["id"])
        self.assertEqual(done["phase"], "ready", done)
        row = self.history.generation(job["id"])
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["kind"], "t2i")
        self.assertEqual(row["image_model"], "visionmaster-pro-v3")
        self.assertEqual(row["image_model_label"], "VisionmasterPro_V3")
        self.assertEqual(row["image_model_family"], "sdxl")
        self.assertEqual(row["model_repository"], "votepurchase/pornmasterPro_noobV3VAE")
        self.assertEqual(row["model_revision"], "75f59d136b165d48f3e678bb057af99f7cf1a71e")
        self.assertEqual(row["workflow"], "sdxl-visionmaster-pro-v3")
        self.assertIs(row["quality_tags"], False)
        self.assertEqual(row["user"], "tester")
        self.assertFalse(row["masked"])
        self.assertIsNotNone(row["duration_seconds"])
        self.assertEqual(len(row["outputs"]), 1)
        out = row["outputs"][0]
        self.assertEqual(out["asset_id"], done["assets"][0])
        self.assertEqual(out["operation"], "generate")
        self.assertIsNone(out["source_asset_id"])
        self.assertIsNone(out["near_duplicate"], "a generation has nothing to compare against")
        self.assertRegex(out["dhash"], r"^[0-9a-f]{16}$")
        usage = self.history.model_usage()
        self.assertEqual([(c["image_model"], c["workflow"], c["revision"], c["generations"])
                          for c in usage["checkpoints"]],
                         [("visionmaster-pro-v3", "sdxl-visionmaster-pro-v3",
                           "75f59d136b165d48f3e678bb057af99f7cf1a71e", 1)])

    def test_edit_that_returns_the_source_is_recorded_as_a_near_duplicate(self):
        src = self.lib.add(NewAsset(type="image", ext="png", operation="upload", data=self.image,
                                    width=64, height=48))
        job = self.jobs.submit({"kind": "edit", "prompt": "a beach", "source_id": src["id"],
                                "edit_mode": "change"}, user="tester")
        done = self.wait(job["id"])
        self.assertEqual(done["phase"], "ready", done)
        row = self.history.generation(job["id"])
        self.assertEqual(row["kind"], "edit")
        self.assertEqual(row["edit_mode"], "change")
        self.assertEqual(row["source_asset_id"], src["id"])
        self.assertEqual(row["denoise"], 1.0)
        self.assertIsNone(row["strength_applied"], "an instruction edit has no denoise strength")
        out = row["outputs"][0]
        self.assertEqual(out["source_asset_id"], src["id"])
        self.assertEqual(out["similarity_method"], "dhash64")
        self.assertEqual(out["similarity_distance"], 0)
        self.assertEqual(out["similarity_score"], 1.0)
        self.assertIs(out["near_duplicate"], True)

    def test_a_real_edit_is_not_a_near_duplicate_and_the_lineage_is_a_chain(self):
        src = self.lib.add(NewAsset(type="image", ext="png", operation="upload", data=ramp(flip=True),
                                    width=64, height=48))
        job = self.jobs.submit({"kind": "edit", "prompt": "a beach", "source_id": src["id"],
                                "edit_mode": "change"}, user="tester")
        done = self.wait(job["id"])
        self.assertEqual(done["phase"], "ready", done)
        out = self.history.generation(job["id"])["outputs"][0]
        self.assertGreater(out["similarity_distance"], NEAR_DUPLICATE_BITS)
        self.assertIs(out["near_duplicate"], False)
        chain = self.history.lineage(done["assets"][0])
        self.assertEqual([c["asset_id"] for c in chain], [done["assets"][0]])
        self.assertEqual(chain[0]["source_asset_id"], src["id"])

    def test_failure_is_recorded_with_its_code(self):
        self.router.fail = True
        job = self.jobs.submit({"kind": "t2i", "prompt": "a lighthouse"}, user="tester")
        done = self.wait(job["id"])
        self.assertEqual(done["phase"], "failed")
        row = self.history.generation(job["id"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "out_of_memory")
        self.assertIn("memory", (row["error_message"] or "") + (row["error_detail"] or ""))
        self.assertEqual(row["outputs"], [])

    def test_listing_filters_and_paginates(self):
        for model in ("qwen-image-2512", "visionmaster-pro-v3", "qwen-image-2512"):
            size = "832x1216" if model == "visionmaster-pro-v3" else "1328x1328"
            self.wait(self.jobs.submit({"kind": "t2i", "prompt": "x", "image_model": model,
                                        "size": size}, user="tester")["id"])
        listing = self.history.generations()
        self.assertEqual(listing["total"], 3)
        self.assertEqual(len(listing["data"]), 3)
        only = self.history.generations(image_model="visionmaster-pro-v3")
        self.assertEqual(only["total"], 1)
        self.assertEqual(only["data"][0]["image_model"], "visionmaster-pro-v3")
        page = self.history.generations(limit=2, offset=2)
        self.assertEqual((page["total"], len(page["data"])), (3, 1))
        self.assertEqual(self.history.generations(kind="edit")["total"], 0)

    def test_unknown_generation_is_refused(self):
        with self.assertRaises(CatalogError):
            self.history.generation("0" * 16)

    def test_events_that_arrive_out_of_order_still_record_the_result(self):
        """MediaJobs enqueues a job before it delivers "submitted", so a job
        that fails at once can report its end first."""
        job = _FakeImageJob()
        self.history.observe(job, "failed")
        self.history.observe(job, "submitted")
        row = self.history.generation(job.id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "router_unavailable")
        self.assertEqual(row["prompt"], "a lighthouse")
        self.assertEqual(row["image_model"], "qwen-image-2512")

    def test_video_jobs_are_ignored(self):
        with self.lib.connect() as con:
            before = con.execute("SELECT COUNT(*) FROM img_generations").fetchone()[0]
        self.history.observe(_FakeVideoJob(), "submitted")
        with self.lib.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM img_generations").fetchone()[0], before)


class _FakeVideoJob:
    kind = "t2v"
    id = "f" * 16
    params: dict = {}
    user = "tester"
    created = 0.0
    phase = "queued"


class _FakeImageJob:
    """A finished image job, for delivering observer events by hand."""

    kind = "t2i"
    id = "a" * 16
    params = {"kind": "t2i", "prompt": "a lighthouse", "image_model": "qwen-image-2512", "size": "1328x1328"}
    user = "tester"
    family = "qwen-image"
    created = 1.0
    started = 1.0
    ended = 2.0
    phase = "failed"
    detail = "submitted to gx10-02"
    error = "gx10-02 could not be reached; try again shortly"
    error_code = "router_unavailable"
    error_hint = None
    elapsed_generation = None
    router_job = None
    assets: list = []


if __name__ == "__main__":
    unittest.main()


class RouteTests(unittest.TestCase):
    """The IMG routes register as session-authenticated browser routes."""

    def test_routes_are_registered_and_private(self):
        from gx_control_ui import routes_img  # noqa: F401 - importing registers the routes
        from gx_control_ui.server import Handler

        found = {(m, p.pattern): access for m, p, _fn, access in Handler.routes
                 if p.pattern.startswith("/api/images/")}
        self.assertEqual(set(found.values()), {"session"},
                         "image history must never be reachable without a session")
        patterns = set(found)
        self.assertIn(("GET", r"/api/images/generations"), patterns)
        self.assertIn(("GET", r"/api/images/models"), patterns)
        self.assertTrue(any(m == "GET" and "lineage" in p for m, p in patterns))
        self.assertFalse(any(m != "GET" for m, _p in patterns), "the history API is read-only")
