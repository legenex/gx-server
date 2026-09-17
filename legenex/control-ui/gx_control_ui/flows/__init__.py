"""Creative Flows (D-040, workstream FLO).

``FlowService`` is the one object the HTTP routes use (``app.flows``). It
validates every document, enforces ownership, applies the lock rules, plans
runs and hands them to the engine. See ``docs/20-creative-flows.md``.

Ownership: flows created in the browser belong to ``ui`` (every signed-in
operator of this private Control Center can see them); flows created with a
gateway key belong to ``key:<16 hex>`` and are visible only to that key (and
to signed-in operators).
"""

from __future__ import annotations

import builtins
import copy
import logging
import time
from collections.abc import Callable
from typing import Any

from . import ai as ai_mod
from . import catalog as cat
from .engine import FAILED_STATES, FlowEngine, plan_for
from .schema import FlowValidationError, readiness, validate_document
from .services import NodeFailure, Services
from .store import FlowError, FlowStore
from .templates import builtin_templates

log = logging.getLogger("gx.ui.flows")

UI_OWNER = "ui"
RUN_MODES = ("full", "node", "from", "downstream", "rerun_failed", "regenerate")

__all__ = ["FlowError", "FlowService", "FlowValidationError", "UI_OWNER"]


def _invalid(exc: FlowValidationError) -> FlowError:
    return FlowError(str(exc), 422, "invalid_flow", exc.issues)


class FlowService:
    def __init__(self, services: Services, *, audit: Callable[..., None] | None = None,
                 voice_available: Callable[[], str | None] | None = None,
                 engine: FlowEngine | None = None) -> None:
        self.services = services
        self.store: FlowStore = services.store
        self.audit = audit or (lambda **kw: None)
        self._voice_available = voice_available or (lambda: None)
        self.engine = engine or FlowEngine(services)
        for tpl in builtin_templates():
            self.store.upsert_builtin(tpl["id"], tpl["name"], tpl["description"], tpl["category"],
                                      validate_document(tpl["graph"]))

    # ============================================================ catalogue
    def live_unavailable(self) -> dict[str, str]:
        """Node types that exist but cannot run on this Control Center right now."""
        out: dict[str, str] = {}
        voice_reason = self._voice_available()
        if voice_reason:
            for t, n in cat.NODES.items():
                if n.service == "voice" or t == "voice.saved":
                    out[t] = voice_reason
        if self.services.wan is None:
            out["video.lora"] = "the Wan LoRA library is not installed on this Control Center"
        if self.services.music is None:
            for t, n in cat.NODES.items():
                if n.service == "music":
                    out[t] = "gx-music is not configured"
        return out

    def catalog(self) -> dict[str, Any]:
        return cat.catalog_public(self.live_unavailable())

    def options(self) -> dict[str, Any]:
        """Dynamic choices for select fields (image models, LoRA presets, voices)."""
        out: dict[str, Any] = {"image_models": [], "edit_modes": {}, "image_sizes": {}, "lora_presets": [],
                               "voices": [], "llm_models": [m for m, _ in cat.LLM_MODELS], "errors": {}}
        catalog = self.services.image_catalog
        if catalog is not None:
            opts = catalog.options()
            out["image_default"] = {"generate": opts.get("default_generate"), "edit": opts.get("default_edit")}
            for m in opts.get("models", []):
                out["image_models"].append({"id": m["id"], "label": m["label"], "family": m.get("family"),
                                            "operations": m.get("operations", []),
                                            "description": m.get("description", ""),
                                            "qualities": m.get("qualities", []),
                                            "default_size": m.get("default_size")})
                out["image_sizes"][m["id"]] = m.get("sizes", [])
                out["edit_modes"][m["id"]] = [{"id": x["id"], "label": x["label"],
                                               "description": x.get("description", ""),
                                               "strength_applies": x.get("strength_applies", False)}
                                              for x in m.get("edit_modes", [])]
        if self.services.wan is not None:
            try:
                out["lora_presets"] = [{"id": p["id"], "name": p["name"], "description": p.get("description", ""),
                                        "builtin": p.get("builtin", False),
                                        "loras": len((p.get("data") or {}).get("loras") or [])}
                                       for p in self.services.wan.presets()]
            except Exception as exc:  # noqa: BLE001 - an options source must not break the page
                out["errors"]["lora_presets"] = str(exc)[:200]
        studio = self.services.voice()
        if studio is not None:
            try:
                out["voices"] = [{"id": v.get("id"), "name": v.get("name"), "kind": v.get("kind"),
                                  "description": v.get("description") or ""}
                                 for v in studio.list_voices(include_presets=True)]
            except Exception as exc:  # noqa: BLE001
                out["errors"]["voices"] = str(exc)[:200]
        return out

    # ================================================================ flows
    def list(self, *, owner: str | None, q: str = "", limit: int = 100) -> builtins.list[dict]:
        return self.store.list_flows(owner=owner, q=q, limit=limit)

    def get(self, flow_id: str, *, owner: str | None) -> dict:
        flow = self.store.get_flow(flow_id, owner=owner)
        flow["readiness"] = readiness(flow["graph"])
        runs = self.store.list_runs(flow_id, limit=1)
        flow["last_run"] = runs[0] if runs else None
        return flow

    def create(self, body: dict, *, owner: str, user: str) -> dict:
        template_id = body.get("template_id")
        if template_id is not None:
            tpl = self.store.get_template(str(template_id), owner=None if owner == UI_OWNER else owner)
            graph = copy.deepcopy(tpl["graph"])
            if isinstance(body.get("name"), str) and body["name"].strip():
                graph["name"] = body["name"].strip()
        else:
            graph = body.get("graph")
            if graph is None:
                graph = {"name": body.get("name") or "Untitled flow", "nodes": [], "edges": []}
        asset = body.get("asset_id")
        if asset is not None:
            graph = self._with_asset(graph, str(asset))
        try:
            clean = validate_document(graph)
        except FlowValidationError as exc:
            raise _invalid(exc) from None
        flow = self.store.create_flow(clean, owner=owner, author=user,
                                      template_id=str(template_id) if template_id else None)
        self.audit(user=user, ip="", action="flows.create", outcome="ok", flow=flow["id"],
                   template=template_id, nodes=len(clean["nodes"]))
        return self.get(flow["id"], owner=None)

    def _with_asset(self, graph: dict, asset_id: str) -> dict:
        """#/flows?asset=<id>: start with that Library item as an input node."""
        row = self.services.library.get(asset_id)
        ntype = {"image": "image.upload", "video": "video.input", "audio": "sound.upload"}[row["type"]]
        graph = copy.deepcopy(graph)
        nodes = list(graph.get("nodes") or [])
        used = {n.get("id") for n in nodes}
        nid, i = "input", 1
        while nid in used:
            i += 1
            nid = f"input_{i}"
        nodes.insert(0, {"id": nid, "type": ntype, "label": (row.get("title") or "")[:80],
                         "position": {"x": 0.0, "y": 0.0}, "config": {"asset_id": row["id"]},
                         "disabled": False, "locked": False, "notes": ""})
        graph["nodes"] = nodes
        return graph

    def check_assets(self, graph: Any, *, owner: str, asset_id: Any = None) -> None:
        """API keys may only reference Library items their own flow runs produced."""
        if owner == UI_OWNER:
            return
        ids = [asset_id] if asset_id is not None else []
        if isinstance(graph, dict):
            for node in graph.get("nodes") or []:
                if isinstance(node, dict) and isinstance(node.get("config"), dict) and node["config"].get("asset_id"):
                    ids.append(node["config"]["asset_id"])
        for aid in ids:
            if not isinstance(aid, str) or not self.store.asset_owned(aid, owner):
                raise FlowError(f"Library item {str(aid)[:40]} is not available to this key", 404, "not_found")

    def update(self, flow_id: str, body: dict, *, owner: str | None, user: str) -> dict:
        base = body.get("version")
        if isinstance(base, bool) or not isinstance(base, int):
            raise FlowError("send the version you edited (version)", 400, "invalid_request")
        current = self.store.get_flow(flow_id, owner=owner)
        try:
            clean = validate_document(body.get("graph"))
        except FlowValidationError as exc:
            raise _invalid(exc) from None
        self._check_locks(current["graph"], clean)
        updated = self.store.update_flow(flow_id, clean, base_version=base, author=user, owner=owner)
        return self.get(updated["id"], owner=None)

    @staticmethod
    def _check_locks(old: dict, new: dict) -> None:
        new_nodes = {n["id"]: n for n in new["nodes"]}
        for node in old.get("nodes", []):
            if not node.get("locked"):
                continue
            label = node.get("label") or cat.NODES.get(node["type"], cat.NODES["text.input"]).label
            now = new_nodes.get(node["id"])
            if now is None:
                raise FlowError(f"'{label}' is locked; unlock it before deleting it", 409, "locked")
            if now["type"] != node["type"] or now.get("config") != node.get("config") \
                    or bool(now.get("disabled")) != bool(node.get("disabled")):
                raise FlowError(f"'{label}' is locked; unlock it before changing its settings", 409, "locked")

    def delete(self, flow_id: str, *, owner: str | None, user: str) -> None:
        self.store.delete_flow(flow_id, owner=owner)
        self.audit(user=user, ip="", action="flows.delete", outcome="ok", flow=flow_id)

    def duplicate(self, flow_id: str, *, owner: str | None, user: str, new_owner: str) -> dict:
        src = self.store.get_flow(flow_id, owner=owner)
        graph = copy.deepcopy(src["graph"])
        graph["name"] = f"{graph['name']} (copy)"[:120]
        for n in graph["nodes"]:
            n["locked"] = False
        return self.create({"graph": graph}, owner=new_owner, user=user)

    def versions(self, flow_id: str, *, owner: str | None) -> builtins.list[dict]:
        self.store.get_flow(flow_id, owner=owner)
        return self.store.versions(flow_id)

    def version(self, flow_id: str, version: int, *, owner: str | None) -> dict:
        self.store.get_flow(flow_id, owner=owner)
        return self.store.version(flow_id, version)

    def restore(self, flow_id: str, version: int, *, owner: str | None, user: str) -> dict:
        current = self.store.get_flow(flow_id, owner=owner)
        old = self.store.version(flow_id, version)
        try:
            clean = validate_document(old["graph"])
        except FlowValidationError as exc:
            raise _invalid(exc) from None
        updated = self.store.update_flow(flow_id, clean, base_version=current["version"], author=user,
                                         owner=owner)
        self.audit(user=user, ip="", action="flows.restore", outcome="ok", flow=flow_id, version=version)
        return self.get(updated["id"], owner=None)

    # ============================================================ templates
    def templates(self, *, owner: str | None) -> builtins.list[dict]:
        return self.store.list_templates(owner=None if owner == UI_OWNER else owner)

    def template(self, tid: str, *, owner: str | None) -> dict:
        return self.store.get_template(tid, owner=None if owner == UI_OWNER else owner)

    def save_template(self, body: dict, *, owner: str, user: str) -> dict:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise FlowError("a template needs a name (1-120 characters)")
        description = body.get("description") or ""
        category = body.get("category") or "custom"
        if not isinstance(description, str) or len(description) > 2000:
            raise FlowError("description must be at most 2000 characters")
        if category not in ("custom", "ads", "audio", "music", "video", "image"):
            raise FlowError("unknown template category")
        if body.get("flow_id"):
            graph = self.store.get_flow(str(body["flow_id"]), owner=None if owner == UI_OWNER else owner)["graph"]
        else:
            graph = body.get("graph")
        try:
            clean = validate_document(graph)
        except FlowValidationError as exc:
            raise _invalid(exc) from None
        for n in clean["nodes"]:
            n["locked"] = False
        tpl = self.store.create_template(clean, owner=owner, name=name.strip(), description=description,
                                         category=category)
        self.audit(user=user, ip="", action="flows.template.create", outcome="ok", template=tpl["id"])
        return tpl

    def duplicate_template(self, tid: str, body: dict, *, owner: str, user: str) -> dict:
        src = self.template(tid, owner=owner)
        raw = body.get("name")
        name = raw if isinstance(raw, str) and raw.strip() else f"{src['name']} (copy)"
        return self.save_template({"name": name[:120], "description": src["description"],
                                   "category": "custom", "graph": src["graph"]}, owner=owner, user=user)

    def delete_template(self, tid: str, *, owner: str | None, user: str) -> None:
        self.store.delete_template(tid, owner=None if owner == UI_OWNER else owner)
        self.audit(user=user, ip="", action="flows.template.delete", outcome="ok", template=tid)

    # ================================================================== runs
    def run(self, flow_id: str, body: dict, *, owner: str | None, run_owner: str, user: str,
            allowed_aliases: set[str] | None = None) -> dict:
        mode = body.get("mode", "full")
        if mode not in RUN_MODES:
            raise FlowError(f"mode must be one of {', '.join(RUN_MODES)}")
        node_id = body.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise FlowError("node_id must be a node id")
        flow = self.store.get_flow(flow_id, owner=owner)
        expected = body.get("version")
        if expected is not None and expected != flow["version"]:
            raise FlowError(f"the flow is at version {flow['version']}; save or reload before running", 409,
                            "version_conflict")
        graph = flow["graph"]
        failed: list[str] = []
        parent = None
        if mode == "rerun_failed":
            parent = body.get("run_id")
            if not isinstance(parent, str):
                runs = self.store.list_runs(flow_id, limit=1)
                if not runs:
                    raise FlowError("this flow has not run yet")
                parent = runs[0]["id"]
            previous = self.store.get_run(parent, owner=None if run_owner == UI_OWNER else run_owner)
            if previous["flow_id"] != flow_id:
                raise FlowError("that run belongs to another flow", 404, "not_found")
            failed = [n for n, s in previous["nodes"].items() if s["status"] in FAILED_STATES]
        try:
            members, forced = plan_for(graph, mode, node_id, failed)
        except ValueError as exc:
            raise FlowError(str(exc)) from None
        if not graph["nodes"]:
            raise FlowError("the flow is empty; add nodes first")
        issues = readiness(graph, only=members)
        if issues:
            raise FlowError(f"the flow is not ready to run: {issues[0]['message']}", 422, "not_ready", issues)
        live = self.live_unavailable()
        blocked = [nid for nid in members
                   for n in graph["nodes"] if n["id"] == nid and not n.get("disabled") and n["type"] in live]
        if blocked:
            node = next(n for n in graph["nodes"] if n["id"] == blocked[0])
            raise FlowError(f"{node.get('label') or cat.NODES[node['type']].label} cannot run: "
                            f"{live[node['type']]}", 422, "unavailable")
        if allowed_aliases is not None:
            needed = sorted({a for n in graph["nodes"] if n["id"] in members and not n.get("disabled")
                             for a in cat.NODES[n["type"]].aliases})
            llm_models = {str(n["config"].get("model") or "gx-auto") for n in graph["nodes"]
                          if n["id"] in members and cat.NODES[n["type"]].service == "llm"}
            needed = sorted(set(needed) - {"gx-auto"} | llm_models)
            missing = [a for a in needed if a not in allowed_aliases]
            if missing:
                raise FlowError(f"this API key does not allow {', '.join(missing)}", 403, "forbidden")
        run_id = self.engine.start(flow=flow, graph=graph, members=members, forced=forced, mode=mode,
                                   target=node_id, owner=run_owner, user=user, parent_run=parent)
        self.audit(user=user, ip="", action="flows.run", outcome="started", flow=flow_id, run=run_id, mode=mode,
                   nodes=len(members))
        return self.store.get_run(run_id)

    def run_state(self, run_id: str, *, owner: str | None, graph: bool = False) -> dict:
        run = self.store.get_run(run_id, owner=owner, graph=graph)
        run["active"] = run["status"] in ("queued", "running")
        return run

    def runs(self, flow_id: str | None, *, owner: str | None, limit: int = 50) -> builtins.list[dict]:
        return self.store.list_runs(flow_id, owner=owner, limit=limit)

    def cancel(self, run_id: str, *, owner: str | None, user: str) -> dict:
        run = self.store.get_run(run_id, owner=owner)
        if run["status"] not in ("queued", "running"):
            raise FlowError(f"the run is already {run['status']}", 409, "not_active")
        if not self.engine.cancel(run_id):
            raise FlowError("the run is not active on this Control Center", 409, "not_active")
        self.audit(user=user, ip="", action="flows.cancel", outcome="ok", run=run_id)
        deadline = time.time() + 3
        while time.time() < deadline:
            state = self.store.get_run(run_id)
            if state["status"] not in ("queued", "running"):
                return state
            time.sleep(0.1)
        return self.store.get_run(run_id)

    def cancel_node(self, run_id: str, node_id: str, *, owner: str | None, user: str) -> dict:
        run = self.store.get_run(run_id, owner=owner)
        if node_id not in run["nodes"]:
            raise FlowError("that node is not part of the run", 404, "not_found")
        if not self.engine.cancel_node(run_id, node_id):
            raise FlowError("the run is not active", 409, "not_active")
        self.audit(user=user, ip="", action="flows.cancel_node", outcome="ok", run=run_id, node=node_id)
        return self.store.get_run(run_id)

    def node_detail(self, run_id: str, node_id: str, *, owner: str | None) -> dict:
        return self.store.node_detail(run_id, node_id, owner=owner)

    # ==================================================================== AI
    def ai_create(self, body: dict, *, user: str) -> dict:
        prompt = body.get("prompt")
        model = body.get("model") or "gx-auto"
        if not isinstance(prompt, str) or not isinstance(model, str):
            raise FlowError("prompt and model must be text")
        voices = self.options().get("voices") or []
        live = self.live_unavailable()
        available = {t: t not in live for t in cat.NODES}
        started = time.time()
        try:
            result = ai_mod.generate(self.services.llm, prompt, model=model, voices=voices, available=available)
        except NodeFailure as exc:
            self.audit(user=user, ip="", action="flows.ai_create", outcome="failed", code=exc.code)
            raise FlowError(str(exc), 502 if exc.code in ("gateway_error", "gateway_unreachable", "ai_invalid",
                                                          "empty_answer") else 400, exc.code) from None
        result["seconds"] = round(time.time() - started, 1)
        self.audit(user=user, ip="", action="flows.ai_create", outcome="ok", nodes=len(result["graph"]["nodes"]),
                   attempts=result["attempts"])
        return result

    def validate(self, graph: Any) -> dict:
        try:
            clean = validate_document(graph)
        except FlowValidationError as exc:
            return {"valid": False, "issues": exc.issues}
        return {"valid": True, "issues": [], "readiness": readiness(clean), "graph": clean}

    # ============================================================== secrets
    def secrets(self) -> builtins.list[dict]:
        return self.services.secrets.names()

    def set_secret(self, name: str, value: str, *, user: str) -> None:
        self.services.secrets.set(name, value)
        self.audit(user=user, ip="", action="flows.secret.set", outcome="ok", name=name)

    def delete_secret(self, name: str, *, user: str) -> bool:
        ok = self.services.secrets.delete(name)
        self.audit(user=user, ip="", action="flows.secret.delete", outcome="ok" if ok else "missing", name=name)
        return ok

    # ============================================================= activity
    def activity(self, user: str, since: float, limit: int) -> builtins.list[dict]:
        items = []
        for r in self.store.runs_for_activity(since, limit):
            if r["user"] != user:
                continue
            status = {"succeeded": "ok", "failed": "failed", "cancelled": "cancelled", "interrupted": "failed",
                      "running": "running", "queued": "waiting"}.get(r["status"], "waiting")
            items.append({"id": r["id"], "title": f"Flow: {r.get('flow_name') or r['flow_id']} ({r['mode']})",
                          "status": status, "at": r["created_at"],
                          "duration_ms": int(r["duration_s"] * 1000) if r.get("duration_s") else None,
                          "error": r.get("error"), "link": f"#/flows?flow={r['flow_id']}&run={r['id']}",
                          "detail": {"nodes": r["summary"].get("nodes"), "cached": r["summary"].get("cached"),
                                     "executed": r["summary"].get("executed")}})
        return items
