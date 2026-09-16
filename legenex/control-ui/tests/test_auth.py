from __future__ import annotations

import json
import os
import time
import unittest

from support import TempEnv  # noqa: F401  (sets sys.path)

from gx_control_ui import auth
from gx_control_ui.auth import (
    AuthError, LoginThrottle, PasswordStore, SessionManager, csrf_ok, hash_password, verify_password,
)

FAST_N = 2**10  # keep scrypt cheap in tests; production uses 2**15


class TestHashing(unittest.TestCase):
    def test_roundtrip_and_wrong_password(self):
        rec = hash_password("correct horse battery", n=FAST_N)
        self.assertEqual(rec["algo"], "scrypt")
        self.assertNotIn("correct", json.dumps(rec))
        self.assertTrue(verify_password("correct horse battery", rec))
        self.assertFalse(verify_password("correct horse batterY", rec))
        self.assertFalse(verify_password("", rec))

    def test_salt_is_random(self):
        a = hash_password("same password here", n=FAST_N)
        b = hash_password("same password here", n=FAST_N)
        self.assertNotEqual(a["salt"], b["salt"])
        self.assertNotEqual(a["hash"], b["hash"])

    def test_malformed_record_never_verifies(self):
        for rec in ({}, {"algo": "md5"}, {"algo": "scrypt", "salt": "!!", "hash": "x", "n": 2, "r": 1, "p": 1}):
            self.assertFalse(verify_password("anything goes", rec))

    def test_production_cost_parameters(self):
        self.assertGreaterEqual(auth.SCRYPT_N, 2**15)
        self.assertGreaterEqual(auth.SCRYPT_R, 8)


class TestPolicy(unittest.TestCase):
    def test_password_policy(self):
        for bad in ("short", "a" * 20, " leading-space-pass", "x" * 2000):
            with self.assertRaises(AuthError):
                auth.validate_new_password(bad)
        auth.validate_new_password("Tr0ub4dor&3-horse")

    def test_username_policy(self):
        self.assertEqual(auth.validate_username(" Admin "), "admin")
        for bad in ("", "a b", "x" * 40, "root;rm"):
            with self.assertRaises(AuthError):
                auth.validate_username(bad)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.store = PasswordStore(self.env.cfg.password_file)

    def tearDown(self):
        self.env.cleanup()

    def test_unconfigured(self):
        self.assertFalse(self.store.configured())
        self.assertFalse(self.store.check("admin", "whatever-password"))
        self.assertEqual(self.store.generation(), 0)

    def test_set_check_and_generation(self):
        self.store.set_password("admin", "first-Password-1", n=FAST_N)
        self.assertTrue(self.store.check("admin", "first-Password-1"))
        self.assertTrue(self.store.check("ADMIN", "first-Password-1"))
        self.assertFalse(self.store.check("other", "first-Password-1"))
        self.assertEqual(self.store.generation(), 1)
        time.sleep(0.01)
        self.store.set_password("admin", "second-Password-2", n=FAST_N)
        self.assertEqual(self.store.generation(), 2)
        self.assertFalse(self.store.check("admin", "first-Password-1"))
        self.assertTrue(self.store.check("admin", "second-Password-2"))

    def test_file_mode_and_no_plaintext(self):
        self.store.set_password("admin", "Plaintext-Never-Stored-9", n=FAST_N)
        st = os.stat(self.env.cfg.password_file)
        self.assertEqual(st.st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.env.cfg.secret_dir).st_mode & 0o777, 0o700)
        self.assertNotIn("Plaintext-Never-Stored-9", self.env.cfg.password_file.read_text())

    def test_refuses_group_readable_store(self):
        self.store.set_password("admin", "some-Password-123", n=FAST_N)
        os.chmod(self.env.cfg.password_file, 0o644)
        fresh = PasswordStore(self.env.cfg.password_file)
        with self.assertRaises(AuthError):
            fresh.load()
        self.assertFalse(fresh.configured())

    def test_malformed_store(self):
        self.env.cfg.secret_dir.mkdir(parents=True, exist_ok=True)
        auth.write_private_file(self.env.cfg.password_file, "[]")
        self.assertFalse(PasswordStore(self.env.cfg.password_file).configured())


class TestSessions(unittest.TestCase):
    def test_create_get_destroy(self):
        sm = SessionManager(idle_seconds=60, max_seconds=600)
        token, sess = sm.create("admin", 1, "1.2.3.4")
        self.assertGreaterEqual(len(token), 40)
        self.assertIs(sm.get(token, 1), sess)
        self.assertIsNone(sm.get(token + "x", 1))
        self.assertIsNone(sm.get(None, 1))
        self.assertIsNone(sm.get("y" * 500, 1))
        sm.destroy(token)
        self.assertIsNone(sm.get(token, 1))

    def test_token_not_stored_in_clear(self):
        sm = SessionManager(60, 600)
        token, _ = sm.create("admin", 1)
        self.assertNotIn(token, sm._sessions)

    def test_idle_and_absolute_expiry(self):
        sm = SessionManager(idle_seconds=60, max_seconds=600)
        token, sess = sm.create("admin", 1)
        sess.last_seen -= 61
        self.assertIsNone(sm.get(token, 1))
        token, sess = sm.create("admin", 1)
        sess.created -= 601
        self.assertIsNone(sm.get(token, 1))

    def test_generation_change_invalidates(self):
        sm = SessionManager(60, 600)
        token, _ = sm.create("admin", 1)
        self.assertIsNone(sm.get(token, 2))

    def test_capacity_evicts_oldest(self):
        sm = SessionManager(60, 600)
        first, s = sm.create("admin", 1)
        s.last_seen -= 30
        for _ in range(SessionManager.MAX_SESSIONS):
            sm.create("admin", 1)
        self.assertIsNone(sm.get(first, 1))
        self.assertLessEqual(sm.count(), SessionManager.MAX_SESSIONS)

    def test_csrf(self):
        sm = SessionManager(60, 600)
        _, sess = sm.create("admin", 1)
        self.assertTrue(csrf_ok(sess, sess.csrf))
        self.assertFalse(csrf_ok(sess, None))
        self.assertFalse(csrf_ok(sess, ""))
        self.assertFalse(csrf_ok(sess, sess.csrf[:-1] + "x"))


class TestThrottle(unittest.TestCase):
    def test_lockout_after_failures(self):
        t = LoginThrottle(per_ip=3, global_limit=100, window=60, lockout=60)
        for _ in range(2):
            t.failure("10.0.0.1")
            self.assertEqual(t.blocked("10.0.0.1"), 0)
        t.failure("10.0.0.1")
        self.assertGreater(t.blocked("10.0.0.1"), 0)
        self.assertEqual(t.blocked("10.0.0.2"), 0)

    def test_success_resets(self):
        t = LoginThrottle(per_ip=3, global_limit=100)
        t.failure("a")
        t.failure("a")
        t.success("a")
        t.failure("a")
        self.assertEqual(t.blocked("a"), 0)

    def test_global_limit(self):
        t = LoginThrottle(per_ip=100, global_limit=5, window=60)
        for i in range(5):
            t.failure(f"ip{i}")
        self.assertGreater(t.blocked("fresh"), 0)


if __name__ == "__main__":
    unittest.main()


class AcceptanceAccountTests(unittest.TestCase):
    """D-035: the acceptance account signs in from loopback only."""

    def test_loopback_only_and_separate_from_admin(self):
        import json as _json
        import urllib.request as _ur
        from tests.support import TestEnv
        env = self.enterContext(TestEnv()) if hasattr(TestEnv, "__enter__") else TestEnv()
        store = PasswordStore(env.cfg.acceptance_file)
        store.set_password("acceptance", "Acceptance-Pass-123", n=2**10)
        self.assertTrue(store.check("acceptance", "Acceptance-Pass-123"))
        self.assertFalse(PasswordStore(env.cfg.password_file).check("acceptance", "Acceptance-Pass-123"))
