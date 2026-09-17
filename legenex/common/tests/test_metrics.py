import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gxcommon.metrics import Metrics, allowed_field, clean_fields, read_metrics, redact_text  # noqa: E402


class FieldRules(unittest.TestCase):
    def test_content_and_secret_names_are_dropped(self):
        for name in ("text", "prompt", "transcript", "authorization", "api_key", "openai_api_key", "cookie",
                     "session_token", "ticket", "password", "key", "messages", "content", "db_secret"):
            self.assertFalse(allowed_field(name), name)

    def test_ordinary_names_are_kept(self):
        for name in ("job_id", "session_id", "duration_ms", "alias", "completion_tokens", "prompt_tokens",
                     "error_code", "waiting_reason", "user", "owner", "bytes_in"):
            self.assertTrue(allowed_field(name), name)

    def test_values_are_redacted_and_truncated(self):
        fake = "sk" + "-" + "abcdefghijklmnop"  # built at run time: no key-shaped literal in the repo
        out = redact_text(f"key {fake} and Bearer " + "abcdefghijklmnopq\n" + "x" * 500)
        self.assertNotIn(fake, out)
        self.assertNotIn("abcdefghijklmnopq", out)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), 200)

    def test_nested_values(self):
        out = clean_fields({"resident": {"gx-voice": 12.5, "prompt": "x", "bad": object()},
                            "stages": [1, 2, "a"], "blob": object(), "mixed": [object()],
                            "nan": float("nan")})
        self.assertEqual(out["resident"], {"gx-voice": 12.5})
        self.assertEqual(out["stages"], [1, 2, "a"])
        self.assertNotIn("blob", out)
        self.assertNotIn("mixed", out)
        self.assertIsNone(out["nan"])


class Emit(unittest.TestCase):
    def test_line_shape_and_file_sink(self):
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "m.jsonl"
            m = Metrics("gx-call", node="gx10-02", file=path, stream=buf)
            m.emit("model.load", alias="gx-call", outcome="ok", duration_ms=5, prompt="secret words",
                   user="user:admin")
            line = json.loads(buf.getvalue())
            self.assertEqual(line["kind"], "metric")
            self.assertEqual(line["service"], "gx-call")
            self.assertEqual(line["node"], "gx10-02")
            self.assertEqual(line["event"], "model.load")
            self.assertNotIn("prompt", line)
            self.assertEqual(json.loads(path.read_text()), line)
            self.assertEqual(path.stat().st_mode & 0o007, 0)
            got = read_metrics([path], user="user:admin")
            self.assertEqual(len(got), 1)
            self.assertEqual(read_metrics([path], user="user:other"), [])

    def test_reserved_fields_cannot_be_overwritten(self):
        buf = io.StringIO()
        Metrics("gx-live", node="n", stream=buf).emit("failure", kind="x", service="evil", event="bad")
        line = json.loads(buf.getvalue())
        self.assertEqual((line["kind"], line["service"], line["event"]), ("metric", "gx-live", "failure"))

    def test_invalid_event_and_service(self):
        with self.assertRaises(ValueError):
            Metrics("GX Call")
        with self.assertRaises(ValueError):
            Metrics("gx-call", stream=io.StringIO()).emit("Bad Event!")

    def test_timer_success_and_failure(self):
        buf = io.StringIO()
        m = Metrics("gx-voice", node="n", stream=buf)
        with m.timer("generation", alias="gx-voice") as t:
            t.set(audio_seconds=1.5)

        class Boom(Exception):
            code = "engine_down"

        with self.assertRaises(Boom), m.timer("generation", alias="gx-voice"):
            raise Boom()
        ok, bad = [json.loads(x) for x in buf.getvalue().splitlines()]
        self.assertEqual(ok["outcome"], "ok")
        self.assertEqual(ok["audio_seconds"], 1.5)
        self.assertIn("duration_ms", ok)
        self.assertEqual((bad["outcome"], bad["error_code"]), ("failed", "engine_down"))

    def test_write_failure_never_raises(self):
        class Broken(io.StringIO):
            def write(self, s):
                raise OSError("disk full")

        Metrics("gx-voice", node="n", stream=Broken(), file="/proc/forbidden/x.jsonl").emit("failure")

    def test_disabled(self):
        buf = io.StringIO()
        self.assertIsNone(Metrics("gx-voice", stream=buf, enabled=False).emit("failure"))
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
