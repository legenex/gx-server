#!/usr/bin/env python3
"""Live acceptance of the Control UI backend (D-034/D-035), as the loopback-only
`acceptance` account, against the real cluster.

    gx_ui_live_check.py keys          # virtual key: create -> use -> restrict -> revoke -> refused
    gx_ui_live_check.py library       # media jobs + library operations (generates real media)
    gx_ui_live_check.py manager       # Model Manager test mode on a tiny public GGUF

No secret is printed: the temporary API key lives in memory only, and the
report records its masked form. Results: /srv/logs/acceptance/<UTC>-ui-<suite>.json
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UI = "http://127.0.0.1:8088"
GATEWAY = "http://127.0.0.1:4000/v1"
PASSWORD_FILE = Path("/srv/projects/gx-cluster/secrets/control-ui/acceptance-password")
OUT = Path("/srv/logs/acceptance")


class Session:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        body = self.call("POST", "/api/login", {"username": "acceptance",
                                                "password": PASSWORD_FILE.read_text().strip()})
        self.csrf = body["csrf"]

    def call(self, method: str, path: str, body=None, *, raw: bytes | None = None, ctype: str = "application/json",
             headers: dict | None = None, expect: int | None = None, timeout: float = 600):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(UI + path, data=data, method=method)
        req.add_header("Origin", UI)
        if data is not None:
            req.add_header("Content-Type", ctype)
        if self.csrf and method != "GET":
            req.add_header("X-CSRF-Token", self.csrf)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                status, payload, ct = r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status, payload, ct = exc.code, exc.read(), exc.headers.get("Content-Type", "")
        if expect is not None and status != expect:
            raise AssertionError(f"{method} {path}: HTTP {status}, expected {expect}: {payload[:300]!r}")
        if "json" in ct:
            return json.loads(payload or b"null")
        return payload


def gateway(secret: str, method: str, path: str, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GATEWAY + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except ValueError:
            return exc.code, {}


class Report:
    def __init__(self, suite: str) -> None:
        self.suite = suite
        self.rows: list[dict] = []

    def check(self, name: str, ok: bool, **info) -> bool:
        self.rows.append({"check": name, "pass": bool(ok), **info})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(info, default=str)[:300]}", flush=True)
        return ok

    def save(self) -> int:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = OUT / f"{stamp}-ui-{self.suite}.json"
        path.write_text(json.dumps(self.rows, indent=2, default=str))
        passed = sum(r["pass"] for r in self.rows)
        print(f"SUMMARY ui-{self.suite}: {passed}/{len(self.rows)} passed -> {path}")
        return 0 if passed == len(self.rows) else 1


# ---------------------------------------------------------------- keys
def cleanup_temp_keys(s: Session) -> list[str]:
    removed = []
    for k in s.call("GET", "/api/keys", expect=200)["keys"]:
        if k["name"].startswith("acceptance-temp-"):
            s.call("POST", f"/api/keys/{k['id']}/revoke", {"confirm": True}, expect=200)
            removed.append(k["name"])
    return removed


def suite_keys(s: Session, rep: Report) -> None:
    leftovers = cleanup_temp_keys(s)
    if leftovers:
        print(f"removed leftover test keys: {leftovers}")
    try:
        _suite_keys(s, rep)
    finally:
        cleanup_temp_keys(s)


def _suite_keys(s: Session, rep: Report) -> None:
    name = f"acceptance-temp-{int(time.time())}"
    created = s.call("POST", "/api/keys", {"name": name, "models": ["gx-mini", "gx-fast"], "expiry": "1d"}, expect=200)
    secret = created["secret"]
    rep.check("create returns the secret once, masked copy for display", secret.startswith("sk-") and
              created["masked"] != secret, masked=created["masked"], expires=created.get("expires"))
    listing = s.call("GET", "/api/keys", expect=200)
    raw = json.dumps(listing)
    mine = [k for k in listing["keys"] if k["name"] == name]
    rep.check("listing shows the key, masked, and never a secret", bool(mine) and secret not in raw and
              all("secret" not in k for k in listing["keys"]), listed=len(listing["keys"]),
              status=mine[0]["status"] if mine else None)
    master = ""
    for line in (Path(__file__).resolve().parents[1] / "gateway" / ".env").read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            master = line.split("=", 1)[1].strip().strip('"')
    pages = [raw] + [json.dumps(s.call("GET", p_)) for p_ in (
        "/api/playground/config", "/api/system", "/api/overview", "/api/models", "/api/manager/inventory",
        "/api/media/options", "/api/docs")]
    rep.check("the LiteLLM master key appears in no browser-facing API response",
              bool(master) and all(master not in body for body in pages), responses_checked=len(pages))
    status, models = gateway(secret, "GET", "/models")
    ids = [m["id"] for m in models.get("data", [])]
    rep.check("GET /v1/models works with the new key", status == 200 and "gx-mini" in ids, status=status, models=ids)
    t0 = time.time()
    status, chat = gateway(secret, "POST", "/chat/completions", {
        "model": "gx-mini", "max_tokens": 16, "temperature": 0,
        "messages": [{"role": "user", "content": "Reply with the word: pong"}]})
    answer = ((chat.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    rep.check("gx-mini chat works with the new key", status == 200 and "pong" in answer.lower(),
              status=status, answer=answer[:40], seconds=round(time.time() - t0, 2))
    status, denied = gateway(secret, "POST", "/chat/completions", {
        "model": "gx-max", "max_tokens": 4, "messages": [{"role": "user", "content": "hi"}]})
    rep.check("the key is refused for an alias it was not granted (gx-max)", status in (401, 403),
              status=status, error=str(denied.get("error", ""))[:120])
    probe = s.call("POST", "/api/keys/test", {"secret": secret, "model": "gx-mini"}, expect=200)
    rep.check("Control UI 'Test this key' performs a real request", probe.get("chat_status") == 200,
              result={k: v for k, v in probe.items() if k != "models"})
    s.call("POST", f"/api/keys/{created['id']}/revoke", {"confirm": False}, expect=400)
    s.call("POST", f"/api/keys/{created['id']}/revoke", {"confirm": True}, expect=200)
    time.sleep(2)
    status, after = gateway(secret, "GET", "/models")
    rep.check("the revoked key is refused", status in (400, 401, 403), status=status,
              error=str(after.get("error", ""))[:120])
    listing = s.call("GET", "/api/keys", expect=200)
    rep.check("the revoked key is gone from the list", not [k for k in listing["keys"] if k["name"] == name])
    secret = ""


# ---------------------------------------------------------------- library
def wait_job(s: Session, job_id: str, budget: float = 3600) -> dict:
    deadline = time.time() + budget
    phases = []
    while time.time() < deadline:
        j = s.call("GET", f"/api/media/jobs/{job_id}", expect=200)
        if not phases or phases[-1] != j["phase"]:
            phases.append(j["phase"])
        if j["phase"] in ("ready", "failed"):
            j["phases_seen"] = phases
            return j
        time.sleep(3)
    raise AssertionError("media job timed out")


def suite_library(s: Session, rep: Report) -> None:
    created: list[str] = []

    def job(body: dict, label: str) -> dict:
        j = s.call("POST", "/api/media/jobs", body, expect=202)
        done = wait_job(s, j["id"])
        rep.check(f"{label} job ready", done["phase"] == "ready", phases=done["phases_seen"],
                  error=done.get("error"), seconds=done["elapsed_seconds"], assets=done["assets"])
        created.extend(done["assets"])
        return done

    img1 = job({"kind": "t2i", "prompt": "TEST gx acceptance: a red vintage bicycle leaning on a blue wall",
                "size": "1024x1024", "seed": 101, "title": "TEST bicycle"}, "generate image #1")
    img2 = job({"kind": "t2i", "prompt": "TEST gx acceptance: a lighthouse at dawn, photorealistic",
                "size": "1024x1024", "seed": 202, "title": "TEST lighthouse"}, "generate image #2")
    a1 = img1["assets"][0]
    before = s.call("GET", f"/api/media/assets/{a1}", expect=200)
    edit = job({"kind": "edit", "source_id": a1, "prompt": "Make the bicycle bright green and the wall white.",
                "title": "TEST bicycle edited"}, "edit image")
    after = s.call("GET", f"/api/media/assets/{a1}", expect=200)
    rep.check("original preserved after edit", before["sha256"] == after["sha256"], sha=after["sha256"][:16])
    e1 = s.call("GET", f"/api/media/assets/{edit['assets'][0]}", expect=200)
    rep.check("edit is a new asset linked to its parent", e1["parent_id"] == a1 and e1["operation"] == "edit",
              parent=e1["parent_id"], ancestors=len(e1["ancestors"]))
    parent = s.call("GET", f"/api/media/assets/{a1}", expect=200)
    rep.check("parent lists the edit as a child", any(c["id"] == e1["id"] for c in parent["children"]))
    var = job({"kind": "variation", "source_id": img2["assets"][0], "title": "TEST lighthouse variation"}, "variation")
    t2v = job({"kind": "t2v", "prompt": "TEST gx acceptance: waves rolling onto a beach, slow camera pan",
               "seconds": 2, "title": "TEST waves"}, "generate video")
    i2v = job({"kind": "i2v", "source_id": img2["assets"][0], "prompt": "the lighthouse beam sweeps across the sky",
               "seconds": 2, "title": "TEST lighthouse animated"}, "image to video")
    v2v = job({"kind": "v2v", "source_id": t2v["assets"][0], "prompt": "make this scene take place at night",
               "seconds": 2, "title": "TEST waves at night"}, "edit video")
    for vid in (t2v["assets"][0], i2v["assets"][0], v2v["assets"][0]):
        a = s.call("GET", f"/api/media/assets/{vid}", expect=200)
        rep.check(f"video {vid} has real motion metadata", (a["frame_count"] or 0) >= 9 and
                  (a["distinct_frames"] or 0) >= (a["frame_count"] or 0) // 2,
                  frames=a["frame_count"], distinct=a["distinct_frames"], fps=a["fps"], duration=a["duration"])
        head = s.call("GET", a["url"], headers={"Range": "bytes=0-1023"})
        rep.check(f"video {vid} streams with range requests", isinstance(head, bytes) and len(head) == 1024)
    v2 = s.call("GET", f"/api/media/assets/{v2v['assets'][0]}", expect=200)
    rep.check("edited video links to its source video", v2["parent_id"] == t2v["assets"][0])
    upload = s.call("POST", "/api/media/upload", raw=s.call("GET", f"/api/media/assets/{a1}/file"),
                    ctype="image/png", headers={"X-Title": "TEST%20upload"}, expect=200)
    created.append(upload["id"])
    rep.check("upload registers a new asset", upload["operation"] == "upload" and upload["width"] == 1024)
    # --- library operations
    listing = s.call("GET", "/api/media/assets?q=TEST%20gx%20acceptance&limit=200", expect=200)
    rep.check("search finds the test assets", listing["total"] >= 4, total=listing["total"])
    vids = s.call("GET", "/api/media/assets?type=video&limit=200", expect=200)
    rep.check("type filter", all(i["type"] == "video" for i in vids["items"]) and vids["total"] >= 3)
    ops = s.call("GET", "/api/media/assets?operation=edit&limit=200", expect=200)
    rep.check("operation filter", all(i["operation"] == "edit" for i in ops["items"]) and ops["total"] >= 1)
    models = s.call("GET", "/api/media/assets?model=gx-video&limit=200", expect=200)
    rep.check("model filter", all(i["model_alias"] == "gx-video" for i in models["items"]))
    newest = s.call("GET", "/api/media/assets?sort=newest&limit=5", expect=200)["items"]
    oldest = s.call("GET", "/api/media/assets?sort=oldest&limit=5", expect=200)["items"]
    rep.check("sort newest/oldest", newest[0]["created_at"] >= newest[-1]["created_at"] and
              oldest[0]["created_at"] <= oldest[-1]["created_at"])
    fav = s.call("POST", f"/api/media/assets/{a1}", {"favourite": True}, expect=200)
    favs = s.call("GET", "/api/media/assets?favourite=1", expect=200)
    rep.check("favourite", fav["favourite"] and any(i["id"] == a1 for i in favs["items"]))
    unfav = s.call("POST", f"/api/media/assets/{a1}", {"favourite": False}, expect=200)
    rep.check("unfavourite", not unfav["favourite"])
    ren = s.call("POST", f"/api/media/assets/{a1}", {"title": "TEST bicycle renamed"}, expect=200)
    rep.check("rename", ren["title"] == "TEST bicycle renamed")
    dl = s.call("GET", f"/api/media/assets/{a1}/file?download=1")
    rep.check("download returns the PNG", isinstance(dl, bytes) and dl[:8] == b"\x89PNG\r\n\x1a\n", bytes=len(dl))
    thumb = s.call("GET", f"/api/media/assets/{t2v['assets'][0]}/thumbnail")
    rep.check("video thumbnail", isinstance(thumb, bytes) and thumb[:2] == b"\xff\xd8")
    zsel = [a1, e1["id"], t2v["assets"][0]]
    z = s.call("POST", "/api/media/zip", {"ids": zsel}, expect=200)
    blob = s.call("GET", z["url"])
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    rep.check("ZIP contains exactly the selection plus a manifest", len(names) == 4 and
              "gx-media-manifest.json" in names and not any(".." in n or n.startswith("/") for n in names),
              names=names)
    again = s.call("GET", z["url"])
    rep.check("ZIP link is single-use", isinstance(again, dict) and again.get("error"))
    # delete one, then several
    s.call("POST", "/api/media/delete", {"ids": [upload["id"]]}, expect=400)
    one = s.call("POST", "/api/media/delete", {"ids": [upload["id"]], "confirm": True}, expect=200)
    rep.check("delete one test asset", one["deleted"] == [upload["id"]])
    many = [var["assets"][0], i2v["assets"][0]]
    res = s.call("POST", "/api/media/delete", {"ids": many, "confirm": True}, expect=200)
    rep.check("multi-delete test assets", sorted(res["deleted"]) == sorted(many))
    gone = s.call("GET", f"/api/media/assets/{many[0]}")
    rep.check("deleted asset is gone", isinstance(gone, dict) and "error" in gone)
    rep.check("summary", True, kept=[x for x in created if x not in many and x != upload["id"]])


# ---------------------------------------------------------------- manager
TINY_REPO = "ggml-org/models"


def suite_manager(s: Session, rep: Report) -> None:
    res = s.call("GET", "/api/manager/search?q=tinyllama%20gguf&limit=5", expect=200)
    rep.check("Hugging Face search", len(res["results"]) > 0, first=res["results"][0]["repository"])
    by_id = s.call("POST", "/api/manager/lookup", {"ref": "HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive"}, expect=200)
    rep.check("repository id lookup with pinned revision", len(by_id["revision"]) == 40 and by_id["formats"] == ["gguf"],
              revision=by_id["revision"], runtimes=by_id["runtimes"], quant=by_id["quantization"][:3])
    by_url = s.call("POST", "/api/manager/lookup",
                    {"ref": "https://huggingface.co/kyaky/Qwen3.6-35B-A3B-Uncensored-NVFP4/tree/main"}, expect=200)
    rep.check("full URL lookup + metadata", by_url["repository"] == "kyaky/Qwen3.6-35B-A3B-Uncensored-NVFP4" and
              by_url["moe"] and by_url["vision"] and "vLLM (llama-swap)" in by_url["runtimes"],
              params=by_url["parameters"], size=by_url["size_bytes"], licence=by_url["licence"])
    # A known-gated repository, used purely as a fixture for gated detection.
    # It is NOT the gx-reason target (D-042); gx-reason is ungated.
    gated = s.call("POST", "/api/manager/lookup", {"ref": "iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070"}, expect=200)
    rep.check("gated status detected", bool(gated["gated"]), gated=gated["gated"], accessible=gated["accessible"])
    overlay = s.call("POST", "/api/manager/lookup",
                     {"ref": "pocharlies/deepseek-v4-flash-0731-uncensored-abliterated-refusal-directions"}, expect=200)
    rep.check("a refusal-direction overlay is not treated as a checkpoint", overlay["kind"] == "not_a_checkpoint")
    lora = s.call("POST", "/api/manager/lookup", {"ref": "rzgar/Wan2.2_LightX2V_4Step_Uncensored"}, expect=200)
    rep.check("an adapter is classified as a LoRA", lora["kind"] == "lora")
    tiny = s.call("POST", "/api/manager/lookup", {"ref": TINY_REPO}, expect=200)
    target_name = "gx-mm-acceptance-tiny"
    plan = s.call("POST", "/api/manager/plan", {
        "repository": tiny["repository"], "revision": tiny["revision"], "node": "node1", "category": "staging",
        "name": target_name, "include": "tinyllamas/stories15M-q4_0.gguf"}, expect=200)
    rep.check("plan: size and disk fit", plan["ok"] and plan["download_bytes"] > 0,
              bytes=plan["download_bytes"], free=plan["free_bytes"], warnings=plan["warnings"])
    job = s.call("POST", "/api/manager/stage", plan, expect=202)
    prog = []
    while True:
        j = s.call("GET", f"/api/manager/jobs/{job['id']}", expect=200)
        if j["progress"] is not None:
            prog.append(j["progress"])
        if j["state"] != "running":
            break
        time.sleep(2)
    rep.check("staging download + sha256 verification", j["state"] == "succeeded", progress=prog[-3:],
              result=j["result"], tail=j["output"][-2:])
    inv = s.call("GET", "/api/manager/inventory", expect=200)
    mine = [m for m in inv["installed"] if m["name"] == target_name]
    rep.check("installed inventory lists it with the pinned revision", bool(mine) and
              mine[0]["revision"] == tiny["revision"] and mine[0]["deletable"],
              entry={k: mine[0][k] for k in ("path", "size", "verified")} if mine else None)
    test = s.call("POST", "/api/manager/test", {"node": "node1", "path": f"/srv/models/staging/{target_name}",
                                                "runtime": "llama.cpp"}, expect=202)
    while True:
        j = s.call("GET", f"/api/manager/jobs/{test['id']}", expect=200)
        if j["state"] != "running":
            break
        time.sleep(2)
    # stories15M is a 15M-parameter toy: it produces text but cannot do arithmetic.
    rep.check("test-serve starts a temporary container and gets a real completion",
              bool(j["result"].get("answer")) and j["result"].get("load_seconds") is not None,
              result=j["result"], tail=j["output"][-2:])
    refused_assign = s.call("POST", "/api/manager/assign", {"alias": "gx-mini", "path": f"/srv/models/gguf/{target_name}"})
    rep.check("an alias is not assigned without a correct test answer",
              isinstance(refused_assign, dict) and "error" in refused_assign, error=refused_assign.get("error"))
    prod = s.call("POST", "/api/manager/assign", {"alias": "gx-mini", "dry_run": True,
                                                  "path": "/srv/models/gguf/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive"},
                  expect=202)
    while True:
        j = s.call("GET", f"/api/manager/jobs/{prod['id']}", expect=200)
        if j["state"] != "running":
            break
        time.sleep(1)
    rep.check("assignment preview (dry run) renders the binding change and writes nothing",
              j["state"] == "succeeded" and j["result"].get("changes"), changes=j["result"].get("changes"))
    refused = s.call("POST", "/api/manager/delete", {"node": "node1", "confirm": "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive",
                                                     "path": "/srv/models/gguf/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive"})
    rep.check("delete protection: the live gx-mini model cannot be deleted",
              isinstance(refused, dict) and "referenced" in str(refused.get("error", {}).get("message", "")),
              error=refused.get("error"))
    refused2 = s.call("POST", "/api/manager/delete", {"node": "node2", "confirm": "DeepSeek-V4-Flash-0731-NVFP4",
                                                      "path": "/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4"})
    rep.check("delete protection: an unaccepted rollback model cannot be deleted",
              isinstance(refused2, dict) and "referenced" in str(refused2.get("error", {}).get("message", "")))
    bad = s.call("POST", "/api/manager/delete", {"node": "node1", "confirm": "x", "path": "/etc/passwd"})
    rep.check("delete refuses paths outside the model folders", isinstance(bad, dict) and bad.get("error"))
    wrong = s.call("POST", "/api/manager/delete", {"node": "node1", "confirm": "nope",
                                                   "path": f"/srv/models/staging/{target_name}"})
    rep.check("delete needs the typed confirmation", isinstance(wrong, dict) and wrong.get("error"))
    d = s.call("POST", "/api/manager/delete", {"node": "node1", "confirm": target_name,
                                               "path": f"/srv/models/staging/{target_name}"}, expect=202)
    while True:
        j = s.call("GET", f"/api/manager/jobs/{d['id']}", expect=200)
        if j["state"] != "running":
            break
        time.sleep(1)
    rep.check("unused test model deleted", j["state"] == "succeeded" and
              not Path(f"/srv/models/staging/{target_name}").exists(), result=j["result"])


def main() -> int:
    suite = sys.argv[1] if len(sys.argv) > 1 else "keys"
    rep = Report(suite)
    s = Session()
    try:
        {"keys": suite_keys, "library": suite_library, "manager": suite_manager}[suite](s, rep)
    except Exception as exc:  # noqa: BLE001
        rep.check("suite completed", False, error=f"{type(exc).__name__}: {exc}"[:400])
    return rep.save()


if __name__ == "__main__":
    raise SystemExit(main())
