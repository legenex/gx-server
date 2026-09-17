"""Hermetic tests for the Open WebUI identity sync (D-038).

The prompts are built only from the registry and only describe the model the
alias is bound to NOW. The container side is replaced by a fake runner that
behaves like Open WebUI's model table; nothing touches Docker.
"""

from __future__ import annotations

import copy
import json
import re
import unittest

from support import REPO, TempEnv

from gx_control_ui import owui_identity as oi
from gx_control_ui.model_manager import ModelManager
from gx_control_ui.util import CmdResult

REGISTRY = json.loads((REPO / "legenex/models/registry.json").read_text())
FULL = "HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive"


class FakeOpenWebUI:
    """Open WebUI's model table as the container script sees it."""

    def __init__(self, version: str = oi.VERIFIED_OWUI) -> None:
        self.version = version
        self.rows: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.fail = False

    def __call__(self, args, timeout=10.0, input_text=None, merge_stderr=True):
        assert args[:4] == ["docker", "exec", "-i", "open-webui"], args
        assert "WEBUI_SECRET_KEY" in args[6] and "cat .webui_secret_key" in args[6]
        payload = json.loads(input_text)
        self.calls.append(payload)
        if self.fail:
            return CmdResult(1, "Traceback: boom", 1.0)
        out = {"version": self.version, "rows": {}, "written": [], "skipped": []}
        ids = payload.get("ids") or [r["id"] for r in payload.get("rows", [])]
        for mid in ids:
            out["rows"][mid] = copy.deepcopy(self.rows.get(mid))
        if payload["op"] == "upsert":
            for row in payload["rows"]:
                cur = self.rows.get(row["id"])
                if cur is not None and not cur["meta"].get(oi.MARKER) and not payload.get("adopt"):
                    out["skipped"].append({"id": row["id"], "reason": "row exists and was not created by the sync"})
                    continue
                self.rows[row["id"]] = {**copy.deepcopy(row), "is_active": True if cur is None else cur["is_active"],
                                        "user_id": "admin-1", "grants": 0 if cur is None else cur["grants"]}
                out["written"].append(row["id"])
        return CmdResult(0, "some import warning\n" + json.dumps(out) + "\n", 5.0)


def sync_with(fake: FakeOpenWebUI, registry: dict | None = None, env: TempEnv | None = None):
    path = (env.root if env else None)
    if path is None:
        raise AssertionError("env required")
    reg = path / "registry.json"
    reg.write_text(json.dumps(registry or REGISTRY))
    audits: list[dict] = []
    return oi.OpenWebUIIdentity(reg, runner=fake, audit=lambda **kw: audits.append(kw)), reg, audits


class PromptTests(unittest.TestCase):
    def test_gx_mini_prompt_states_only_verified_facts(self):
        entry = REGISTRY["aliases"]["gx-mini"]
        self.assertTrue(oi.facts_match(entry))
        p = oi.model_prompt("gx-mini", entry)
        for fact in ("You are gx-mini", FULL, "revision c09cdbcdb1fe", "derived from Qwen/Qwen3.5-4B",
                     "4B dense", "Q4_K_M", "BF16 vision projector", "not full precision", "llama.cpp", "gx10-01",
                     "65,536 tokens per request", "8,192", "answer yes"):
            self.assertIn(fact, p)
        self.assertIn("user-facing alias, not a model name", p)
        self.assertIn("Do not claim to be an official release of the base model, a larger model", p)
        self.assertNotRegex(p, r"\b(72|110|235)B\b")
        self.assertLess(len(p), 2000)

    def test_every_direct_alias_has_bound_verified_facts(self):
        for alias in ("gx-mini", "gx-fast", "gx-reason", "gx-max"):
            entry = REGISTRY["aliases"][alias]
            with self.subTest(alias=alias):
                self.assertTrue(oi.facts_match(entry), f"{alias} identity block does not match its binding")
                ident = entry["identity"]
                for key in ("base_model", "derivation", "parameters", "base_parameters", "weights", "verified"):
                    self.assertTrue(ident.get(key), key)
                self.assertIn(entry["repository"], oi.model_prompt(alias, entry))
        self.assertEqual(REGISTRY["aliases"]["gx-mini"]["identity"]["files_sha256"][
            "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"],
            "79e28ecacf84e75b6056cf4059636d435aa9eb67795780f7b7dbc7d32a962741")

    def test_after_a_reassignment_the_old_facts_are_not_reused(self):
        entry = copy.deepcopy(REGISTRY["aliases"]["gx-mini"])
        entry.update(repository="someone/Other-Model-GGUF", revision="0123456789abcdef")
        self.assertFalse(oi.facts_match(entry))
        p = oi.model_prompt("gx-mini", entry)
        self.assertIn("someone/Other-Model-GGUF (revision 0123456789ab)", p)
        self.assertIn("have not been verified", p)
        for stale in ("HauhauCS", "Qwen3.5-4B", "4B dense", "Q4_K_M"):
            self.assertNotIn(stale, p)
        self.assertNotIn("(from", oi.description("gx-mini", entry))
        # the runtime and the served limits still come from the binding
        self.assertIn("llama.cpp", p)
        self.assertIn("65,536", p)

    def test_router_prompt_never_names_a_model(self):
        p = oi.router_prompt(REGISTRY)
        self.assertIn("answering through gx-auto", p)
        self.assertIn("routing journal", p)
        self.assertIn("57,344", p)
        self.assertNotIn("HauhauCS", p)

    def test_missing_repository_is_an_error(self):
        with self.assertRaises(oi.IdentityError):
            oi.model_prompt("gx-mini", {"runtime": "llama.cpp"})

    def test_rows(self):
        rows = oi.desired_rows(REGISTRY, synced_at="2026-09-17T12:00:00+0200")
        self.assertEqual([r["id"] for r in rows], list(oi.ALIASES))
        for r in rows:
            self.assertEqual((r["name"], r["base_model_id"]), (r["id"], None))
            self.assertEqual(r["meta"]["tags"], [{"name": "gx-cluster"}])
            marker = r["meta"][oi.MARKER]
            self.assertEqual(marker["source"], "legenex/models/registry.json")
            self.assertRegex(marker["prompt_sha256"], r"^[0-9a-f]{16}$")
            self.assertNotRegex(json.dumps(r), r"sk-[A-Za-z0-9]{8,}")
        self.assertEqual(rows[0]["meta"][oi.MARKER]["repository"], FULL)
        self.assertEqual(oi.desired_rows(REGISTRY)[0]["params"], rows[0]["params"], "stable")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.fake = FakeOpenWebUI()

    def tearDown(self):
        self.env.cleanup()

    def test_plan_apply_and_nothing_to_do_afterwards(self):
        sync, _, audits = sync_with(self.fake, env=self.env)
        plan = sync.plan()
        self.assertEqual({i["state"] for i in plan["items"]}, {"missing"})
        self.assertFalse(plan["in_sync"])
        self.assertEqual(self.fake.calls[-1]["op"], "read")
        out = sync.apply(user="admin")
        self.assertEqual(out["written"], list(oi.ALIASES))
        self.assertTrue(out["in_sync"])
        self.assertEqual(audits[-1]["action"], "openwebui.identity.sync")
        self.assertIn(FULL, self.fake.rows["gx-mini"]["params"]["system"])
        writes = len([c for c in self.fake.calls if c["op"] == "upsert"])
        self.assertEqual(sync.apply(user="admin")["written"], [])
        self.assertEqual(len([c for c in self.fake.calls if c["op"] == "upsert"]), writes, "no write when in sync")

    def test_registry_change_is_drift_and_only_that_row_is_rewritten(self):
        sync, reg_path, _ = sync_with(self.fake, env=self.env)
        sync.apply()
        reg = json.loads(reg_path.read_text())
        reg["aliases"]["gx-fast"]["repository"] = "someone/New-Fast"
        reg_path.write_text(json.dumps(reg))
        states = {i["id"]: i["state"] for i in sync.plan()["items"]}
        self.assertEqual(states["gx-fast"], "drift")
        self.assertEqual(states["gx-mini"], "ok")
        out = sync.apply()
        self.assertEqual(out["written"], ["gx-fast"])
        self.assertIn("someone/New-Fast", self.fake.rows["gx-fast"]["params"]["system"])
        self.assertIn("have not been verified", self.fake.rows["gx-fast"]["params"]["system"])

    def test_rows_created_by_someone_else_are_never_overwritten_without_adopt(self):
        self.fake.rows["gx-mini"] = {"id": "gx-mini", "name": "My mini", "base_model_id": None, "is_active": True,
                                     "params": {"system": "custom"}, "meta": {"description": "mine"}, "grants": 2}
        sync, _, _ = sync_with(self.fake, env=self.env)
        self.assertEqual({i["id"]: i["state"] for i in sync.plan()["items"]}["gx-mini"], "foreign")
        out = sync.apply()
        self.assertEqual(out["skipped"], [{"id": "gx-mini", "reason": "row exists and was not created by the sync"}])
        self.assertEqual(self.fake.rows["gx-mini"]["params"]["system"], "custom")
        self.assertFalse(out["in_sync"])
        out = sync.apply(adopt=True)
        self.assertIn(FULL, self.fake.rows["gx-mini"]["params"]["system"])
        self.assertEqual(self.fake.rows["gx-mini"]["grants"], 2, "existing grants are kept")
        self.assertTrue(out["in_sync"])

    def test_disabled_row_is_reported_and_kept_disabled(self):
        sync, _, _ = sync_with(self.fake, env=self.env)
        sync.apply()
        self.fake.rows["gx-max"]["is_active"] = False
        self.assertEqual({i["id"]: i["state"] for i in sync.plan()["items"]}["gx-max"], "inactive")

    def test_version_gate(self):
        self.fake.version = "0.12.0"
        sync, _, _ = sync_with(self.fake, env=self.env)
        self.assertFalse(sync.plan()["version_ok"])
        with self.assertRaises(oi.IdentityError):
            sync.apply()
        self.assertEqual(self.fake.rows, {})
        self.assertTrue(sync.apply(force_version=True)["written"])

    def test_container_failure_is_an_identity_error(self):
        self.fake.fail = True
        sync, _, _ = sync_with(self.fake, env=self.env)
        with self.assertRaises(oi.IdentityError):
            sync.plan()

    def test_unreadable_registry(self):
        sync = oi.OpenWebUIIdentity(self.env.root / "missing.json", runner=self.fake)
        with self.assertRaises(oi.IdentityError):
            sync.plan()

    def test_cli(self):
        self.assertEqual(oi.main(["nonsense"]), 2)

    def test_container_script_touches_only_model_rows(self):
        script = oi._CONTAINER_SCRIPT
        self.assertIn("Models.insert_new_model", script)
        self.assertIn("Models.update_model_by_id", script)
        for forbidden in ("delete", "Chats", "Auths", "config", "set_access_grants"):
            self.assertNotIn(forbidden, script)
        self.assertIn("access_grants=None if current is not None else []", script)


class ModelManagerHookTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()

    def tearDown(self):
        self.env.cleanup()

    def manager(self):
        return ModelManager(self.env.cfg, cluster=None, hf=None)

    def test_success_and_failure_are_logged_never_raised(self):
        mm = self.manager()
        logs: list[str] = []

        class Job:
            def log(self, line):
                logs.append(line)

        mm._sync_identity(Job())  # no hook: nothing happens
        self.assertEqual(logs, [])
        mm.identity_sync = lambda: {"in_sync": True, "written": ["gx-mini"]}
        mm._sync_identity(Job())
        self.assertEqual(logs[-1], "Open WebUI identity entries in sync (updated gx-mini)")

        def boom():
            raise oi.IdentityError("open-webui did not answer")
        mm.identity_sync = boom
        mm._sync_identity(Job())
        self.assertTrue(re.match(r"Open WebUI identity sync failed: open-webui did not answer", logs[-1]))
        self.assertIn("owui_identity apply", logs[-1])

    def test_assign_and_rollback_call_the_hook(self):
        src = (REPO / "legenex/control-ui/gx_control_ui/model_manager.py").read_text()
        assign = src[src.index("    def _assign("):src.index("    def _sync_identity(")]
        rollback = src[src.index("    def _rollback("):src.index("    def accept(")]
        self.assertIn("self._sync_identity(job)", assign)
        self.assertIn("self._sync_identity(job)", rollback)


if __name__ == "__main__":
    unittest.main()
