"""LiteLLM virtual keys for gateway clients (D-035).

The browser never sees the LiteLLM master key. This module calls LiteLLM's
key-management API server-side with the master key and returns:

* on creation: the new secret, ONCE, in the HTTP response only (never logged,
  never stored by the Control UI);
* in listings: LiteLLM's own masked key name (`sk-...abcd`), never the secret.

Keys are scoped to the public aliases the user chose. "Replace" creates a
new key with the same settings and then revokes the old one.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .util import HTTPError, bearer, http_json
from datetime import UTC

PUBLIC_ALIASES = ("gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto", "gx-image", "gx-video", "gx-music")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32,128}$")
EXPIRY = {"never": None, "1d": "1d", "7d": "7d", "30d": "30d", "90d": "90d", "365d": "365d"}


class KeyError_(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_request(body: dict) -> dict:
    name = body.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name.strip()):
        raise KeyError_("name: 1-63 characters of letters, digits, space, '.', '_' or '-'")
    models = body.get("models")
    if not isinstance(models, list) or not models:
        raise KeyError_("choose at least one allowed alias")
    bad = [m for m in models if m not in PUBLIC_ALIASES]
    if bad:
        raise KeyError_(f"unknown aliases: {', '.join(map(str, bad))}")
    expiry = body.get("expiry", "never")
    if expiry not in EXPIRY and not (isinstance(expiry, str) and re.fullmatch(r"[1-9][0-9]{0,3}d", expiry)):
        raise KeyError_(f"expiry must be one of {', '.join(EXPIRY)}")
    out: dict[str, Any] = {"name": name.strip(), "models": sorted(set(models), key=PUBLIC_ALIASES.index),
                           "expiry": expiry}
    for field, hi in (("rpm_limit", 100_000), ("tpm_limit", 100_000_000), ("max_parallel_requests", 1000)):
        value = body.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= hi:
            raise KeyError_(f"{field} must be a whole number between 1 and {hi}")
        out[field] = value
    return out


class KeyManager:
    def __init__(self, base: str, master_key_fn) -> None:
        self.base = base.rstrip("/")
        self._master = master_key_fn

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 20) -> Any:
        key = self._master()
        if not key:
            raise KeyError_("the LiteLLM master key is not available to the Control UI", 503)
        try:
            status, data = http_json(method, self.base + path, body=body, headers=bearer(key), timeout=timeout)
        except HTTPError as exc:
            raise KeyError_(f"LiteLLM unreachable: {exc.message}", 502) from None
        if not 200 <= status < 300:
            detail = data.get("error") if isinstance(data, dict) else data
            if isinstance(detail, dict):
                detail = detail.get("message") or detail
            raise KeyError_(f"LiteLLM refused ({status}): {str(detail)[:300]}", 502)
        return data

    # ------------------------------------------------------------------ read
    def list(self) -> list[dict]:
        data = self._call("GET", "/key/list?return_full_object=true&size=100")  # LiteLLM caps size at 100
        out = []
        for k in data.get("keys", []) if isinstance(data, dict) else []:
            if not isinstance(k, dict):
                continue
            expires = k.get("expires")
            out.append({
                "id": k.get("token"),
                "name": k.get("key_alias") or (k.get("metadata") or {}).get("name") or "(unnamed)",
                "masked": k.get("key_name") or "sk-…",
                "models": k.get("models") or [],
                "created": k.get("created_at"),
                "expires": expires,
                "last_used": k.get("last_active"),
                "spend": k.get("spend"),
                "rpm_limit": k.get("rpm_limit"),
                "tpm_limit": k.get("tpm_limit"),
                "max_parallel_requests": k.get("max_parallel_requests"),
                "status": "blocked" if k.get("blocked") else ("expired" if _expired(expires) else "active"),
                "managed_by_ui": (k.get("metadata") or {}).get("created_by") == "gx-control-ui",
            })
        out.sort(key=lambda r: r.get("created") or "", reverse=True)
        return out

    def _find(self, key_id: str) -> dict:
        if not isinstance(key_id, str) or not TOKEN_RE.match(key_id):
            raise KeyError_("invalid key id")
        for k in self.list():
            if k["id"] == key_id:
                return k
        raise KeyError_("no such key", 404)

    # ----------------------------------------------------------------- write
    def create(self, body: dict, *, user: str) -> dict:
        req = validate_request(body)
        payload: dict[str, Any] = {
            "key_alias": req["name"],
            "models": req["models"],
            "metadata": {"created_by": "gx-control-ui", "name": req["name"], "ui_user": user,
                         "created_at": int(time.time())},
        }
        duration = EXPIRY.get(req["expiry"], req["expiry"])
        if duration:
            payload["duration"] = duration
        for field in ("rpm_limit", "tpm_limit", "max_parallel_requests"):
            if field in req:
                payload[field] = req[field]
        data = self._call("POST", "/key/generate", payload)
        secret = data.get("key") if isinstance(data, dict) else None
        if not isinstance(secret, str) or not secret:
            raise KeyError_("LiteLLM did not return a key", 502)
        return {
            "secret": secret,
            "id": data.get("token") or data.get("token_id"),
            "name": req["name"],
            "models": req["models"],
            "expires": data.get("expires"),
            "masked": _mask(secret),
            "note": "Copy the key now. It is not stored by the Control UI and cannot be shown again.",
        }

    def revoke(self, key_id: str) -> dict:
        k = self._find(key_id)
        self._call("POST", "/key/delete", {"keys": [key_id]})
        return {"revoked": key_id, "name": k["name"]}

    def replace(self, key_id: str, *, user: str) -> dict:
        """New secret, same name, aliases, expiry and limits; the old key stops working.

        LiteLLM requires unique key aliases, so the new key is created under a
        temporary alias, the old key is deleted, and the new key then takes the
        original name.
        """
        old = self._find(key_id)
        expiry = _remaining_days(old.get("expires"))
        if expiry == "expired":
            raise KeyError_("the key has already expired; create a new key instead")
        temp_name = f"{old['name'][:50]}-rotating"
        new = self.create({"name": temp_name, "models": old["models"] or list(PUBLIC_ALIASES),
                           "expiry": expiry,
                           **{f: old[f] for f in ("rpm_limit", "tpm_limit", "max_parallel_requests") if old.get(f)}},
                          user=user)
        self._call("POST", "/key/delete", {"keys": [key_id]})
        try:
            self._call("POST", "/key/update", {"key": new["id"], "key_alias": old["name"],
                                                "metadata": {"created_by": "gx-control-ui", "name": old["name"],
                                                             "ui_user": user, "replaced": key_id[:12],
                                                             "created_at": int(time.time())}})
            new["name"] = old["name"]
        except KeyError_:
            new["note"] = (f"{new['note']} The key kept the temporary name '{temp_name}'; rename it later.")
        new["replaced"] = key_id
        return new


def _mask(secret: str) -> str:
    return f"{secret[:3]}…{secret[-4:]}" if len(secret) > 10 else "…"


def _remaining_days(expires: Any) -> str:
    if not expires:
        return "never"
    from datetime import datetime

    try:
        when = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    except ValueError:
        return "never"
    seconds = (when - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return "expired"
    return f"{max(1, int(-(-seconds // 86400)))}d"


def _expired(expires: Any) -> bool:
    if not expires:
        return False
    try:
        from datetime import datetime

        when = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        return when < datetime.now(UTC)
    except ValueError:
        return False


def probe(gateway_base: str, secret: str, model: str = "gx-mini") -> dict:
    """Use a key for real: list models, then a short chat on `model`."""
    headers = bearer(secret)
    out: dict[str, Any] = {}
    try:
        status, data = http_json("GET", f"{gateway_base.rstrip('/')}/v1/models", headers=headers, timeout=15)
        out["models_status"] = status
        out["models"] = [m.get("id") for m in (data.get("data") or [])] if isinstance(data, dict) else []
        t0 = time.time()
        status, data = http_json("POST", f"{gateway_base.rstrip('/')}/v1/chat/completions", headers=headers,
                                 body={"model": model, "max_tokens": 16, "temperature": 0,
                                       "messages": [{"role": "user", "content": "Reply with the word: pong"}]},
                                 timeout=120)
        out["chat_status"] = status
        out["chat_seconds"] = round(time.time() - t0, 2)
        if isinstance(data, dict) and data.get("choices"):
            out["chat_answer"] = (data["choices"][0].get("message") or {}).get("content", "")[:80]
        elif isinstance(data, dict):
            err = data.get("error")
            out["chat_error"] = str(err.get("message") if isinstance(err, dict) else err)[:200]
    except HTTPError as exc:
        out["error"] = exc.message
    return out
