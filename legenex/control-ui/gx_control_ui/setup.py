"""Client setup (D-037): Kilo Code, Open WebUI and generic OpenAI clients.

Every label below was read from the installed clients on gx10-01
(2026-09-17): the Kilo Code 7.7.2 VS Code extension (`dist/webview.js`,
config schema in the bundled binary; the npm CLI 7.5.14 agrees) and Open
WebUI 0.11.3 (`AddConnectionModal.svelte`, `admin/Settings/Connections.svelte`).
The versions are re-detected live so the page says when the installed client
differs from the version the steps were verified against.

The connection tests use the key the user pastes (never the master key) and
never store it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .util import HTTPError, bearer, http, http_json, run

TEXT_ALIASES = ("gx-auto", "gx-mini", "gx-fast", "gx-reason", "gx-max")
KILO_VERIFIED = "7.7.2"
OPENWEBUI_VERIFIED = "0.11.3"
KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{8,200}$")

#: Model capabilities for the Kilo config (context/output limits as LiteLLM serves them).
KILO_MODELS = {
    "gx-auto": {"reasoning": False, "attachment": True, "input": ["text", "image"], "context": 57344, "output": 8192},
    "gx-mini": {"reasoning": False, "attachment": True, "input": ["text", "image"], "context": 57344, "output": 8192},
    "gx-fast": {"reasoning": False, "attachment": True, "input": ["text", "image"], "context": 98304,
                "output": 32768},
    "gx-reason": {"reasoning": True, "attachment": True, "input": ["text", "image"], "context": 49152,
                  "output": 16384},
    "gx-max": {"reasoning": True, "attachment": False, "input": ["text"], "context": 262144, "output": 65536},
}


def kilo_config(base_url: str, key_placeholder: str = "{env:GX_API_KEY}") -> str:
    models = {}
    for alias, m in KILO_MODELS.items():
        models[alias] = {"name": alias, "tool_call": True, "attachment": m["attachment"],
                         "reasoning": m["reasoning"],
                         "modalities": {"input": m["input"], "output": ["text"]},
                         "limit": {"context": m["context"], "output": m["output"]}}
    doc = {"$schema": "https://app.kilo.ai/config.json", "model": "gx-cluster/gx-auto",
           "provider": {"gx-cluster": {"name": "GX Cluster", "npm": "@ai-sdk/openai-compatible",
                                       "options": {"baseURL": base_url, "apiKey": key_placeholder,
                                                   "timeout": 900000},
                                       "models": models}}}
    return json.dumps(doc, indent=2)


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
    base = cfg.public_gateway_url
    offline = cfg.offline
    kilo = {"verified_version": KILO_VERIFIED} if offline else _cached("kilo", _kilo_versions)
    owui = {"verified_against": OPENWEBUI_VERIFIED} if offline else _cached("owui", _openwebui_version)
    return {
        "gateway_url": base,
        "local_gateway_url": cfg.litellm_base.rstrip("/") + "/v1",
        "playground_url": cfg.public_playground_url,
        "music_api_url": cfg.public_playground_url.rstrip("/") + "/v1/music",
        "text_aliases": list(TEXT_ALIASES),
        "creative_aliases": {"gx-image": "GX-Playground or /v1/images/* on the gateway",
                             "gx-video": "GX-Playground or /v1/videos/* on the gateway",
                             "gx-music": "GX-Playground or the music API on GX-Playground (not a chat model)"},
        "gx_auto": {
            "recommended_for_kilo": True,
            "routes": [
                {"when": "simple / trivial requests", "to": "gx-mini"},
                {"when": "coding, actions and tool work", "to": "gx-fast"},
                {"when": "hard reasoning and difficult debugging", "to": "gx-reason"},
            ],
            "gx_max": "gx-auto never starts gx-max. It only uses gx-max if gx-max is already running.",
        },
        "kilo": {
            **kilo,
            "provider_api": "OpenAI Compatible",
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
                "API key: paste the key you created on the API Keys page (or {env:GX_API_KEY}).",
                "Headers (optional): leave empty.",
                "Models: click Add model for each alias. ID and Name: gx-auto (then gx-mini, gx-fast, "
                "gx-reason, and gx-max if you want it). Tick Image for all except gx-max; tick Reasoning for "
                "gx-reason and gx-max.",
                "Click Submit, then choose gx-cluster / gx-auto as the model.",
                "Tool calling and context limits are not in this form: use \"Edit advanced settings in the "
                "JSON config file\" or copy the full config below.",
            ],
            "labels": ["Providers", "Custom provider", "Connect", "Provider ID", "Display name", "Provider API",
                       "OpenAI Compatible", "Base URL", "API key", "Headers (optional)", "Models", "Add model",
                       "Reasoning", "Image", "Submit"],
        },
        "openwebui": {
            **owui,
            "manual_steps": [
                "Open Open WebUI and sign in as an administrator.",
                "User menu > Admin Panel > Settings (or user menu > Settings). A settings window opens.",
                "In the left sidebar choose Admin > AI > Connections.",
                "Switch on OpenAI API. Under Manage OpenAI API Connections click + (Add Connection).",
                "Connection Type: External",
                f"URL: {base}   (Open WebUI on gx10-01 itself can use http://127.0.0.1:4000/v1)",
                "Auth: Bearer, then paste your key into the API Key field.",
                "API Type: Chat Completions",
                "Advanced > Provider: leave on Default.",
                "Model IDs: gx-auto, gx-mini, gx-fast, gx-reason (add gx-max only if users may start it).",
                "Click Verify Connection, then Save.",
            ],
            "labels": ["Admin Panel", "Settings", "Admin", "AI", "Connections", "OpenAI API",
                       "Manage OpenAI API Connections", "Add Connection", "Connection Type", "External", "URL",
                       "Auth", "Bearer", "API Key", "API Type", "Chat Completions", "Advanced", "Provider",
                       "Default", "Model IDs", "Verify Connection", "Save"],
            "note": "Open WebUI is a chat client. Image, video and music creation live in GX-Playground.",
        },
        "generic": {"examples": examples(base, cfg.public_playground_url.rstrip("/"))},
    }


def examples(base: str, playground: str) -> dict:
    return {
        "env": "export GX_API_KEY=YOUR_GX_API_KEY   # the key from Control Center > API Keys",
        "curl_models": f'curl {base}/models \\\n  -H "Authorization: Bearer $GX_API_KEY"',
        "curl_chat": (f'curl {base}/chat/completions \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n'
                      '  -H "Content-Type: application/json" \\\n'
                      '  -d \'{"model": "gx-mini", "messages": [{"role": "user", "content": "Say hello"}]}\''),
        "python": (
            "from openai import OpenAI  # pip install openai\n\n"
            f'client = OpenAI(base_url="{base}", api_key="YOUR_GX_API_KEY")\n\n'
            "for model in (\"gx-mini\", \"gx-fast\", \"gx-reason\", \"gx-auto\"):\n"
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
            "  model: 'gx-auto',\n"
            "  messages: [{ role: 'user', content: 'Say hello in five words.' }],\n"
            "  max_tokens: 64,\n"
            "});\n"
            "console.log(reply.choices[0].message.content);\n"),
        "gx_max": (
            f'curl {base}/chat/completions \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n'
            '  -H "Content-Type: application/json" --max-time 1200 \\\n'
            '  -d \'{"model": "gx-max", "messages": [{"role": "user", "content": "Hard question..."}]}\'\n'
            "# gx-max takes over BOTH nodes: the first request drains every other model and waits ~9 minutes\n"
            "# while it loads. The key must allow gx-max. Prefer starting it from Resource Control."),
        "music": (
            f'curl {playground}/v1/music/generations \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n'
            '  -H "Content-Type: application/json" \\\n'
            '  -d \'{"prompt": "warm lo-fi beat", "style_tags": ["lo-fi", "chill"], "instrumental": true, '
            '"duration": 30}\'\n'
            "# The key must allow gx-music. Poll GET /v1/music/{id}; download GET /v1/music/{id}/content?format=mp3"),
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
        wanted = ["gx-auto"] if client == "kilo" else ["gx-mini", "gx-auto"]
        visible = [w for w in wanted if w in ids]
        checks.append({"check": f"{' / '.join(wanted)} visible to this key", "ok": bool(visible),
                       "detail": "yes" if visible else "the key does not allow these aliases"})
        model = "gx-auto" if client == "kilo" else (visible[0] if visible else "gx-mini")
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


def _err(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        if err:
            return str(err)[:200]
    return ""
