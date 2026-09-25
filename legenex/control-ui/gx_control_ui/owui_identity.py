"""Open WebUI identity metadata, generated from the model registry (D-038).

Production Open WebUI (container ``open-webui``, https://chat.legenex.co)
lists the GX aliases straight from its LiteLLM connection. Without a model
entry there is no system prompt, and a small fine-tune answers "what model are
you?" from its training data: gx-mini called itself the official Qwen3.5 and,
in another chat, Grok-3. That is not a routing fault; routing is proven
separately (sha256, llama-server model_path, gateway logs).

This module keeps one Open WebUI model entry per text alias, with the alias
as its id and name and a short, factual system prompt built ONLY from
``legenex/models/registry.json``:

* the alias's current repository and revision (always);
* the verified ``identity`` facts (base model, derivation, size, weights),
  used only while ``identity.repository`` / ``identity.revision`` match the
  current binding. After a Model Manager reassignment the prompt shrinks to
  what is known instead of describing the previous model;
* the served context and output limits and the runtime.

Entries are written through Open WebUI's own model layer inside its
container (``docker exec -i open-webui python3 -``, payload on stdin), owned
by the oldest admin, with no access grants (the same visibility the aliases
had before: administrators only). Rows this module did not create are never
modified unless ``adopt`` is set. Nothing else in Open WebUI is touched.

    python3 -m gx_control_ui.owui_identity plan|check|apply [--adopt]
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .util import run

log = logging.getLogger("gx.ui.owui_identity")

CONTAINER = "open-webui"
VERIFIED_OWUI = "0.11.4"
#: text aliases whose Open WebUI entries carry an identity prompt
ALIASES = ("gx-mini", "gx-code", "gx-auto", "gx-max")
MARKER = "gx_identity"
ROLE = {
    "gx-mini": "the fast local model",
    "gx-code": "the coding model (Ornith workers on both nodes)",
    "gx-max": "the dual-worker solver and reviewer workflow",
}
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY = REPO_ROOT / "legenex" / "models" / "registry.json"


class IdentityError(Exception):
    pass


def _short(rev: str | None) -> str:
    return (rev or "")[:12] or "unknown"


def _tokens(n: Any) -> str | None:
    return f"{int(n):,}" if isinstance(n, int) and n > 0 else None


def facts_match(entry: dict) -> bool:
    ident = entry.get("identity") or {}
    return bool(ident) and ident.get("repository") == entry.get("repository") \
        and ident.get("revision") == entry.get("revision")


def model_prompt(alias: str, entry: dict) -> str:
    """The system prompt for one direct alias. Only registry facts."""
    repo = entry.get("repository")
    if not repo:
        raise IdentityError(f"{alias} has no repository in the registry")
    ident = entry.get("identity") or {}
    matched = facts_match(entry)
    lines = [f"You are {alias} in the GX-Cluster, a private two-node AI cluster: {ROLE.get(alias, 'a model')}. "
             f"\"{alias}\" is a user-facing alias, not a model name."]
    under = f"Your underlying deployed language model is {repo} (revision {_short(entry.get('revision'))})"
    if matched and ident.get("derivation"):
        under += f", {ident['derivation']}"
    lines.append(under + ".")
    if matched:
        details = []
        if ident.get("parameters"):
            details.append(f"Parameters: {ident['parameters']}.")
        if ident.get("base_model") and ident.get("base_parameters"):
            details.append(f"Base model {ident['base_model']}: {ident['base_parameters']} parameters.")
        if ident.get("weights"):
            details.append(f"Weights: {ident['weights']}.")
        if ident.get("modalities"):
            details.append(f"Capabilities: {ident['modalities']}.")
        lines.append(" ".join(details))
    else:
        lines.append("Other details of this model (size, precision, training) have not been verified for this "
                     "deployment; do not state them.")
    runtime_raw = str(entry.get("runtime") or "")
    engine = runtime_raw.split(" (")[0].split(" via ")[0].strip()
    if engine:
        node = entry.get("node")
        lines.append(f"Runtime: served locally through {engine}" + (f" on {node}" if node else "")
                     + (", behind llama-swap and a LiteLLM gateway." if "llama-swap" in runtime_raw
                        else ", behind a LiteLLM gateway."))
    ctx, out = _tokens(entry.get("context")), _tokens(entry.get("max_output"))
    if ctx:
        lines.append(f"Configured context: up to {ctx} tokens per request (prompt and reply together)"
                     + (f"; replies up to {out} tokens." if out else "."))
    base = ident.get("base_model") if matched else None
    answer = f"that you are {alias}, and that your underlying model is {repo}"
    if base:
        answer += f", derived from {base}"
    lines.append(f"When asked what model you are, say {answer}. If asked whether you are {repo}, answer yes: it is "
                 f"the model generating your replies, served under the alias {alias}.")
    lines.append("Do not claim to be an official release of the base model, a larger model, or any other model or "
                 "product (for example GPT, Claude, Gemini or Grok). Do not invent parameter counts, "
                 "quantization, precision, context length, training data, deployment details, safety "
                 "properties or compliance claims beyond what is written here; if asked, say you do not know.")
    return "\n".join(x for x in lines if x)


def router_prompt(registry: dict) -> str:
    aliases = registry.get("aliases") or {}
    entry = aliases.get("gx-auto") or {}
    ctx, out = _tokens(entry.get("context")), _tokens(entry.get("max_output"))
    return "\n".join(x for x in [
        "You are answering through gx-auto, the automatic router of the GX-Cluster, a private two-node AI "
        "cluster. \"gx-auto\" is a user-facing alias, not a model.",
        "Each request is sent to gx-mini or gx-code, chosen by a deterministic classifier. You cannot see which "
        "one was chosen for this reply. gx-max is a separate explicit mode.",
        "When asked what model you are, say that you are answering through gx-auto and that the model that "
        "wrote the reply is recorded in the cluster's routing journal; do not guess a model name.",
        (f"Configured context: up to {ctx} tokens per request" + (f"; replies up to {out} tokens." if out else "."))
        if ctx else "",
        "Do not claim to be any particular model or product (for example Qwen, GPT, Claude, Gemini or Grok), and "
        "do not invent parameter counts, precision, deployment details, safety properties or compliance claims.",
    ] if x)


def description(alias: str, entry: dict) -> str:
    if alias == "gx-auto":
        return "Automatic router: sends each request to gx-mini or gx-code."
    ident = entry.get("identity") or {}
    base = f" (from {ident['base_model']})" if facts_match(entry) and ident.get("base_model") else ""
    return f"{ROLE.get(alias, alias).capitalize()}. Underlying model: {entry.get('repository')}{base}."


# Open WebUI 0.11.4: native function calling + scoped builtin tools.
# Memory tools are on so personal memory works through the model. Other
# system tools stay off for ordinary chat. ask_user lives under
# builtinTools.user_input.
BUILTIN_TOOLS = {
    "user_input": True,
    "time": False,
    "files": False,
    "knowledge": False,
    "chats": False,
    "subagents": False,
    "memory": True,
    "web_search": False,
    "image_generation": False,
    "code_interpreter": False,
    "notes": False,
    "channels": False,
    "tasks": False,
    "automations": False,
    "calendar": False,
    "notifications": False,
}


def _vision(alias: str, entry: dict) -> bool:
    if alias in ("gx-max",):
        return False
    if alias == "gx-auto":
        return True
    mods = str((entry.get("identity") or {}).get("modalities") or "").lower()
    return "image" in mods


def desired_rows(registry: dict, *, synced_at: str | None = None) -> list[dict]:
    aliases = registry.get("aliases") or {}
    rows = []
    for alias in ALIASES:
        entry = aliases.get(alias)
        if not entry:
            continue
        prompt = router_prompt(registry) if alias == "gx-auto" else model_prompt(alias, entry)
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        vision = _vision(alias, entry)
        rows.append({
            "id": alias, "name": alias, "base_model_id": None,
            "params": {"system": prompt, "function_calling": "native"},
            "meta": {"description": description(alias, entry), "tags": [{"name": "gx-cluster"}],
                     "capabilities": {
                         "vision": vision, "builtin_tools": True, "file_upload": vision,
                         "web_search": False, "image_generation": False,
                         "code_interpreter": False, "memory": True,
                     },
                     "builtinTools": dict(BUILTIN_TOOLS),
                     MARKER: {"source": "legenex/models/registry.json", "alias": alias,
                              "repository": entry.get("repository"), "revision": entry.get("revision"),
                              "facts_verified": alias == "gx-auto" or facts_match(entry),
                              "prompt_sha256": digest, "synced_at": synced_at}},
        })
    return rows


# ------------------------------------------------------------- container side
#: Runs INSIDE the open-webui container with its own Python and model layer.
#: Input (stdin): {"op": "read"|"upsert", "ids": [...], "rows": [...], "adopt": bool}
_CONTAINER_SCRIPT = r'''
import asyncio, json, sys
payload = json.load(sys.stdin)
async def main():
    from open_webui.models.models import Models, ModelForm, ModelMeta, ModelParams
    from open_webui.models.users import Users
    import open_webui
    version = None
    try:
        version = json.load(open("/app/package.json")).get("version")
    except Exception:
        pass
    out = {"version": version, "rows": {}, "written": [], "skipped": []}
    ids = payload.get("ids") or [r["id"] for r in payload.get("rows", [])]
    for mid in ids:
        m = await Models.get_model_by_id(mid)
        out["rows"][mid] = None if m is None else {
            "id": m.id, "name": m.name, "base_model_id": m.base_model_id, "user_id": m.user_id,
            "is_active": m.is_active, "params": m.params.model_dump(), "meta": m.meta.model_dump(),
            "grants": len(m.access_grants or [])}
    if payload.get("op") != "upsert":
        return out
    admins = [u for u in (await Users.get_users()).get("users", []) if getattr(u, "role", None) == "admin"]
    if not admins:
        raise SystemExit("no admin user in Open WebUI")
    owner = sorted(admins, key=lambda u: u.created_at)[0].id
    for row in payload["rows"]:
        current = out["rows"].get(row["id"])
        if current is not None and not (current["meta"] or {}).get("gx_identity") and not payload.get("adopt"):
            out["skipped"].append({"id": row["id"], "reason": "row exists and was not created by the sync"})
            continue
        form = ModelForm(id=row["id"], base_model_id=row["base_model_id"], name=row["name"],
                         meta=ModelMeta(**row["meta"]), params=ModelParams(**row["params"]),
                         access_grants=None if current is not None else [],
                         is_active=True if current is None else current["is_active"])
        if current is None:
            res = await Models.insert_new_model(form, owner)
        else:
            res = await Models.update_model_by_id(row["id"], form)
        if res is None:
            raise SystemExit(f"Open WebUI refused to write {row['id']}")
        out["written"].append(row["id"])
    return out
print(json.dumps(asyncio.run(main())))
'''


@dataclass
class Result:
    ok: bool
    data: dict


class OpenWebUIIdentity:
    def __init__(self, registry_path: Path = DEFAULT_REGISTRY, *, container: str = CONTAINER,
                 runner: Callable[..., Any] = run, audit: Callable[..., None] | None = None) -> None:
        self.registry_path = Path(registry_path)
        self.container = container
        self.runner = runner
        self.audit = audit or (lambda **kw: None)

    def registry(self) -> dict:
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IdentityError(f"cannot read the model registry: {exc}") from None

    def _exec(self, payload: dict) -> dict:
        # Open WebUI's start.sh loads its secret from a file; its model layer needs it
        # to import. The key stays inside the container and is never printed.
        wrapper = ('cd /app/backend && WEBUI_SECRET_KEY="$(cat .webui_secret_key)" '
                   'GLOBAL_LOG_LEVEL=ERROR exec python3 -c "$1"')
        res = self.runner(["docker", "exec", "-i", self.container, "sh", "-c", wrapper, "gx-identity",
                           _CONTAINER_SCRIPT], timeout=120, input_text=json.dumps(payload))
        if not res.ok:
            raise IdentityError(f"open-webui did not answer: {res.out.strip()[-300:]}")
        try:
            return json.loads(res.out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise IdentityError(f"unexpected output from open-webui: {res.out.strip()[-300:]}") from None

    def plan(self) -> dict:
        rows = desired_rows(self.registry())
        live = self._exec({"op": "read", "ids": [r["id"] for r in rows]})
        items = []
        for row in rows:
            cur = live["rows"].get(row["id"])
            if cur is None:
                state = "missing"
            elif not (cur.get("meta") or {}).get(MARKER):
                state = "foreign"  # someone else's row: reported, never overwritten without adopt
            elif self._differs(cur, row):
                state = "drift"
            elif not cur.get("is_active"):
                state = "inactive"
            else:
                state = "ok"
            items.append({"id": row["id"], "state": state, "repository": row["meta"][MARKER]["repository"],
                          "facts_verified": row["meta"][MARKER]["facts_verified"],
                          "prompt_sha256": row["meta"][MARKER]["prompt_sha256"]})
        version = live.get("version")
        return {"open_webui_version": version, "verified_against": VERIFIED_OWUI,
                "version_ok": version == VERIFIED_OWUI, "items": items,
                "in_sync": all(i["state"] == "ok" for i in items)}

    @staticmethod
    def _differs(cur: dict, row: dict) -> bool:
        stored = (cur.get("meta") or {}).get(MARKER) or {}
        params = cur.get("params") or {}
        meta = cur.get("meta") or {}
        return (params.get("system") != row["params"]["system"]
                or params.get("function_calling") != row["params"].get("function_calling")
                or (meta.get("builtinTools") or {}) != (row["meta"].get("builtinTools") or {})
                or (meta.get("capabilities") or {}) != (row["meta"].get("capabilities") or {})
                or cur.get("name") != row["name"] or cur.get("base_model_id") is not None
                or stored.get("repository") != row["meta"][MARKER]["repository"])

    def check(self) -> dict:
        return self.plan()

    def apply(self, *, user: str = "system", adopt: bool = False, force_version: bool = False) -> dict:
        plan = self.plan()
        if not plan["version_ok"] and not force_version:
            raise IdentityError(f"Open WebUI is {plan['open_webui_version']}, the sync was verified against "
                                f"{VERIFIED_OWUI}; re-verify before writing")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        rows = desired_rows(self.registry(), synced_at=stamp)
        todo = [r for r, i in zip(rows, plan["items"], strict=True) if i["state"] != "ok"]
        if not todo:
            return {**plan, "written": [], "skipped": []}
        out = self._exec({"op": "upsert", "rows": todo, "adopt": adopt})
        self.audit(user=user, ip="", action="openwebui.identity.sync", outcome="ok",
                   written=out.get("written"), skipped=out.get("skipped"))
        after = self.plan()
        return {**after, "written": out.get("written", []), "skipped": out.get("skipped", [])}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    op = args[0] if args else "check"
    sync = OpenWebUIIdentity()
    try:
        if op in ("plan", "check"):
            result = sync.plan()
            print(json.dumps(result, indent=2))
            return 0 if result["in_sync"] or op == "plan" else 1
        if op == "apply":
            result = sync.apply(user="cli", adopt="--adopt" in args, force_version="--force-version" in args)
            print(json.dumps(result, indent=2))
            return 0 if result["in_sync"] else 1
        if op == "prompt":
            for row in desired_rows(sync.registry()):
                print(f"===== {row['id']} =====\n{row['params']['system']}\n")
            return 0
    except IdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("usage: python3 -m gx_control_ui.owui_identity plan|check|apply [--adopt]|prompt", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
