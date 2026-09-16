from __future__ import annotations

import unittest

from support import env_vars, fake_key

from gx_control_ui.redact import MASK, is_credential_key, redact, redact_obj


class TestRedact(unittest.TestCase):
    def test_exact_secret_values(self):
        key = fake_key("zz")
        with env_vars(GX_SWAP_API_KEY=key):
            out = redact(f"curl -H x:{key} http://h")
        self.assertNotIn(key, out)
        self.assertIn(MASK, out)

    def test_placeholders_are_not_treated_as_secrets(self):
        with env_vars(GX_MEDIA_API_KEY="not-required"):
            self.assertEqual(redact("this is not-required text"), "this is not-required text")

    def test_bearer_and_authorization(self):
        token = fake_key("tok")
        for text in (f"Authorization: Bearer {token}", f"authorization=bearer {token}", f"got bearer {token} ok"):
            out = redact(text)
            self.assertNotIn(token, out, text)

    def test_key_value_pairs(self):
        secret = fake_key("v")
        for text in (f"api_key={secret}", f'"password": "{secret}"', f"MASTER_KEY: {secret}",
                     f"client_secret={secret}&x=1", f"token={secret}"):
            self.assertNotIn(secret, redact(text), text)

    def test_known_token_shapes(self):
        samples = [
            fake_key("sk-"),
            "gh" + "p_" + "A" * 36,
            "github_" + "pat_" + "B" * 30,
            "hf" + "_" + "C" * 34,
            "AK" + "IA" + "D" * 16,
        ]
        for s in samples:
            self.assertNotIn(s, redact(f"value {s} end"), s)

    def test_url_credentials(self):
        pw = fake_key("p")
        out = redact(f"postgres://litellm:{pw}@db:5432/x")
        self.assertNotIn(pw, out)
        self.assertIn("postgres://litellm:", out)
        self.assertIn("@db:5432", out)

    def test_private_key_block(self):
        body = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH " + "PRIVATE KEY-----"
        self.assertNotIn("abc", redact(body))

    def test_harmless_text_untouched(self):
        text = "2026-09-16 INFO gx.server 127.0.0.1 GET /health 200 tokens=42 max_tokens=512"
        self.assertEqual(redact(text), text)

    def test_obj_masks_credential_keys_but_keeps_counters(self):
        data = {"api_key": "x", "Authorization": "y", "access_token": "z", "token": "t",
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
                "max_tokens": 10, "nested": [{"password": "p"}]}
        out = redact_obj(data)
        for k in ("api_key", "Authorization", "access_token", "token"):
            self.assertEqual(out[k], MASK, k)
        self.assertEqual(out["usage"], {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12})
        self.assertEqual(out["max_tokens"], 10)
        self.assertEqual(out["nested"][0]["password"], MASK)

    def test_is_credential_key(self):
        self.assertTrue(is_credential_key("GX_SWAP_API_KEY"))
        self.assertFalse(is_credential_key("reasoning_tokens"))
        self.assertFalse(is_credential_key("tokens"))



class TestJsonLogging(unittest.TestCase):
    def test_log_lines_are_json_and_redacted(self):
        import json
        import logging

        from gx_control_ui.server import JsonFormatter
        secret = fake_key()
        record = logging.LogRecord("gx.ui", logging.INFO, __file__, 1, "call with Bearer %s done", (secret,), None)
        line = JsonFormatter().format(record)
        data = json.loads(line)
        self.assertEqual(data["level"], "INFO")
        self.assertNotIn(secret, line)


if __name__ == "__main__":
    unittest.main()
