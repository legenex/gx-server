"""Client setup and Connections page: Kilo Code, Open WebUI, OpenAI clients.

Labels were read from the installed clients on gx10-01. Kilo Code version is
re-detected from ~/.vscode/extensions; Open WebUI from the running container.

The Connections page retrieves the live gateway key server-side from
legenex/gateway/.env (LITELLM_MASTER_KEY). It is masked by default, revealed
only to an authenticated admin session, never written to Git, static HTML,
frontend bundles, logs, or screenshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError

from .util import HTTPError, bearer, http, http_json, run

TEXT_ALIASES = ("gx-mini", "gx-code", "gx-auto", "gx-max")
PUBLIC_MODELS = TEXT_ALIASES
KILO_VERIFIED = "7.7.9"
OPENWEBUI_VERIFIED = "0.11.4"
KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{8,200}$")
INTERNAL_GATEWAY = "http://127.0.0.1:4000/v1"

#: Model capabilities for the Kilo config (context/output limits as LiteLLM serves them).
KILO_MODELS = {
    "gx-mini": {"reasoning": False, "attachment": True, "input": ["text", "image"], "context": 57344, "output": 8192},
    "gx-code": {"reasoning": False, "attachment": True, "input": ["text", "image"], "context": 49152, "output": 16384},
    "gx-auto": {"reasoning": False, "attachment": True, "input": ["text", "image"], "context": 49152, "output": 16384},
    "gx-max": {"reasoning": True, "attachment": False, "input": ["text"], "context": 49152, "output": 16384},
}


def kilo_config(base_url: str, key_placeholder: str = "{env:GX_API_KEY}") -> str:
    models = {}
    for alias, m in KILO_MODELS.items():
        models[alias] = {"name": alias, "tool_call": True, "attachment": m["attachment"],
                         "reasoning": m["reasoning"],
                         "modalities": {"input": m["input"], "output": ["text"]},
                         "limit": {"context": m["context"], "output": m["output"]}}
    doc = {"$schema": "https://app.kilo.ai/config.json", "model": "gx-cluster/gx-code",
           "provider": {"gx-cluster": {"name": "GX Cluster", "npm": "@ai-sdk/openai-compatible",
                                       "options": {"baseURL": base_url, "apiKey": key_placeholder,
                                                   "timeout": 900000, "chunkTimeout": 30000},
                                       "models": models}}}
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
    home = Path.home()
    found = []
    for d in ("~/.vscode/extensions", "~/.vscode-server/extensions", "~/.cursor/extensions"):
        base = Path(os.path.expanduser(d))
        if base.is_dir():
            for e in sorted(base.iterdir()):
                m = re.match(r"kilocode\.kilo-code-(\d+\.\d+\.\d+)", e.name)
                if m:
                    found.append(m.group(1))
    cli = None
    for candidate in sorted(home.glob(".nvm/versions/node/*/lib/node_modules/@kilocode/cli/package.json")):
        try:
            cli = json.loads(candidate.read_text()).get("version")
        except (OSError, ValueError):
            pass
    ext = max(found, key=lambda v: tuple(int(x) for x in v.split("."))) if found else None
    return {"extension": ext, "extensions_found": sorted(set(found)), "cli": cli,
            "verified_against": KILO_VERIFIED,
            "matches": ext == KILO_VERIFIED if ext else None}


def _openwebui_version() -> dict:
    res = run(["docker", "exec", "open-webui", "sh", "-c", "grep -m1 '\"version\"' /app/package.json"], timeout=10)
    m = re.search(r'"version"\s*:\s*"([^"]+)"', res.out or "")
    version = m.group(1) if res.ok and m else None
    return {"version": version, "container": "open-webui" if version else None,
            "verified_against": OPENWEBUI_VERIFIED, "matches": version == OPENWEBUI_VERIFIED if version else None,
            "base_url_same_host": "http://127.0.0.1:4000/v1"}


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
    payload = {
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
        "gx_auto": {
            "recommended_for_kilo": False,
            "routes": [
                {"when": "simple / trivial requests", "to": "gx-mini"},
                {"when": "coding, tools, debugging and substantial work", "to": "gx-code"},
            ],
            "gx_max": "gx-auto never starts gx-max. Choose gx-max explicitly for the solver/reviewer workflow.",
        },
        "kilo": {
            **kilo,
            "provider_api": "OpenAI Compatible",
            "recommended_model": "gx-code",
            "alternatives": ["gx-mini", "gx-auto", "gx-max"],
            "config_file": "~/.config/kilo/kilo.jsonc (global) or .kilo/kilo.jsonc in a project",
            "config_example": kilo_config(base),
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
                "Models: Add gx-code (recommended), then gx-mini, gx-auto, gx-max if you want them. "
                "Tick Image for all except gx-max; tick Reasoning for gx-max.",
                "Click Submit, then choose gx-cluster / gx-code as the coding model.",
            ],
            "labels": ["Providers", "Custom provider", "Connect", "Provider ID", "Display name", "Provider API",
                       "OpenAI Compatible", "Base URL", "API key", "Headers (optional)", "Models", "Add model",
                       "Reasoning", "Image", "Submit"],
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
                "Model IDs: gx-auto, gx-mini, gx-code, gx-max.",
            ],
            "labels": ["Admin Panel", "Settings", "Connections", "OpenAI API", "URL", "Auth", "Bearer",
                       "API Key", "API Type", "Chat Completions", "Model IDs"],
            "note": "OpenWebUI talks to loopback :4000. External clients use the Tailscale URL.",
        },
        "generic": {"examples": examples(base)},
    }
    return payload


def examples(base: str, playground: str | None = None) -> dict:
    return {
        "env": "export GX_API_KEY=YOUR_GX_API_KEY   # gateway key from Connections (Reveal)",
        "curl_models": f'curl {base}/models \\\n  -H "Authorization: Bearer $GX_API_KEY"',
        "curl_chat": (f'curl {base}/chat/completions \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n'
                      '  -H "Content-Type: application/json" \\\n'
                      '  -d \'{"model": "gx-code", "messages": [{"role": "user", "content": "Reply exactly CODE_OK"}]}\''),
        "python": (
            "from openai import OpenAI  # pip install openai\n\n"
            f'client = OpenAI(base_url="{base}", api_key="YOUR_GX_API_KEY")\n\n'
            "for model in (\"gx-mini\", \"gx-code\", \"gx-auto\"):\n"
            "    reply = client.chat.completions.create(\n"
            "        model=model,\n"
            "        messages=[{\"role\": \"user\", \"content\": \"Say hello in five words.\"}],\n"
            "        max_tokens=64,\n"
            "    )\n"
            "    print(model, reply.choices[0].message.content)\n"),
        "javascript": (
            "import OpenAI from 'openai'; // npm install openai\n\n"
            f"const client = new OpenAI({{ baseURL: '{base}', apiKey: process.env.GX_API_KEY }});\n\n"
            "const reply = await client.chat.completions.create({\n"
            "  model: 'gx-code',\n"
            "  messages: [{ role: 'user', content: 'Say hello in five words.' }],\n"
            "  max_tokens: 64,\n"
            "});\n"
            "console.log(reply.choices[0].message.content);\n"),
        "gx_max": (
            f'curl {base}/chat/completions \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n'
            '  -H "Content-Type: application/json" --max-time 1200 \\\n'
            '  -d \'{"model": "gx-max", "messages": [{"role": "user", "content": '
            '"Fix this Python: def add(a,b): return a-b"}]}\'\n'
            "# gx-max runs the dual-worker solver (gx-code-01) + reviewer (gx-code-02) workflow."),
    }


def _kilo_shaped(task: str) -> list[dict]:
    return [
        {"role": "system", "content": "You are Kilo, a coding agent. Use the tools provided."},
        {"role": "user", "content": f"<task>\n{task}\n</task>\n<environment_details>\n# VSCode Visible Files\n"
                                    "(none)\n# Current Mode\ncode\n</environment_details>"},
    ]


def _fingerprint(messages: list[dict]) -> str:
    blob = json.dumps(messages, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
                       "detail": ", ".join(i for i in ids if i) or _err(data), "ms": round((time.time() - t0) * 1000)})
        if status != 200:
            out["connected"] = False
            out["summary"] = "The gateway refused the key." if status in (401, 403) else f"HTTP {status}"
            return out
        wanted = list(PUBLIC_MODELS)
        visible = [w for w in wanted if w in ids]
        checks.append({"check": f"{' / '.join(wanted)} visible to this key", "ok": len(visible) == len(wanted),
                       "detail": "yes" if len(visible) == len(wanted) else "the key does not allow these aliases"})
        model = "gx-auto" if client == "kilo" else "gx-mini"
        if client == "kilo":
            messages = _kilo_shaped("Reply with the single word: pong")
        else:
            messages = [{"role": "user", "content": "Reply with the single word: pong"}]
        t0 = time.time()
        res = http("POST", f"{base}/v1/chat/completions", headers=headers, timeout=180,
                   body={"model": model, "messages": messages, "max_tokens": 16, "temperature": 0})
        try:
            body = res.json()
        except ValueError:
            body = {}
        answer = ""
        if isinstance(body, dict) and body.get("choices"):
            answer = ((body["choices"][0].get("message") or {}).get("content") or "").strip()[:80]
        checks.append({"check": f"real completion on {model}", "ok": res.status == 200 and bool(answer),
                       "status": res.status, "detail": answer or _err(body),
                       "ms": round((time.time() - t0) * 1000)})
        routed = res.headers.get("x-gx-routed-to") or res.headers.get("X-GX-Routed-To")
        if client == "kilo":
            decision = None
            try:
                _, journal = http_json("GET", f"{cfg.orchestrator_base}/routing/decisions?fingerprint="
                                       f"{_fingerprint(messages)}&limit=5", timeout=5)
                decision = next((d for d in (journal or {}).get("data", []) if d.get("event") == "decision"), None)
            except HTTPError:
                pass
            tier = (decision or {}).get("tier") or routed
            checks.append({"check": "gx-auto routing decision (orchestrator journal)", "ok": bool(tier),
                           "detail": f"trivial request routed to {tier}" if tier else "no decision found",
                           "tier": tier, "intent": (decision or {}).get("intent")})
            out["routing"] = {"tier": tier, "intent": (decision or {}).get("intent"),
                              "signals": (decision or {}).get("signals")}
    except HTTPError as exc:
        checks.append({"check": "gateway reachable", "ok": False, "detail": exc.message})
    out["connected"] = all(c["ok"] for c in checks)
    out["summary"] = "CONNECTED" if out["connected"] else next(
        (f"{c['check']}: {c.get('detail')}" for c in checks if not c["ok"]), "FAILED")
    return out


def test_live(cfg, target: str) -> dict:
    """Server-side connection test using the live gateway key. Never logs the key."""
    target = (target or "gateway").strip()
    allowed = ("gateway", "gx-mini", "gx-code", "gx-auto")
    if target not in allowed:
        raise ValueError("target must be gateway, gx-mini, gx-code or gx-auto")
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
                       "detail": "gx-mini gx-code gx-auto gx-max" if not missing else f"missing {missing}"})
        if target != "gateway":
            t0 = time.time()
            res = http("POST", f"{base}/v1/chat/completions", headers=headers, timeout=180,
                       body={"model": target, "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
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
    out["summary"] = "PASS" if out["ok"] else next((f"{c['check']}: {c.get('detail')}" for c in checks if not c.get("ok")), "FAILED")
    return out


def _err(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        if err:
            return str(err)[:200]
    return ""
