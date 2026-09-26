"""Open WebUI admin API helper. Runs INSIDE the open-webui container (copied in by provision.py).

Mints a 10-minute session JWT for an existing user, the same token /api/v1/auths/signin issues
(HS256, WEBUI_SECRET_KEY, claims id/iat/exp/jti), calls the local API on 127.0.0.1:3000 and
prints JSON with every credential redacted. The secret never leaves the container.

stdin: {"email": <user email or null for "the only admin">, "require_admin": true, "ops": [...]}
  {"call": [METHOD, PATH, BODY|null], "timeout": s}
  {"openai_upsert": {"url": ..., "key": ..., "config": {...}|null, "config_if_new": {...}}}
      read-modify-write of the OpenAI connection list done in-process, so the other
      connections' keys are never exported. config null keeps the existing settings.
  {"openai_has": url}
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:3000"
DB = "file:/app/backend/data/webui.db?mode=ro"
# Field names whose string values are credentials. `auth_type` and ENABLE_* flags are not.
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTHORIZATION|COOKIE|CREDENTIAL)", re.I)
SAFE_NAMES = {"auth_type"}
SECRET_VALUE = re.compile(r"(Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{10,}(\.[A-Za-z0-9_-]+){0,2}"
                          r"|hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xai-[A-Za-z0-9]{20,})")


def redact(o, name=""):
    """Recursively redact credentials by field name and by token shape. Returns a copy."""
    if isinstance(o, dict):
        return {k: redact(v, k) for k, v in o.items()}
    if isinstance(o, list):
        if name and SECRET_NAME.search(name) and name not in SAFE_NAMES and any(isinstance(v, str) and v for v in o):
            return "<redacted>"
        return [redact(v, name) for v in o]
    if isinstance(o, str):
        if o and name and SECRET_NAME.search(name) and name not in SAFE_NAMES and not name.upper().startswith("ENABLE_"):
            return "<redacted>"
        return SECRET_VALUE.sub("<redacted>", o)
    return o


def user_row(email, require_admin):
    c = sqlite3.connect(DB, uri=True)
    if email:
        rows = c.execute("select id, role, email from user where email=?", (email,)).fetchall()
    else:
        rows = c.execute("select id, role, email from user where role='admin'").fetchall()
        if len(rows) != 1:
            sys.exit(f"expected exactly one Open WebUI admin, found {len(rows)}; pass an email")
    if not rows:
        sys.exit("user not found")
    if require_admin and rows[0][1] != "admin":
        sys.exit("user is not an admin")
    return rows[0]


def token(user_id):
    import jwt  # PyJWT ships with Open WebUI

    now = int(time.time())
    return jwt.encode({"id": user_id, "iat": now, "exp": now + 600, "jti": str(uuid.uuid4())},
                      os.environ["WEBUI_SECRET_KEY"], algorithm="HS256")


def call(tok, method, path, body=None, timeout=600):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")[:500]


def openai_config(tok):
    st, cfg = call(tok, "GET", "/openai/config")
    if st != 200:
        sys.exit(f"GET /openai/config failed: {st}")
    return cfg


def openai_upsert(tok, spec):
    cfg = openai_config(tok)
    urls = list(cfg["OPENAI_API_BASE_URLS"])
    keys = (list(cfg["OPENAI_API_KEYS"]) + [""] * len(urls))[:len(urls)]   # the server pairs them by index
    confs = dict(cfg["OPENAI_API_CONFIGS"])
    if spec["url"] in urls:
        idx, action = urls.index(spec["url"]), "updated"
        keys[idx] = spec["key"]
        if spec.get("config") is not None:
            confs[str(idx)] = spec["config"]
    else:
        urls.append(spec["url"])
        keys.append(spec["key"])
        idx, action = len(urls) - 1, "added"
        confs[str(idx)] = spec.get("config") or spec.get("config_if_new") or {"enable": True}
    body = {"ENABLE_OPENAI_API": cfg["ENABLE_OPENAI_API"], "OPENAI_API_BASE_URLS": urls,
            "OPENAI_API_KEYS": keys, "OPENAI_API_CONFIGS": confs}
    st, out = call(tok, "POST", "/openai/config/update", body)
    return {"action": action, "index": idx, "status": st, "result": redact(out)}


def main():
    req = json.load(sys.stdin)
    uid, role, email = user_row(req.get("email"), req.get("require_admin", True))
    tok = token(uid)
    results = [{"op": "identity", "user_id": uid, "role": role}]
    for op in req["ops"]:
        if "call" in op:
            m, p, b = (list(op["call"]) + [None])[:3]
            st, out = call(tok, m, p, b, timeout=op.get("timeout", 600))
            results.append({"op": f"{m} {p}", "status": st, "body": redact(out)})
        elif "openai_upsert" in op:
            results.append({"op": "openai_upsert", **openai_upsert(tok, op["openai_upsert"])})
        elif "openai_has" in op:
            results.append({"op": "openai_has", "present": op["openai_has"] in openai_config(tok)["OPENAI_API_BASE_URLS"]})
    print(json.dumps(results, default=str))


if __name__ == "__main__":
    main()
