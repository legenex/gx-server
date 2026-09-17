#!/usr/bin/env python3
"""Production Open WebUI acceptance for the GX aliases (D-038).

Runs against the REAL `open-webui` container behind https://chat.legenex.co,
never a throw-away instance:

1. creates a disposable `user`-role account inside Open WebUI (its own
   password hashing), grants it read access to the gx-* entries only;
2. signs in through the public URL (proves chat.legenex.co is this database:
   the account exists nowhere else);
3. lists models and asks the identity questions through
   /api/chat/completions (Open WebUI applies the entry's system prompt and
   calls its LiteLLM connection);
4. correlates every answer with the gateway (LiteLLM spend log: key hash,
   model group, api_base, token counts), llama-swap's request log and the
   gx-mini llama-server slot log;
5. runs direct LiteLLM controls with and without the identity prompt;
6. removes the grants and the account (and with it the test chats), and
   verifies nothing is left.

Secrets (the account password, the gateway master key, Open WebUI's secret)
are generated or read at run time and never printed or stored.

    python3 legenex/tests/owui_identity_acceptance.py [--aliases gx-mini,gx-fast,gx-auto] [--out DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import traceback
from collections import Counter
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PUBLIC = "https://chat.legenex.co"
GATEWAY = "http://127.0.0.1:4000"
QUESTIONS = [
    "What model are you?",
    "What is your full underlying model name?",
    "Are you HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive?",
    "How many parameters is your underlying base model?",
    "What runtime are you served through?",
    "What context length are you configured to serve per request?",
]
FULL = "HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive"

_IN_CONTAINER = r'''
import asyncio, json, sys
p = json.load(sys.stdin)
async def main():
    from open_webui.models.auths import Auths
    from open_webui.models.users import Users
    from open_webui.models.chats import Chats
    from open_webui.models.access_grants import AccessGrants
    from open_webui.utils.auth import get_password_hash
    op = p["op"]
    if op == "create":
        if await Users.get_user_by_email(p["email"]):
            raise SystemExit("test account already exists")
        user = await Auths.insert_new_auth(p["email"], await get_password_hash(p["password"]), p["name"],
                                           role="user")
        for mid in p["models"]:
            await AccessGrants.grant_access("model", mid, "user", user.id, "read")
        return {"user_id": user.id}
    if op == "find":
        found = [u.id for u in (await Users.get_users()).get("users", [])
                 if str(u.email).startswith("gx-acceptance-") and str(u.email).endswith("@example.invalid")]
        return {"user_ids": found}
    if op == "cleanup":
        uid = p["user_id"]
        revoked = [mid for mid in p["models"]
                   if await AccessGrants.revoke_access("model", mid, "user", uid, "read")]
        listing = await Chats.get_chats_by_user_id(uid)
        items = getattr(listing, "items", listing)
        chats = len(items) if isinstance(items, list) else getattr(listing, "total", None)
        deleted = await Auths.delete_auth_by_id(uid)
        left = await Users.get_user_by_id(uid)
        grants_left = 0
        for mid in p["models"]:
            grants_left += sum(1 for g in await AccessGrants.get_grants_by_resource("model", mid)
                               if g.principal_id == uid)
        return {"revoked": revoked, "chats_before_delete": chats, "deleted": deleted,
                "user_left": left is not None, "grants_left": grants_left}
    raise SystemExit("unknown op")
print(json.dumps(asyncio.run(main())))
'''


def owui(payload: dict) -> dict:
    wrapper = ('cd /app/backend && WEBUI_SECRET_KEY="$(cat .webui_secret_key)" GLOBAL_LOG_LEVEL=ERROR '
               'exec python3 -c "$1"')
    res = subprocess.run(["docker", "exec", "-i", "open-webui", "sh", "-c", wrapper, "acceptance", _IN_CONTAINER],
                         input=json.dumps(payload), capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"open-webui: {res.stderr.strip()[-400:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


def http(method: str, url: str, body: dict | None = None, token: str | None = None, timeout: float = 600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "gx-acceptance/1",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}"), dict(exc.headers)
        except ValueError:
            return exc.code, {"raw": raw[:300].decode("utf-8", "replace")}, dict(exc.headers)


def env_value(name: str) -> str:
    for line in (REPO / "legenex/gateway/.env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError(f"{name} missing from the gateway .env")


def owui_gateway_key_hash() -> str:
    script = ("import sqlite3,json,hashlib;c=sqlite3.connect('file:/app/backend/data/webui.db?mode=ro',uri=True);"
              "u=json.loads(c.execute(\"select value from config where key='openai.api_base_urls'\").fetchone()[0]);"
              "k=json.loads(c.execute(\"select value from config where key='openai.api_keys'\").fetchone()[0]);"
              "print(json.dumps([[a,hashlib.sha256(b.encode()).hexdigest()] for a,b in zip(u,k)]))")
    out = subprocess.run(["docker", "exec", "open-webui", "python3", "-c", script], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    pairs = json.loads(out)
    gw = [h for url, h in pairs if ":4000" in url]
    return gw[0] if gw else ""


def spend_rows(since: dt.datetime, model_group: str) -> list[dict]:
    q = ("select request_id, api_key, model_group, api_base, prompt_tokens, completion_tokens, "
         "to_char(\"startTime\", 'YYYY-MM-DD\"T\"HH24:MI:SS.MS') from \"LiteLLM_SpendLogs\" "
         f"where \"startTime\" >= '{since.strftime('%Y-%m-%d %H:%M:%S')}' and model_group = '{model_group}' "
         "order by \"startTime\"")
    out = subprocess.run(["docker", "exec", "gx-litellm-db", "psql", "-U", "litellm", "-d", "litellm", "-At", "-F",
                          "\t", "-c", q], capture_output=True, text=True, timeout=30, check=True).stdout
    rows = []
    for line in out.splitlines():
        rid, key, group, base, pt, ct, start = line.split("\t")
        rows.append({"request_id": rid, "api_key_sha256": key, "model_group": group, "api_base": base,
                     "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0), "start_utc": start})
    return rows


def docker_log(container: str, since: str, pattern: str) -> list[str]:
    res = subprocess.run(["docker", "logs", "--timestamps", "--since", since, container], capture_output=True,
                         text=True, timeout=30)
    return [line[:220] for line in (res.stdout + res.stderr).splitlines() if re.search(pattern, line)]


def text_of(body: dict) -> str:
    try:
        msg = body["choices"][0]["message"]
        return (msg.get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def judge(i: int, answer: str) -> list[str]:
    """Problems with an identity answer (empty = acceptable)."""
    a = answer.lower()
    bad = []
    if re.search(r"\b(7|14|32|72|110|235|397)\s*b\b|\b(72|110|235)[- ]?billion", a):
        bad.append("claims a larger model size")
    if re.search(r"\bgpt|\bclaude|\bgrok|\bgemini|chatgpt", a) and "not" not in a and "don't" not in a:
        bad.append("claims another product")
    if re.search(r"full[- ]precision", a) and not re.search(r"not (a )?full[- ]precision|not full", a):
        bad.append("claims full-precision weights")
    if re.search(r"\bi(?:'m| am) (?:an? |the )?official\b", a):
        bad.append("claims to be an official release")
    need = {0: ["gx-mini"], 1: [FULL.lower()], 2: ["yes"], 3: ["4"], 4: ["llama.cpp"],
            5: ["65,536", "65536", "65 536", "65k", "64k"]}[i]
    if not any(n in a for n in need):
        bad.append(f"missing expected fact ({' / '.join(need)})")
    if i == 3 and not re.search(r"\b4\s*(b\b|billion)", a):
        bad.append("base size is not stated as 4B")
    return bad


def _checks(args, report: dict, check, email: str, password: str, models: list[str]) -> None:
    status, body, _ = http("POST", f"{PUBLIC}/api/v1/auths/signin", {"email": email, "password": password})
    token = body.get("token") if status == 200 else None
    check("public sign-in at chat.legenex.co reaches the production database", bool(token), status=status)
    if not token:
        return
    status, ver, _ = http("GET", f"{PUBLIC}/api/version")
    check("chat.legenex.co serves Open WebUI 0.11.3", status == 200 and ver.get("version") == "0.11.3",
          version=ver.get("version"))
    status, listing, _ = http("GET", f"{PUBLIC}/api/models", token=token)
    data = listing.get("data", []) if isinstance(listing, dict) else []
    seen = {m["id"]: m for m in data}
    report["models_visible"] = sorted(seen)
    for alias in models:
        m = seen.get(alias) or {}
        info = m.get("info") or {}
        desc = ((info.get("meta") or {}).get("description")) or ""
        check(f"{alias} visible to the test account with its gx-cluster entry",
              alias in seen and m.get("name") == alias and bool(desc),
              entry_name=m.get("name"), description=desc[:120])
    check("no retired or foreign models are offered", set(seen) <= set(models), visible=sorted(seen))

    key_hash = owui_gateway_key_hash()
    report["owui_gateway_key_sha256_prefix"] = key_hash[:12]
    master = env_value("LITELLM_MASTER_KEY")

    # ---- gx-mini: the six questions, one conversation, then each one fresh
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)
    since_docker = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    convo: list[dict] = []
    transcript = []
    for i, q in enumerate(QUESTIONS):
        convo.append({"role": "user", "content": q})
        t0 = time.time()
        status, body, _ = http("POST", f"{PUBLIC}/api/chat/completions", token=token,
                               body={"model": "gx-mini", "messages": convo, "stream": False})
        answer = text_of(body)
        convo.append({"role": "assistant", "content": answer})
        usage = body.get("usage") or {}
        problems = judge(i, answer) if status == 200 else [f"HTTP {status}"]
        transcript.append({"mode": "conversation", "q": q, "a": answer, "status": status,
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "completion_tokens": usage.get("completion_tokens"),
                           "seconds": round(time.time() - t0, 2), "problems": problems})
        check(f"gx-mini conversation Q{i + 1}: {q}", not problems, answer=answer[:220], problems=problems)
    for i, q in enumerate(QUESTIONS):
        status, body, _ = http("POST", f"{PUBLIC}/api/chat/completions", token=token,
                               body={"model": "gx-mini", "messages": [{"role": "user", "content": q}],
                                     "stream": False})
        answer = text_of(body)
        usage = body.get("usage") or {}
        problems = judge(i, answer) if status == 200 else [f"HTTP {status}"]
        transcript.append({"mode": "fresh", "q": q, "a": answer, "status": status,
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "completion_tokens": usage.get("completion_tokens"), "problems": problems})
        check(f"gx-mini fresh chat Q{i + 1}: {q}", not problems, answer=answer[:220], problems=problems)
    report["gx_mini_transcript"] = transcript

    # ---- correlation: gateway, llama-swap, llama-server
    # LiteLLM writes its spend log in batches: wait (bounded) for all rows
    deadline = time.time() + 120
    while True:
        rows = spend_rows(since, "gx-mini")
        ours = [r for r in rows if r["api_key_sha256"] == key_hash]
        if len(ours) >= len(transcript) or time.time() > deadline:
            break
        time.sleep(5)
    # Open WebUI shares this key with other users of the same instance (and Kilo Code),
    # so a request is matched by its exact (prompt, completion) token pair.
    mine = Counter((t["prompt_tokens"], t["completion_tokens"]) for t in transcript)
    matched, others = [], []
    for r in ours:
        pair = (r["prompt_tokens"], r["completion_tokens"])
        if mine[pair] > 0:
            mine[pair] -= 1
            matched.append(r)
        else:
            others.append(r)
    report["litellm_spend_rows"] = matched
    report["other_traffic_same_key"] = [{k: r[k] for k in ("start_utc", "prompt_tokens", "completion_tokens")}
                                        for r in others]
    check("every Open WebUI test request is in the LiteLLM log under Open WebUI's key, with the exact token "
          "counts Open WebUI returned", len(matched) == len(transcript) and not +mine,
          matched=len(matched), expected=len(transcript), unmatched=list((+mine).elements()),
          concurrent_other_requests=len(others))
    check("LiteLLM sent them to node-1 llama-swap",
          bool(matched) and all(r["api_base"].rstrip("/") == "http://gx-llama-swap-node01:8080/v1"
                                for r in matched),
          api_base=sorted({r["api_base"] for r in matched}), request_ids=[r["request_id"] for r in matched][:3])
    tokens_gw = sorted((r["prompt_tokens"], r["completion_tokens"]) for r in matched)
    swap = docker_log("gx-llama-swap-node01", since_docker, r'POST /v1/chat/completions')
    report["llama_swap_lines"] = swap
    check("llama-swap node01 proxied them (from the gx-litellm container)",
          len([s for s in swap if "172.21.0.4" in s]) >= len(transcript), lines=len(swap))
    slots = docker_log("gx-mini", since_docker, r"stop processing: n_tokens")
    report["gx_mini_slot_lines"] = slots
    totals = sorted(int(m.group(1)) for m in (re.search(r"n_tokens = (\d+)", s) for s in slots) if m)
    expect = sorted(p + c for p, c in tokens_gw)
    near = all(any(abs(t - e) <= 2 for t in totals) for e in expect)
    check("the gx-mini llama-server processed contexts of exactly those sizes", near and len(totals) >= len(expect),
          server_contexts=totals, gateway_totals=expect)
    props = json.loads(subprocess.run(["curl", "-s", "127.0.0.1:19001/props"], capture_output=True, text=True,
                                      timeout=10).stdout)
    check("the serving process is the verified HauhauCS GGUF with 65536-token slots",
          props.get("model_path", "").endswith("Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf")
          and props.get("default_generation_settings", {}).get("n_ctx") == 65536,
          model_path=props.get("model_path"), n_ctx=props.get("default_generation_settings", {}).get("n_ctx"),
          modalities=props.get("modalities"))

    # ---- controls: direct LiteLLM with the same prompt, and without any prompt
    # the listing hides system prompts from non-owners: read the stored one (read-only)
    system = json.loads(subprocess.run(
        ["docker", "exec", "open-webui", "python3", "-c",
         "import sqlite3,json;c=sqlite3.connect('file:/app/backend/data/webui.db?mode=ro',uri=True);"
         "print(json.dumps(json.loads(c.execute(\"select params from model where id='gx-mini'\")"
         ".fetchone()[0]).get('system')))"], capture_output=True, text=True, timeout=30, check=True).stdout)
    check("the stored gx-mini entry carries the identity prompt", bool(system) and FULL in system,
          prompt_chars=len(system or ""))
    ctl = []
    for label, msgs in (("with the identity prompt", [{"role": "system", "content": system}]),
                        ("without any system prompt", [])):
        status, body, _ = http("POST", f"{GATEWAY}/v1/chat/completions", token=master,
                               body={"model": "gx-mini", "max_tokens": 200,
                                     "messages": msgs + [{"role": "user", "content": QUESTIONS[1]}]})
        ctl.append({"control": label, "status": status, "answer": text_of(body)})
    report["direct_litellm_controls"] = ctl
    check("direct LiteLLM control with the prompt names the HauhauCS model",
          FULL.lower() in ctl[0]["answer"].lower(), answer=ctl[0]["answer"][:200])
    check("direct LiteLLM control without a prompt answered (model self-description, informational)",
          ctl[1]["status"] == 200, answer=ctl[1]["answer"][:200])

    # ---- other aliases through the production path
    for alias in [a for a in args.aliases.split(",") if a and a != "gx-mini"]:
        t0 = time.time()
        status, body, headers = http("POST", f"{PUBLIC}/api/chat/completions", token=token, timeout=1200,
                                     body={"model": alias, "stream": False, "messages": [
                                         {"role": "user", "content": "What is 17 + 25? Reply with the number only."}]})
        answer = text_of(body)
        check(f"{alias} answers through production Open WebUI", status == 200 and "42" in answer,
              status=status, answer=answer[:80], seconds=round(time.time() - t0, 1))
        status, body, _ = http("POST", f"{PUBLIC}/api/chat/completions", token=token, timeout=1200,
                               body={"model": alias, "stream": False,
                                     "messages": [{"role": "user", "content": "What model are you?"}]})
        answer = text_of(body)
        want = "gx-auto" if alias == "gx-auto" else alias
        check(f"{alias} identifies as {want} through production Open WebUI",
              status == 200 and want in answer.lower(), answer=answer[:220])
        report.setdefault("other_aliases", []).append({"alias": alias, "identity_answer": answer})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aliases", default="gx-mini,gx-fast,gx-auto")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-account", action="store_true", help="debug only; the account must be removed later")
    args = ap.parse_args()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out or f"/srv/logs/acceptance/owui-identity-{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"run": run_id, "target": PUBLIC, "container": "open-webui", "checks": []}

    def check(label: str, ok: bool, **detail) -> None:
        report["checks"].append({"check": label, "ok": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + label + (f"  {json.dumps(detail)[:300]}" if detail else ""), flush=True)

    email = f"gx-acceptance-{run_id.lower()}@example.invalid"
    password = secrets.token_urlsafe(24)
    models = ["gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto"]
    created = owui({"op": "create", "email": email, "password": password, "name": "GX acceptance (temporary)",
                    "models": models})
    uid = created["user_id"]
    report["account"] = {"id": uid, "email": email, "role": "user"}
    crashed = None
    try:
        _checks(args, report, check, email, password, models)
    except Exception as exc:  # noqa: BLE001 - a crash is a failed run, never a pass
        crashed = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        if args.keep_account:
            print(f"KEEPING test account {uid} (remove it!)")
        else:
            cleaned = owui({"op": "cleanup", "user_id": uid, "models": models})
            report["cleanup"] = cleaned
            check("test account, its chats and its grants are gone",
                  cleaned["deleted"] and not cleaned["user_left"] and cleaned["grants_left"] == 0, **cleaned)
        report["crashed"] = crashed
        report["passed"] = crashed is None and all(c["ok"] for c in report["checks"])
        (out_dir / "owui-identity-acceptance.json").write_text(json.dumps(report, indent=2))
        text = json.dumps(report)
        assert password not in text and "sk-" not in text, "secret material in the report"
        print(f"\n{'ALL PASS' if report['passed'] else 'FAILURES'}: "
              f"{sum(c['ok'] for c in report['checks'])}/{len(report['checks'])} -> {out_dir}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
