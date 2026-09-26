"""Shared helpers for the Open WebUI <-> Computer tooling on gx10-01.

Secrets are handled in memory only: LiteLLM keys are read from the secrets store,
session tokens are minted inside the owning container and passed through a pipe,
and nothing returned by these helpers contains a credential unless the name says so.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SECRETS = Path(os.environ.get("GX_SECRETS_DIR", "/srv/projects/gx-cluster/secrets"))
OWUI_CONTAINER = "open-webui"
CPTR_CONTAINER = "gx-computer"
CPTR_PY = "/home/cptr/.venv/bin/python"
CPTR_BASE = "http://127.0.0.1:8000"           # Computer, loopback-published
COMPUTER_URL = CPTR_BASE + "/v1"                # as seen by host-networked Open WebUI
LITELLM_HOST = "http://127.0.0.1:4000"          # LiteLLM admin API from the host
LITELLM_FROM_OWUI = "http://127.0.0.1:4000/v1"  # Open WebUI is host-networked
LITELLM_FROM_CPTR = "http://gx-litellm:4000/v1"  # Computer joins gx_gateway
WORKSPACE = "/projects/gx-cluster"
HOST_WORKSPACE = str(REPO)

sys.path.insert(0, str(REPO / "legenex" / "control-ui"))
from gx_control_ui.api_keys import PUBLIC_ALIASES, KeyManager  # noqa: E402

# Open WebUI forwards these per request; Computer maps them to its own chat/message tree
# and routes title/tag/follow-up tasks (X-OpenWebUI-Task) to the plain model.
OWUI_COMPUTER_HEADERS = {
    "X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
    "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
    "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
    "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
    "X-OpenWebUI-Task": "{{TASK}}",
}


class ToolError(RuntimeError):
    pass


def run(args: list[str], stdin: str | None = None, timeout: int = 900) -> str:
    r = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise ToolError(f"{' '.join(args[:4])} failed: {r.stderr.strip()[-600:]}")
    return r.stdout


def _copy_in(container: str, name: str) -> str:
    dst = f"/tmp/gx-{uuid.uuid4().hex[:8]}-{name}"
    run(["docker", "cp", str(HERE / name), f"{container}:{dst}"])
    return dst


# ---------------------------------------------------------------- Open WebUI
def owui(ops: list[dict], email: str | None = None, require_admin: bool = True) -> list[dict]:
    """Run admin API ops inside open-webui (see owui_api.py). Output is redacted."""
    path = _copy_in(OWUI_CONTAINER, "owui_api.py")
    try:
        out = run(["docker", "exec", "-i", OWUI_CONTAINER, "sh", "-c",
                   f'cd /app/backend && WEBUI_SECRET_KEY="$(cat .webui_secret_key)" exec python3 {path}'],
                  json.dumps({"email": email, "require_admin": require_admin, "ops": ops}), timeout=1800)
    finally:
        subprocess.run(["docker", "exec", OWUI_CONTAINER, "rm", "-f", path], capture_output=True)
    return json.loads(out.strip().splitlines()[-1])


def owui_sql(query: str, params: tuple = ()) -> list[list]:
    """Read-only query against Open WebUI's SQLite (mode=ro)."""
    code = ("import sqlite3,json,sys;c=sqlite3.connect('file:/app/backend/data/webui.db?mode=ro',uri=True);"
            "q,p=json.load(sys.stdin);print(json.dumps([list(r) for r in c.execute(q,p)]))")
    return json.loads(run(["docker", "exec", "-i", OWUI_CONTAINER, "python", "-c", code], json.dumps([query, list(params)])))


def canonical_identity(email: str | None = None) -> dict:
    """The Open WebUI account that is the source of truth (the only admin, unless an email is given)."""
    if email:
        rows = owui_sql("select email, name, role, profile_image_url from user where email = ?", (email,))
    else:
        rows = owui_sql("select email, name, role, profile_image_url from user where role = 'admin'")
    if len(rows) != 1:
        raise ToolError(f"expected exactly one canonical Open WebUI account, found {len(rows)}; pass --email")
    e, name, role, img = rows[0]
    m = re.match(r"^data:(image/[a-z+.-]+);base64,(.*)$", img or "", re.S)
    return {"email": e, "name": name, "role": role,
            "avatar_mime": m[1] if m else None, "avatar_b64": m[2] if m else None}


# ---------------------------------------------------------------- Computer
class Computer:
    """Computer HTTP API as an existing user, via a short-lived session minted in-container."""

    def __init__(self, username: str, ttl: int = 900) -> None:
        path = _copy_in(CPTR_CONTAINER, "cptr_session.py")
        try:
            out = json.loads(run(["docker", "exec", "-i", "-w", "/tmp", CPTR_CONTAINER, CPTR_PY, path],
                                 json.dumps({"username": username, "ttl": ttl})))
        finally:
            subprocess.run(["docker", "exec", CPTR_CONTAINER, "rm", "-f", path], capture_output=True)
        self.username, self.user_id, self.role = username, out["user_id"], out["role"]
        self._token = out["token"]

    @property
    def secret_token(self) -> str:
        return self._token

    def call(self, method: str, path: str, body=None, *, raw: bytes | None = None,
             ctype: str = "application/json", bearer: str | None = None, cookie: bool = True, timeout: int = 120):
        headers = {}
        if cookie and bearer is None:
            headers["Cookie"] = "cptr_session=" + self._token
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if data is not None:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(CPTR_BASE + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return r.status, (json.loads(b) if b else None)
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b)
            except ValueError:
                return e.code, b.decode(errors="replace")[:300]

    @staticmethod
    def q(value: str) -> str:
        return urllib.parse.quote(value, safe="")


def cptr_sql(query: str, params: tuple = ()) -> list[list]:
    """Read-only query against Computer's SQLite (mode=ro)."""
    code = ("import sqlite3,json,sys;c=sqlite3.connect('file:/data/app.db?mode=ro',uri=True);"
            "q,p=json.load(sys.stdin);print(json.dumps([list(r) for r in c.execute(q,p)]))")
    return json.loads(run(["docker", "exec", "-i", CPTR_CONTAINER, CPTR_PY, "-c", code], json.dumps([query, list(params)])))


# ---------------------------------------------------------------- LiteLLM
def _master_key() -> str | None:
    env = REPO / "legenex" / "gateway" / ".env"
    for line in env.read_text().splitlines():
        m = re.match(r"^LITELLM_MASTER_KEY=(.*)$", line.strip())
        if m:
            return m[1].strip().strip("'\"")
    return None


def ensure_litellm_key(name: str, store: Path) -> tuple[str, str]:
    """Return (action, secret) for a LiteLLM virtual key limited to the public aliases.

    Created once through the Control Center KeyManager (same metadata as keys made in
    Control Center -> API Keys) and kept 0600 in the secrets store.
    """
    km = KeyManager(LITELLM_HOST, _master_key)
    if store.exists():
        return "exists", store.read_text().strip()
    if any(k["name"] == name for k in km.list()):
        raise ToolError(f"LiteLLM key '{name}' exists but {store} is missing; revoke it in Control Center first")
    created = km.create({"name": name, "models": list(PUBLIC_ALIASES), "expiry": "never"}, user="gx-computer-tools")
    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(created["secret"])
    return "created", created["secret"]
