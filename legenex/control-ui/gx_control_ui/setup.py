"""Client setup and Connections page: Kilo Code, Open WebUI, OpenAI clients.

V4.1: the public surface is TWO aliases — gx-max (explicit DeepSeek V4.1
Flash, uncensored) and gx-auto (profile/reasoning auto-selection). All
gx-mini/gx-code/gx-fast/gx-reason material is retired with their models.

The Connections page retrieves the live gateway key server-side from
legenex/gateway/.env (LITELLM_MASTER_KEY). It is masked by default, revealed
only to an authenticated admin session, never written to Git, static HTML,
frontend bundles, logs, or screenshots.
"""

from __future__ import annotations

import json
import re
import socket
import time
from typing import Any
from urllib.error import URLError

from .models import read_registry
from .util import HTTPError, bearer, http, http_json, run

TEXT_ALIASES = ("gx-max", "gx-auto")
PUBLIC_MODELS = TEXT_ALIASES
KILO_VERIFIED = "7.7.9"
OPENWEBUI_VERIFIED = "0.11.4"
KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{8,200}$")
INTERNAL_GATEWAY = "http://127.0.0.1:4000/v1"


def _registry_facts(cfg) -> dict:
    """Honest per-alias facts from the registry (context from the production
    pack; output limits are not pinned there, so they are omitted)."""
    reg = read_registry(cfg.registry_path)
    prod = (reg.get("aliases") or {}).get("gx-max") or {}
    spec = (reg.get("models") or {}).get(prod.get("model")) or {}
    context = spec.get("max_context")
    return {"context": context, "vision": bool(spec.get("vision"))}


def kilo_models(cfg) -> dict:
    facts = _registry_facts(cfg)
    models = {}
    for alias in TEXT_ALIASES:
        models[alias] = {"name": alias, "tool_call": True, "attachment": facts["vision"],
                         "reasoning": True, "modalities": {"input": ["text"] + (["image"] if facts["vision"] else []),
                                                           "output": ["text"]}}
        if facts["context"]:
            models[alias]["limit"] = {"context": facts["context"]}
    return models


def kilo_config(cfg, base_url: str, key_placeholder: str = "{env:GX_API_KEY}") -> str:
    doc = {"$schema": "https://app.kilo.ai/config.json", "model": "gx-cluster/gx-auto",
           "provider": {"gx-cluster": {"name": "GX Cluster", "npm": "@ai-sdk/openai-compatible",
                                       "options": {"baseURL": base_url, "apiKey": key_placeholder,
                                                   "timeout": 900000, "chunkTimeout": 30000},
                                       "models": kilo_models(cfg)}}}
    return json.dumps(doc, indent=2)


def mask_key(secret: str) -> str:
    if not secret or len(secret) < 8:
        return "sk-••••"
    prefix = secret[:3] if secret.startswith("sk-") else secret[:2]
    return f"{prefix}••••••••••••{secret[-4:]}"


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _master_key(cfg) -> str:
    return (cfg.secret("LITELLM_MASTER_KEY") or "").strip()


def _kilo_versions() -> dict:
    import os
    from pathlib import Path
    found = []
    for d in ("~/.vscode/extensions", "~/.vscode-server/extensions", "~/.cursor/extensions"):
        base = Path(os.path.expanduser(d))
        if base.is_dir():
            for e in sorted(base.iterdir()):
                m = re.match(r"kilocode\.kilo-code-(\d+\.\d+\.\d+)", e.name)
                if m:
                    found.append(m.group(1))
    ext = max(found, key=lambda v: tuple(int(x) for x in v.split("."))) if found else None
    return {"extension": ext, "extensions_found": sorted(set(found)),
            "verified_against": KILO_VERIFIED,
            "matches": ext == KILO_VERIFIED if ext else None}


def _openwebui_version() -> dict:
    res = run(["docker", "exec", "open-webui", "sh", "-c", "grep -m1 '\"version\"' /app/package.json"],
              timeout=10)
    m = re.search(r'"version"\s*:\s*"([^"]+)"', res.out or "")
    version = m.group(1) if res.ok and m else None
    return {"version": version, "container": "open-webui" if version else None,
            "verified_against": OPENWEBUI_VERIFIED,
            "matches": version == OPENWEBUI_VERIFIED if version else None,
            "note": "an unrelated user app: shown for convenience, not cluster-managed"}


_version_cache: dict[str, tuple[float, dict]] = {}


def _cached(name: str, fn) -> dict:
    now = time.time()
    hit = _version_cache.get(name)
    if hit and now - hit[0] < 300:
        return hit[1]
    value = fn()
    _version_cache[name] = (now, value)
    return value


def setup_info(cfg) -> dict:
    return connections_info(cfg, reveal=False)


def connections_info(cfg, *, reveal: bool = False) -> dict:
    base = cfg.public_gateway_url.rstrip("/")
    if not base.endswith("/v1"):
        base = base + "/v1"
    internal = cfg.litellm_base.rstrip("/") + "/v1"
    offline = cfg.offline
    kilo = {"verified_version": KILO_VERIFIED} if offline else _cached("kilo", _kilo_versions)
    owui = {"verified_against": OPENWEBUI_VERIFIED} if offline else _cached("owui", _openwebui_version)
    secret = "" if offline else _master_key(cfg)
    masked = mask_key(secret) if secret else "sk-•••• (not loaded)"
    healthy = False if offline else _tcp_ok("127.0.0.1", 4000)
    return {
        "gateway": {
            "status": "healthy" if healthy else ("unknown" if offline else "unhealthy"),
            "healthy": healthy,
            "public_url": base,
            "internal_url": internal,
            "api_path": "/v1",
            "auth": "enabled",
            "models": list(PUBLIC_MODELS),
        },
        "api_key": {
            "label": "Gateway master key — use this in Kilo Code, OpenWebUI and OpenAI-compatible clients",
            "source": "legenex/gateway/.env (LITELLM_MASTER_KEY)",
            "masked": masked,
            "revealed": bool(reveal and secret),
            "key": secret if (reveal and secret) else None,
        },
        "gateway_url": base,
        "local_gateway_url": internal,
        "text_aliases": list(TEXT_ALIASES),
        "aliases": {
            "gx-max": "Explicit DeepSeek V4.1 Flash (uncensored). Use for the hardest work; "
                      "it must be READY (started with a profile from the Control Center).",
            "gx-auto": "Recommended default: the orchestrator picks the serving profile and the "
                       "reasoning level per request.",
        },
        "kilo": {
            **kilo,
            "provider_api": "OpenAI Compatible",
            "recommended_model": "gx-auto",
            "alternatives": ["gx-max"],
            "config_file": "~/.config/kilo/kilo.jsonc (global) or .kilo/kilo.jsonc in a project",
            "config_example": kilo_config(cfg, base),
            "key_env": "GX_API_KEY",
            "manual_steps": [
                "Open Kilo Code, then Settings (gear icon) > Providers.",
                "On the Custom provider card (\"Add a custom provider by base URL.\") click Connect.",
                "Provider ID: gx-cluster",
                "Display name: GX Cluster",
                "Provider API: OpenAI Compatible",
                f"Base URL: {base}",
                "API key: the gateway key from this page (Reveal, then Copy).",
                "Headers (optional): leave empty.",
                "Models: Add gx-auto (recommended), then gx-max. Tick Reasoning for both; "
                "tick Image if the registry says the pack is multimodal.",
                "Click Submit, then choose gx-cluster / gx-auto as the coding model.",
            ],
        },
        "openwebui": {
            **owui,
            "connection_type": "External",
            "auth": "Bearer",
            "api_type": "Chat Completions",
            "base_url": INTERNAL_GATEWAY,
            "public_url": base,
            "model_ids": list(PUBLIC_MODELS),
            "enable_openai": True,
            "enable_ollama": False,
            "manual_steps": [
                "OpenWebUI on gx10-01 uses host networking. The production connection is already "
                f"{INTERNAL_GATEWAY} with the gateway master key.",
                "User menu > Admin Panel > Settings > Connections.",
                "OpenAI API: on. Ollama: off.",
                f"URL: {INTERNAL_GATEWAY}",
                "Auth: Bearer. API key: the gateway key from this page.",
                "API Type: Chat Completions. Provider: Default.",
                "Model IDs: gx-auto, gx-max.",
            ],
            "note": "OpenWebUI talks to loopback :4000. External clients use the Tailscale URL.",
        },
        "generic": {"examples": examples(base)},
    }


def examples(base: str) -> dict:
    return {
        "env": "export GX_API_KEY=YOUR_GX_API_KEY   # gateway key from Connections (Reveal)",
        "curl_models": f'curl {base}/models \\\n  -H "Authorization: Bearer $GX_API_KEY"',
        "curl_chat": (f'curl {base}/chat/completions \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n'
                      '  -H "Content-Type: application/json" \\\n'
                      '  -d \'{"model": "gx-auto", "messages": [{"role": "user", '
                      '"content": "Reply exactly CODE_OK"}]}\''),
        "python": (
            "from openai import OpenAI  # pip install openai\n\n"
            f'client = OpenAI(base_url="{base}", api_key="YOUR_GX_API_KEY")\n\n'
            "for model in (\"gx-auto\", \"gx-max\"):\n"
            "    reply = client.chat.completions.create(\n"
            "        model=model,\n"
            "        messages=[{\"role\": \"user\", \"content\": \"Say hello in five words.\"}],\n"
            "        max_tokens=64,\n"
            "    )\n"
            "    print(model, reply.choices[0].message.content)\n"),
    }


def test_connection(cfg, client: str, secret: Any) -> dict:
    """Prove a pasted key works for `client`, through the real gateway."""
    if client not in ("kilo", "openwebui", "generic"):
        raise ValueError("unknown client")
    if not isinstance(secret, str) or not KEY_RE.match(secret):
        raise ValueError("paste a gateway key (sk-...)")
    base = cfg.litellm_base.rstrip("/")
    headers = bearer(secret)
    checks: list[dict] = []
    out: dict[str, Any] = {"client": client, "checks": checks}
    try:
        t0 = time.time()
        status, data = http_json("GET", f"{base}/v1/models", headers=headers, timeout=15)
        ids = [m.get("id") for m in (data.get("data") or [])] if isinstance(data, dict) else []
        checks.append({"check": "GET /v1/models", "ok": status == 200, "status": status,
                       "detail": ", ".join(i for i in ids if i) or _err(data),
                       "ms": round((time.time() - t0) * 1000)})
        if status != 200:
            out["connected"] = False
            out["summary"] = "The gateway refused the key." if status in (401, 403) else f"HTTP {status}"
            return out
        visible = [w for w in PUBLIC_MODELS if w in ids]
        checks.append({"check": "gx-max and gx-auto visible to this key",
                       "ok": len(visible) == len(PUBLIC_MODELS),
                       "detail": "yes" if len(visible) == len(PUBLIC_MODELS)
                       else "the key does not allow these aliases"})
        t0 = time.time()
        res = http("POST", f"{base}/v1/chat/completions", headers=headers, timeout=180,
                   body={"model": "gx-auto",
                         "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                         "max_tokens": 16, "temperature": 0})
        try:
            body = res.json()
        except ValueError:
            body = {}
        answer = ""
        if isinstance(body, dict) and body.get("choices"):
            answer = ((body["choices"][0].get("message") or {}).get("content") or "").strip()[:80]
        checks.append({"check": "real completion on gx-auto", "ok": res.status == 200 and bool(answer),
                       "status": res.status, "detail": answer or _err(body),
                       "ms": round((time.time() - t0) * 1000)})
    except HTTPError as exc:
        checks.append({"check": "gateway reachable", "ok": False, "detail": exc.message})
    out["connected"] = all(c["ok"] for c in checks)
    out["summary"] = "CONNECTED" if out["connected"] else next(
        (f"{c['check']}: {c.get('detail')}" for c in checks if not c["ok"]), "FAILED")
    return out


def test_live(cfg, target: str) -> dict:
    """Server-side connection test using the live gateway key. Never logs the key."""
    target = (target or "gateway").strip()
    allowed = ("gateway", "gx-max", "gx-auto")
    if target not in allowed:
        raise ValueError("target must be gateway, gx-max or gx-auto")
    checks: list[dict] = []
    out: dict[str, Any] = {"target": target, "checks": checks}
    if cfg.offline:
        out["ok"] = False
        out["summary"] = "offline"
        return out
    secret = _master_key(cfg)
    if not secret:
        checks.append({"check": "gateway key loaded", "ok": False, "detail": "LITELLM_MASTER_KEY missing"})
        out["ok"] = False
        out["summary"] = "no gateway key"
        return out
    base = cfg.litellm_base.rstrip("/")
    headers = bearer(secret)
    try:
        t0 = time.time()
        reachable = _tcp_ok("127.0.0.1", 4000)
        checks.append({"check": "gateway reachable", "ok": reachable,
                       "detail": "127.0.0.1:4000" if reachable else "connection refused",
                       "ms": round((time.time() - t0) * 1000)})
        t0 = time.time()
        status, data = http_json("GET", f"{base}/v1/models", headers=headers, timeout=15)
        ids = [m.get("id") for m in (data.get("data") or [])] if isinstance(data, dict) else []
        public = [i for i in ids if i in PUBLIC_MODELS]
        checks.append({"check": "auth valid", "ok": status == 200, "status": status,
                       "detail": f"HTTP {status}" if status != 200 else f"{len(ids)} models from loopback",
                       "ms": round((time.time() - t0) * 1000)})
        missing = [m for m in PUBLIC_MODELS if m not in ids]
        checks.append({"check": "models reachable", "ok": not missing,
                       "detail": "gx-max gx-auto" if not missing else f"missing {missing}"})
        if target != "gateway":
            t0 = time.time()
            res = http("POST", f"{base}/v1/chat/completions", headers=headers, timeout=180,
                       body={"model": target,
                             "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                             "max_tokens": 16, "temperature": 0})
            try:
                body = res.json()
            except ValueError:
                body = {}
            answer = ""
            if isinstance(body, dict) and body.get("choices"):
                answer = ((body["choices"][0].get("message") or {}).get("content") or "").strip()[:80]
            checks.append({"check": f"completion {target}", "ok": res.status == 200 and bool(answer),
                           "status": res.status, "detail": answer or _err(body),
                           "ms": round((time.time() - t0) * 1000)})
    except HTTPError as exc:
        checks.append({"check": "gateway", "ok": False, "detail": exc.message})
    except URLError as exc:
        checks.append({"check": "gateway", "ok": False, "detail": str(exc.reason or exc)})
    out["ok"] = all(c.get("ok") for c in checks) if checks else False
    out["summary"] = "PASS" if out["ok"] else next(
        (f"{c['check']}: {c.get('detail')}" for c in checks if not c.get("ok")), "FAILED")
    return out


def _err(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        if err:
            return str(err)[:200]
    return ""
