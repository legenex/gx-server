"""gx-call sessions on the Control Center: the authoritative side of every call.

    browser / IntakePilot --HTTP--> Control Center (this module)
        | compile agent version -> POST gx-call /v1/call/sessions (fabric, bearer key)
        | register realtime session (tunnel upstream path carries gx-call's join token)
        v
    caller audio --WS--> Playground /rt/call/<sid> --fabric--> gx-call --> engine
        ^                                                       |
        '--------- events (transcripts, tools, timings) --------'
                                                                | long-poll
    CallController thread per call <----------------------------'
        persists transcripts / tool events / metadata events,
        executes tools against the authoritative state (call_state),
        answers the tool call, pushes state/transfer events to the caller,
        finalises the result, runs post-call actions, imports recordings.

The model context is never the source of truth: every captured field is in
``call_state`` with a revision and a change log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import call_agents as ca
from . import call_intake as ci
from .netguard import BlockedURL
from .netguard import fetch as netguard_fetch

log = logging.getLogger("gx.calls")

SESSION_RE = re.compile(r"^call_[0-9a-f]{32}$")
EXTERNAL_REF_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,80}$")
TRANSFER_STATUSES = ("requested", "connected", "failed", "cancelled")
REALTIME_DISPOSITION = {"completed": "completed", "abandoned": "abandoned", "failed": "failed",
                        "timeout": "timeout", "transferred": "transferred", "preempted": "failed",
                        "dropped": "abandoned"}
#: Metadata events persisted (text-bearing events are stored in their own tables).
META_EVENTS = frozenset({"session.created", "session.hello", "session.status", "session.ready", "session.ended",
                         "user.speech.started", "user.speech.stopped", "agent.speech.started",
                         "agent.speech.stopped", "interruption.started", "interruption", "engine.stats",
                         "recording.started", "recording.ready", "tool.timeout", "error", "transfer.updated",
                         "state.updated", "integration.delivery", "notice", "client.info"})
#: Events the Control Center itself records (deliveries, imports) use seq >= this.
LOCAL_SEQ_BASE = 1_000_000_000
TEXT_KEYS = frozenset({"text", "delta", "raw", "arguments", "output", "message_text", "summary_text"})


class CallError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ------------------------------------------------------------ node-2 client --
class CallClient:
    def __init__(self, base: str, key_file: Path, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.key_file = key_file
        self.timeout = timeout

    def _key(self) -> str:
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if len(key) < 32:
            raise CallError("gx-call is not configured on this Control Center (no service key)", 503,
                            "not_configured")
        return key

    def request(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None,
                raw: bool = False) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._key()}", "Content-Type": "application/json",
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:  # noqa: S310 - fixed fabric URL
                payload = res.read()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}").get("error") or {}
            except ValueError:
                err = {}
            raise CallError(err.get("message") or f"gx-call answered HTTP {exc.code}", exc.code,
                            err.get("code") or "gx_call_error") from None
        except (OSError, ValueError) as exc:
            raise CallError("gx-call on gx10-02 is not reachable", 502, "gx_call_unreachable") from exc
        if raw:
            return payload
        return json.loads(payload or b"{}")

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=3) as res:  # noqa: S310
                return json.load(res)
        except (OSError, ValueError):
            return {"state": "unreachable"}


# ------------------------------------------------------ integration secrets --
class IntegrationSecrets:
    """Integration credentials live only on the Control Center host (0600 files)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir()):
            if p.is_file() and ca.SECRET_NAME_RE.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "updated_at": st.st_mtime, "length": st.st_size})
        return out

    def set(self, name: str, value: str) -> dict:
        if not ca.SECRET_NAME_RE.match(name or ""):
            raise CallError("secret names are lower-case letters, digits, - or _ (2-41 characters)")
        if not isinstance(value, str) or not 8 <= len(value) <= 4096 or "\n" in value.strip():
            raise CallError("the secret must be 8-4096 characters on one line")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        path = self.root / name
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value.strip())
        os.replace(tmp, path)
        return {"name": name, "stored": True}

    def delete(self, name: str) -> dict:
        if not ca.SECRET_NAME_RE.match(name or ""):
            raise CallError("invalid secret name")
        (self.root / name).unlink(missing_ok=True)
        return {"name": name, "deleted": True}

    def get(self, name: str | None) -> str | None:
        if not name or not ca.SECRET_NAME_RE.match(name):
            return None
        try:
            return (self.root / name).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None


def _meta_only(event: dict) -> dict:
    """Drop anything that can carry call content (names, answers, free text)."""
    drop = TEXT_KEYS | {"seq", "session_id", "t", "type", "state", "note"}
    if event.get("type") == "transfer.updated":
        drop = drop | {"reason"}
    return {k: v for k, v in event.items() if k not in drop}


# ------------------------------------------------------------------ manager --
class CallManager:
    def __init__(self, *, connect: Callable, agents: ca.AgentStore, client: CallClient, realtime: Any,
                 library: Any, secrets_store: IntegrationSecrets, audit: Callable[..., None],
                 metric: Callable[..., Any] | None = None, fetch: Callable[..., Any] = netguard_fetch,
                 clock: Callable[[], float] = time.time, start_threads: bool = True,
                 ffmpeg_image: str = "linuxserver/ffmpeg:latest") -> None:
        self.connect = connect
        self.agents = agents
        self.client = client
        self.realtime = realtime
        self.library = library
        self.secrets = secrets_store
        self.audit = audit
        self.metric = (lambda *a, **k: None) if metric is None else metric
        self.fetch = fetch
        self.clock = clock
        self.ffmpeg_image = ffmpeg_image
        self._lock = threading.Lock()
        self._controllers: dict[str, threading.Thread] = {}
        self._end_timers: dict[str, threading.Timer] = {}
        self.start_threads = start_threads
        if start_threads:
            threading.Thread(target=self._startup, name="gx-call-startup", daemon=True).start()

    # ------------------------------------------------------------ helpers
    def _session_row(self, sid: str) -> dict:
        if not SESSION_RE.match(sid or ""):
            raise CallError("no such call", 404, "not_found")
        with self.connect() as con:
            row = con.execute("SELECT * FROM call_sessions WHERE session_id = ?", (sid,)).fetchone()
        if row is None:
            raise CallError("no such call", 404, "not_found")
        d = dict(row)
        d["metrics"] = json.loads(d.get("metrics") or "{}")
        d["transfer"] = json.loads(d.get("transfer") or "{}")
        d["error"] = json.loads(d["error"]) if d.get("error") else None
        return d

    def _agent_for(self, row: dict) -> dict:
        return self.agents.get(row["agent_id"], row["agent_version"])

    def _update_session(self, sid: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [json.dumps(v) if k in ("metrics", "transfer", "error") and v is not None else v
                for k, v in fields.items()]
        with self.connect() as con:
            con.execute(f"UPDATE call_sessions SET {cols} WHERE session_id = ?", (*vals, sid))  # noqa: S608

    def model(self) -> dict:
        health = self.client.health()
        try:
            info = self.client.request("GET", "/v1/call/model", timeout=8)
        except CallError as exc:
            info = {"error": {"code": exc.code, "message": str(exc)}}
        return {**info, "health": health}

    # ----------------------------------------------------------- sessions
    def create_session(self, *, agent_id: str, owner: str, via: str, user_label: str,
                       version: int | None = None, record: bool | None = None, external_ref: str | None = None,
                       initial_state: dict | None = None, require_enabled: bool = False,
                       ttl_s: int = 3600) -> dict:
        agent = self.agents.get(agent_id, version)
        if agent["status"] == "archived":
            raise CallError("this agent is archived", 409, "agent_archived")
        if require_enabled and agent["status"] != "enabled":
            raise CallError("this agent is not enabled", 409, "agent_disabled")
        if external_ref is not None and not EXTERNAL_REF_RE.match(str(external_ref)):
            raise CallError("external_ref must be 1-80 characters of letters, digits, . _ : -")
        cfg = agent["config"]
        schema = cfg["structured_output_schema"]
        state = ci.empty_state(schema)
        rejected: dict = {}
        if initial_state:
            state, _changed, rejected = ci.apply_fields(schema, state, initial_state, allow_system=False)
            if rejected:
                raise CallError("initial_state: " + "; ".join(f"{k} {v}" for k, v in rejected.items()))
        want_record = cfg["recording"]["enabled"] if record is None else bool(record)
        if want_record and not cfg["recording"]["enabled"]:
            raise CallError("recording is not enabled for this agent (and needs its consent notice)", 409,
                            "recording_disabled")
        from .realtime import new_session_id  # noqa: PLC0415

        sid = new_session_id("call")
        compiled = ca.compile_for_engine(agent_id, agent["version"], cfg)
        now = self.clock()
        body = {"session_id": sid, "owner": owner.replace("user:", "u-").replace("key:", "k-")[:80],
                "agent": compiled, "record": want_record, "mode": agent["mode"],
                "max_duration_s": cfg["max_call_minutes"] * 60}
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("INSERT INTO call_sessions (session_id, agent_id, agent_version, owner, via, mode, "
                            "state, created_at, external_ref, record, retain_until) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'creating', ?, ?, ?, ?)",
                            (sid, agent_id, agent["version"], owner, via, agent["mode"], now, external_ref,
                             int(want_record), now + cfg["retention_days"] * 86400))
                con.execute("INSERT INTO call_state (session_id, schema_id, data, revision, updated_at) "
                            "VALUES (?, ?, ?, 1, ?)",
                            (sid, str(schema.get("$id") or "custom"), json.dumps(state), now))
                if initial_state:
                    con.execute("INSERT INTO call_state_changes (session_id, at, source, fields, revision) "
                                "VALUES (?, ?, 'api', ?, 1)", (sid, now, json.dumps(sorted(initial_state))))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        try:
            created = self.client.request("POST", "/v1/call/sessions", body, timeout=15)
        except CallError as exc:
            self._update_session(sid, state="ended", disposition="failed", end_reason=exc.code, ended_at=self.clock(),
                                 error={"code": exc.code, "message": str(exc)})
            raise
        upstream = created["upstream_path"]
        self.realtime.register("call", sid, owner=owner, upstream_path=upstream, ttl_s=ttl_s,
                               meta={"agent_id": agent_id, "version": agent["version"], "name": cfg["name"][:60]})
        self._update_session(sid, state=created.get("state", "created"))
        self.audit(user=user_label, action="call.session.create", outcome="ok", session=sid, agent_id=agent_id,
                   version=agent["version"], via=via)
        self._start_controller(sid)
        return {"session_id": sid, "agent_id": agent_id, "agent_version": agent["version"],
                "agent_name": cfg["name"], "state": created.get("state", "created"),
                "engine": created.get("engine"), "ws_path": f"/rt/call/{sid}", "record": want_record,
                "recording_notice": cfg["recording"]["notice"] if want_record else None,
                "state_snapshot": state, "completion": ci.completion(state, cfg["required_fields"]),
                "protocol": "gx-call.v1", "created_at": now}

    def end_session(self, sid: str, reason: str, *, user_label: str) -> dict:
        row = self._session_row(sid)
        if row["state"] != "ended":
            try:
                view = self.client.request("POST", f"/v1/call/sessions/{sid}/end", {"reason": reason}, timeout=30)
            except CallError as exc:
                if exc.status != 404:
                    raise
                self._finalize(sid, {"type": "session.ended", "reason": reason, "disposition": "failed",
                                     "error": {"code": "lost", "message": "gx-call no longer knows this call"}})
            else:
                # gx-call's answer is authoritative and already final in almost every case. Finalise from it
                # instead of waiting for the event poller, which does not run in offline mode and which can be
                # a long-poll away. _finalize is idempotent, so the poller's own session.ended is harmless.
                if isinstance(view, dict) and view.get("state") == "ended":
                    self._finalize(sid, {"type": "session.ended", "reason": view.get("end_reason") or reason,
                                         "disposition": view.get("disposition") or "completed",
                                         "summary": view.get("summary"), "error": view.get("error"),
                                         "duration_s": view.get("duration_s")})
        self.audit(user=user_label, action="call.session.end", outcome="ok", session=sid, reason=reason)
        deadline = self.clock() + 10
        while self._session_row(sid)["state"] != "ended" and self.clock() < deadline:
            time.sleep(0.2)
        return self.session_view(sid)

    # ------------------------------------------------------------ state
    def get_state(self, sid: str) -> dict:
        row = self._session_row(sid)
        with self.connect() as con:
            st = con.execute("SELECT * FROM call_state WHERE session_id = ?", (sid,)).fetchone()
        if st is None:
            raise CallError("the call content was deleted (retention)", 410, "purged")
        agent = self._agent_for(row)
        data = json.loads(st["data"])
        return {"session_id": sid, "schema_id": st["schema_id"], "revision": st["revision"],
                "updated_at": st["updated_at"], "data": data,
                "completion": ci.completion(data, agent["config"]["required_fields"])}

    def update_state(self, sid: str, fields: object, *, source: str, allow_system: bool = False,
                     base_revision: int | None = None) -> dict:
        row = self._session_row(sid)
        agent = self._agent_for(row)
        schema = agent["config"]["structured_output_schema"]
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                st = con.execute("SELECT * FROM call_state WHERE session_id = ?", (sid,)).fetchone()
                if st is None:
                    raise CallError("the call content was deleted (retention)", 410, "purged")
                if base_revision is not None and base_revision != st["revision"]:
                    raise CallError(f"the state changed meanwhile (revision {st['revision']})", 409, "state_conflict")
                current = json.loads(st["data"])
                try:
                    new, changed, rejected = ci.apply_fields(schema, current, fields, allow_system=allow_system)
                except ci.StateError as exc:
                    raise CallError(str(exc)) from None
                revision = st["revision"]
                if changed:
                    revision += 1
                    con.execute("UPDATE call_state SET data = ?, revision = ?, updated_at = ? WHERE session_id = ?",
                                (json.dumps(new), revision, self.clock(), sid))
                    con.execute("INSERT INTO call_state_changes (session_id, at, source, fields, revision) "
                                "VALUES (?, ?, ?, ?, ?)", (sid, self.clock(), source, json.dumps(changed), revision))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        comp = ci.completion(new, agent["config"]["required_fields"])
        result = {"data": new, "changed": changed, "rejected": rejected, "revision": revision, "completion": comp}
        if changed and row["state"] != "ended":
            self._push(sid, {"type": "state.updated", "revision": revision, "changed": changed,
                             "state": new, "completion": comp, "source": source})
            if not comp["missing"] and any(f in agent["config"]["required_fields"] for f in changed):
                self._deliver_async(sid, agent, "intake.completed")
            elif agent["config"]["webhooks"]:
                self._deliver_async(sid, agent, "intake.updated")
        return result

    def _push(self, sid: str, event: dict) -> None:
        try:
            self.client.request("POST", f"/v1/call/sessions/{sid}/events", event, timeout=5)
        except CallError as exc:
            log.info("could not push %s to %s: %s", event.get("type"), sid, exc)

    # --------------------------------------------------------- transfer
    def transfer(self, sid: str, status: str, *, note: str = "", source: str, user_label: str,
                 reason: str = "", summary: str = "") -> dict:
        if status not in TRANSFER_STATUSES:
            raise CallError(f"status must be one of {', '.join(TRANSFER_STATUSES)}")
        row = self._session_row(sid)
        current = row["transfer"] or {}
        if status != "requested" and current.get("status") != "requested":
            raise CallError("no transfer is pending for this call", 409, "no_transfer")
        if status == "requested" and current.get("status") == "requested":
            return current
        agent = self._agent_for(row)
        dest = agent["config"]["transfer_destination"]
        now = self.clock()
        history = list(current.get("history", []))
        history.append({"status": status, "at": now, "source": source, "note": note[:200]})
        transfer = {**current, "status": status, "updated_at": now, "history": history[-20:],
                    "destination": {"type": dest["type"], "value": dest["value"]}}
        if status == "requested":
            transfer.update(requested_at=now, reason=reason[:300], summary=summary[:600])
        self._update_session(sid, transfer=transfer)
        try:
            self.update_state(sid, {"transfer_status": status}, source=source, allow_system=True)
        except CallError:
            pass  # a custom schema without transfer_status
        event = {"type": "transfer.updated", "status": status, "destination": transfer["destination"],
                 "reason": transfer.get("reason"), "note": note[:200] or None}
        if row["state"] != "ended":
            self._push(sid, event)
        if status == "requested":
            integ = dest.get("integration")
            if integ:
                self._deliver_async(sid, agent, "transfer.requested", only=integ,
                                    extra={"transfer": {k: transfer.get(k) for k in ("reason", "summary",
                                                                                     "destination")}})
        if status == "connected" and row["state"] != "ended":
            try:
                self.client.request("POST", f"/v1/call/sessions/{sid}/end", {"reason": "transferred"}, timeout=30)
            except CallError as exc:
                log.warning("could not end transferred call %s: %s", sid, exc)
        self.audit(user=user_label, action=f"call.transfer.{status}", outcome="ok", session=sid)
        return transfer

    # ------------------------------------------------------------ tools
    def execute_tool(self, sid: str, name: str, args: object) -> tuple[bool, str, dict]:
        """Run one model tool call. Returns (ok, ASCII output for the model, event extras)."""
        row = self._session_row(sid)
        agent = self._agent_for(row)
        cfg = agent["config"]
        if name not in cfg["tool_permissions"]:
            return False, json.dumps({"error": f"{name} is not allowed for this agent"}), {}
        args = args if isinstance(args, dict) else {}
        if name == "update_intake_fields":
            fields = args.get("fields") if isinstance(args.get("fields"), dict) else \
                {k: v for k, v in args.items() if k != "fields"}
            try:
                res = self.update_state(sid, fields, source="tool", allow_system=True)
            except CallError as exc:
                return False, f"Could not save: {exc}.", {}
            text = ci.speakable(res["data"], res["changed"], res["rejected"], res["completion"]["missing"])
            return not res["rejected"], text, {"changed": res["changed"], "rejected": res["rejected"],
                                               "completion": res["completion"], "state": res["data"],
                                               "revision": res["revision"]}
        if name == "check_business_hours":
            status = ci.hours_status(cfg["business_hours"])
            if not status["configured"]:
                text = "No business hours are configured; the office line is treated as open."
            elif status["open"]:
                text = f"The office is open now ({status['local_time']} {status['timezone']})."
            else:
                text = (f"The office is closed now ({status['local_time']} {status['timezone']}). "
                        + (f"It opens {status['next_open_spoken']}." if status["next_open_spoken"] else ""))
            return True, text, {"hours": status}
        if name == "lookup_accident_state_rules":
            state_arg = str(args.get("state") or "")
            date_arg = args.get("accident_date")
            if not date_arg:
                date_arg = self.get_state(sid)["data"].get("accident_date")
            try:
                info = ci.state_rules(state_arg, str(date_arg) if date_arg else None)
            except ci.StateError as exc:
                return False, f"Lookup failed: {exc}. Ask the caller which US state the accident happened in.", {}
            return True, ci.state_rules_sentence(info), {"rules": info}
        if name == "request_warm_transfer":
            dest = cfg["transfer_destination"]
            try:
                self.transfer(sid, "requested", source="tool", user_label="gx-call",
                              reason=str(args.get("reason") or "")[:300], summary=str(args.get("summary") or "")[:600])
            except CallError as exc:
                return False, f"The transfer could not be requested: {exc}.", {}
            where = {"queue": "the specialist team", "phone": "a specialist", "webhook": "a specialist"}[dest["type"]]
            return True, (f"Transfer requested to {where}. Tell the caller you are connecting them now and ask them "
                          "to stay on the line."), {"transfer": "requested"}
        if name == "send_webhook":
            hooks = [h for h in cfg["webhooks"] if h["enabled"]]
            subscribed = [h for h in hooks if "tool.send_webhook" in h["events"]] or hooks
            results = [self.deliver(sid, agent, h, "tool.send_webhook",
                                    extra={"reason": str(args.get("reason") or "")[:300]}, attempts=1)
                       for h in subscribed]
            ok = bool(results) and all(r["ok"] for r in results)
            return ok, ("The record was sent to the office system." if ok else
                        "Sending the record failed; tell the caller a colleague will follow up."), {
                "deliveries": [{k: r[k] for k in ("name", "ok", "status")} for r in results]}
        if name == "end_call":
            outcome = str(args.get("outcome") or "").strip().lower().replace(" ", "_")
            fields = ci.schema_fields(cfg["structured_output_schema"])
            if "disposition" in fields and outcome in fields["disposition"].get("enum", []):
                try:
                    self.update_state(sid, {"disposition": outcome}, source="tool", allow_system=True)
                except CallError:
                    pass
            self._schedule_end(sid, 6.0, "agent_ended")
            return True, "The call will end in a few seconds. Say a short goodbye now.", {"ending_in_s": 6}
        return False, json.dumps({"error": f"unknown tool {name}"}), {}

    def _schedule_end(self, sid: str, delay: float, reason: str) -> None:
        def fire() -> None:
            try:
                self.client.request("POST", f"/v1/call/sessions/{sid}/end", {"reason": reason}, timeout=30)
            except CallError as exc:
                log.info("scheduled end of %s failed: %s", sid, exc)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        with self._lock:
            old = self._end_timers.pop(sid, None)
            if old:
                old.cancel()
            self._end_timers[sid] = timer
        timer.start()

    # ------------------------------------------------------- integrations
    def payload(self, sid: str, agent: dict, event: str, extra: dict | None = None) -> dict:
        row = self._session_row(sid)
        try:
            state = self.get_state(sid)
        except CallError:
            state = {"data": {}, "completion": {}}
        return {"event": event, "delivery_id": str(uuid.uuid4()), "sent_at": self.clock(),
                "session": {"session_id": sid, "external_ref": row["external_ref"], "state": row["state"],
                            "disposition": row["disposition"], "created_at": row["created_at"],
                            "ended_at": row["ended_at"], "duration_s": row["duration_s"]},
                "agent": {"agent_id": agent["agent_id"], "version": agent["version"],
                          "name": agent["config"]["name"], "use_case": agent["config"]["use_case"]},
                "intake": state["data"], "completion": state.get("completion"),
                "transfer": {k: row["transfer"].get(k) for k in ("status", "reason", "summary", "destination")}
                if row["transfer"] else None, **(extra or {})}

    def deliver(self, sid: str, agent: dict, integ: dict, event: str, *, extra: dict | None = None,
                attempts: int = 3, shape: str = "event") -> dict:
        body_obj = self.payload(sid, agent, event, extra)
        if shape == "leaddistro":
            body_obj = {"source": "gx-call", "campaign": agent["config"]["leaddistro"].get("campaign"),
                        "lead": body_obj["intake"], "session": body_obj["session"], "agent": body_obj["agent"],
                        "delivery_id": body_obj["delivery_id"]}
        elif shape == "crm":
            body_obj = {"object": "crm.contact", "contact": {k: body_obj["intake"].get(k) for k in
                                                             ("caller_name", "phone", "email")},
                        "intake": body_obj["intake"], "session": body_obj["session"],
                        "agent": body_obj["agent"], "delivery_id": body_obj["delivery_id"]}
        body = json.dumps(body_obj, separators=(",", ":")).encode()
        ts = str(int(self.clock()))
        headers = {"Content-Type": "application/json", "X-GX-Event": event,
                   "X-GX-Delivery": body_obj["delivery_id"], "X-GX-Timestamp": ts}
        secret = self.secrets.get(integ.get("secret_ref"))
        if secret:
            headers["X-GX-Signature"] = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body,
                                                            hashlib.sha256).hexdigest()
        result = {"name": integ["name"], "event": event, "ok": False, "status": None, "attempts": 0}
        delay = 2.0
        for attempt in range(1, attempts + 1):
            result["attempts"] = attempt
            t0 = self.clock()
            try:
                res = self.fetch(integ["url"], method="POST", body=body, headers=headers, timeout=6.0,
                                 max_bytes=65536, allow_http=False)
                result["status"] = res.status
                result["ok"] = 200 <= res.status < 300
                retry = res.status >= 500 or res.status == 429
            except BlockedURL as exc:
                result["error"] = str(exc)[:160]
                retry = False
            except OSError as exc:
                result["error"] = type(exc).__name__
                retry = True
            result["ms"] = round((self.clock() - t0) * 1000)
            if result["ok"] or not retry or attempt == attempts:
                break
            time.sleep(delay)
            delay *= 4
        self._store_event(sid, {"type": "integration.delivery", **{k: result.get(k) for k in
                                                                    ("name", "event", "ok", "status", "attempts",
                                                                     "ms", "error")}})
        return result

    def _deliver_async(self, sid: str, agent: dict, event: str, *, only: str | None = None,
                       extra: dict | None = None) -> None:
        hooks = [h for h in agent["config"]["webhooks"] + agent["config"]["crm"]
                 if h["enabled"] and (only is None or h["name"] == only)
                 and (only is not None or event in h.get("events", []))]
        if only is not None and agent["config"]["leaddistro"].get("integration", {}) and \
                agent["config"]["leaddistro"]["integration"]["name"] == only:
            hooks.append(agent["config"]["leaddistro"]["integration"])
        for h in hooks:
            threading.Thread(target=self.deliver, args=(sid, agent, h, event), kwargs={"extra": extra},
                             name="gx-call-webhook", daemon=True).start()

    # ------------------------------------------------------ controller
    def _start_controller(self, sid: str) -> None:
        if not self.start_threads:
            return
        with self._lock:
            t = self._controllers.get(sid)
            if t and t.is_alive():
                return
            t = threading.Thread(target=self._controller, args=(sid,), name=f"call-ctl-{sid[-6:]}", daemon=True)
            self._controllers[sid] = t
        t.start()

    def _controller(self, sid: str) -> None:
        cursor = self._session_row(sid)["event_cursor"]
        failures = 0
        while True:
            try:
                page = self.client.request("GET", f"/v1/call/sessions/{sid}/events?after={cursor}&wait=25",
                                           timeout=40)
                failures = 0
            except CallError as exc:
                failures += 1
                if exc.status == 404 or failures > 30:
                    self._finalize(sid, {"type": "session.ended", "reason": "lost", "disposition": "failed",
                                         "error": {"code": "gx_call_lost",
                                                   "message": "gx-call no longer knows this call"}})
                    return
                time.sleep(min(10, 0.5 * failures))
                continue
            for event in page.get("events", []):
                try:
                    self.handle_event(sid, event)
                except Exception:  # noqa: BLE001 - one bad event must not stop the call
                    log.exception("event %s of %s failed", event.get("type"), sid)
                cursor = max(cursor, int(event.get("seq", cursor)))
            self._update_session(sid, event_cursor=cursor)
            if page.get("state") == "ended" and not page.get("events"):
                return
            if self._session_row(sid)["state"] == "ended":
                return

    def handle_event(self, sid: str, event: dict) -> None:
        kind = event.get("type")
        at = (event.get("t") or self.clock() * 1000) / 1000.0
        seq = int(event.get("seq") or 0)
        if kind in ("transcript.user.final", "transcript.agent.final"):
            text = str(event.get("text") or "").strip()
            if text:
                with self.connect() as con:
                    con.execute("INSERT OR IGNORE INTO call_transcripts (session_id, seq, speaker, turn, text, "
                                "stream_ms, at, interrupted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (sid, seq, "caller" if kind == "transcript.user.final" else "agent",
                                 event.get("utterance", event.get("turn")), text[:4000], event.get("stream_ms"), at,
                                 int(bool(event.get("interrupted")))))
            return
        if kind == "tool.call":
            call_id = str(event.get("call_id") or "")
            name = str(event.get("name") or "")
            args = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
            with self.connect() as con:
                cur = con.execute("INSERT OR IGNORE INTO call_tool_events (session_id, call_id, name, arguments, at) "
                                  "VALUES (?, ?, ?, ?, ?)", (sid, call_id, name[:60], json.dumps(args)[:8000], at))
                fresh = cur.rowcount == 1
            if not fresh or not event.get("known", True):
                if not event.get("known", True):
                    self._finish_tool(sid, call_id, False, "unknown tool", 0, error="unknown_tool")
                return
            threading.Thread(target=self._run_tool, args=(sid, call_id, name, args), name="gx-call-tool",
                             daemon=True).start()
            return
        if kind == "tool.result":
            return  # already recorded by _run_tool (this is gx-call's echo)
        if kind in META_EVENTS:
            self._store_event(sid, event, seq=seq)
        if kind == "session.status":
            state = event.get("state")
            if state and state != "ended":
                self._update_session(sid, state=state)
        elif kind == "session.ready":
            self._update_session(sid, state="live", live_at=at)
        elif kind in ("agent.speech.started", "interruption", "engine.stats", "tool.timeout"):
            self._metrics_update(sid, event)
        elif kind == "session.ended":
            self._finalize(sid, event)

    def _run_tool(self, sid: str, call_id: str, name: str, args: dict) -> None:
        t0 = self.clock()
        try:
            ok, output, extra = self.execute_tool(sid, name, args)
            error = None if ok else "tool_reported_failure"
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            ok, output, extra, error = False, "The tool failed. Apologise and offer a callback.", {}, type(exc).__name__
        latency = round((self.clock() - t0) * 1000)
        extra_event = {"tool_ms": latency, **{k: v for k, v in extra.items()
                                              if k in ("changed", "rejected", "completion", "state", "revision",
                                                       "transfer", "hours", "rules", "deliveries", "ending_in_s")}}
        try:
            self.client.request("POST", f"/v1/call/sessions/{sid}/tool-results",
                                {"call_id": call_id, "output": _ascii(output)[:1900], "ok": ok, "event": extra_event},
                                timeout=10)
        except CallError as exc:
            error = error or exc.code
        self._finish_tool(sid, call_id, ok, output, latency, error=error)
        self.metric("realtime.latency", service="gx-call", session_id=sid, stage="tool", ms=latency,
                    outcome="ok" if ok else "failed")

    def _finish_tool(self, sid: str, call_id: str, ok: bool, output: str, latency: int, error: str | None) -> None:
        with self.connect() as con:
            con.execute("UPDATE call_tool_events SET result = ?, ok = ?, latency_ms = ?, error = ?, finished_at = ? "
                        "WHERE session_id = ? AND call_id = ?",
                        (output[:4000], int(ok), latency, error, self.clock(), sid, call_id))

    def _store_event(self, sid: str, event: dict, seq: int | None = None) -> None:
        data = json.dumps(_meta_only(event))[:8000]
        with self.connect() as con:
            if seq:
                con.execute("INSERT OR IGNORE INTO call_events (session_id, seq, type, at, data) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (sid, seq, event.get("type"), (event.get("t") or self.clock() * 1000) / 1000.0, data))
            else:
                last = con.execute("SELECT COALESCE(MAX(seq), 0) FROM call_events WHERE session_id = ? AND seq >= ?",
                                   (sid, LOCAL_SEQ_BASE)).fetchone()[0]
                con.execute("INSERT INTO call_events (session_id, seq, type, at, data) VALUES (?, ?, ?, ?, ?)",
                            (sid, max(LOCAL_SEQ_BASE, int(last)) + 1, event.get("type"), self.clock(), data))

    def _metrics_update(self, sid: str, event: dict) -> None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT metrics FROM call_sessions WHERE session_id = ?", (sid,)).fetchone()
                m = json.loads(row["metrics"] or "{}") if row else {}
                kind = event.get("type")
                if kind == "agent.speech.started":
                    if event.get("first_audio_wall_ms") is not None:
                        m["first_audio_wall_ms"] = event["first_audio_wall_ms"]
                    if event.get("turn_latency_ms") is not None:
                        m.setdefault("turn_latency_ms", []).append(event["turn_latency_ms"])
                        m.setdefault("turn_latency_wall_ms", []).append(event.get("turn_latency_wall_ms"))
                elif kind == "interruption":
                    m["interruptions"] = m.get("interruptions", 0) + (1 if event.get("yielded") else 0)
                    if event.get("latency_ms") is not None:
                        m.setdefault("interruption_latency_ms", []).append(event["latency_ms"])
                        m.setdefault("interruption_latency_wall_ms", []).append(event.get("latency_wall_ms"))
                elif kind == "engine.stats":
                    m["rtf"] = event.get("rtf_session")
                    m["step_ms_p95"] = event.get("step_ms_p95")
                    m["backlog_ms_max"] = max(m.get("backlog_ms_max", 0), event.get("backlog_ms") or 0)
                    m["realtime"] = event.get("realtime")
                elif kind == "tool.timeout":
                    m["tool_timeouts"] = m.get("tool_timeouts", 0) + 1
                for key in ("turn_latency_ms", "turn_latency_wall_ms", "interruption_latency_ms",
                            "interruption_latency_wall_ms"):
                    if isinstance(m.get(key), list):
                        m[key] = m[key][-200:]
                con.execute("UPDATE call_sessions SET metrics = ? WHERE session_id = ?", (json.dumps(m), sid))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    # -------------------------------------------------------- finalize
    def _finalize(self, sid: str, event: dict) -> None:
        row = self._session_row(sid)
        if row["state"] == "ended":
            return
        now = self.clock()
        disposition = event.get("disposition") or "completed"
        summary = event.get("summary") or {}
        metrics = dict(row["metrics"])
        for key in ("first_audio_wall_ms", "turn_latency_ms", "turn_latency_wall_ms", "interruption_latency_ms",
                    "interruptions", "stream_ms", "rtf_session", "step_ms_mean", "step_ms_p95", "agent_turns",
                    "user_utterances"):
            if summary.get(key) is not None:
                metrics[f"summary_{key}"] = summary[key]
        live_at = row["live_at"]
        duration = event.get("duration_s") if event.get("duration_s") is not None else \
            (round(now - live_at, 1) if live_at else 0.0)
        with self.connect() as con:
            cur = con.execute("UPDATE call_sessions SET state = 'ended', disposition = ?, end_reason = ?, "
                              "ended_at = ?, duration_s = ?, metrics = ?, error = ? "
                              "WHERE session_id = ? AND state != 'ended'",
                              (disposition, event.get("reason"), now, duration, json.dumps(metrics),
                               json.dumps(event["error"]) if event.get("error") else None, sid))
            finalised_here = cur.rowcount == 1
        if not finalised_here:
            return  # another thread (poller, explicit end or timer) finalised this call first
        try:
            self.realtime.end(sid, REALTIME_DISPOSITION.get(disposition, "completed"))
        except Exception:  # noqa: BLE001 - the registry may not know it (restart)
            log.info("realtime registry did not know %s when it ended", sid)
        with self._lock:
            timer = self._end_timers.pop(sid, None)
        if timer:
            timer.cancel()
        result = self._write_result(sid, disposition)
        agent = self._agent_for(row)
        self.metric("call.disposition", session_id=sid, agent_id=row["agent_id"], disposition=disposition,
                    turns=summary.get("agent_turns"), outcome="failed" if event.get("error") else "ok")
        threading.Thread(target=self._after_call, args=(sid, agent, result), name="gx-call-post",
                         daemon=True).start()

    def _write_result(self, sid: str, disposition: str) -> dict:
        row = self._session_row(sid)
        agent = self._agent_for(row)
        try:
            state = self.get_state(sid)
        except CallError:
            state = {"data": {}, "completion": {"missing": [], "ratio": None}}
        data = state["data"]
        intake_disp = data.get("disposition")
        if intake_disp in (None, "in_progress"):
            if disposition == "transferred":
                intake_disp = "transferred"
            elif disposition in ("abandoned", "failed", "timeout"):
                intake_disp = "abandoned"
            elif not state["completion"]["missing"]:
                intake_disp = "intake_complete"
            else:
                intake_disp = "abandoned"
        with self.connect() as con:
            tools = con.execute("SELECT name, ok FROM call_tool_events WHERE session_id = ?", (sid,)).fetchall()
            turns = con.execute("SELECT COUNT(*) FROM call_transcripts WHERE session_id = ?", (sid,)).fetchone()[0]
        summary = (f"{agent['config']['name']} v{row['agent_version']}: call {disposition}, intake {intake_disp}, "
                   f"{state['completion'].get('filled', 0)}/{len(state['completion'].get('required', []))} required "
                   f"fields, {len(tools)} tool calls, {turns} transcript turns, "
                   f"transfer {row['transfer'].get('status', 'none') if row['transfer'] else 'none'}.")
        result = {"session_id": sid, "disposition": disposition, "intake_disposition": intake_disp,
                  "qualification_status": data.get("qualification_status"),
                  "completion": state["completion"].get("ratio"),
                  "missing_required": state["completion"].get("missing", []),
                  "structured": data, "summary": summary}
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO call_results (session_id, disposition, qualification_status, "
                        "completion, missing_required, structured, summary, post_call, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT post_call FROM call_results WHERE "
                        "session_id = ?), '[]'), ?)",
                        (sid, intake_disp, data.get("qualification_status"), result["completion"],
                         json.dumps(result["missing_required"]), json.dumps(data), summary, sid, self.clock()))
        return result

    def _after_call(self, sid: str, agent: dict, result: dict) -> None:
        row = self._session_row(sid)
        outcomes = []
        cfg = agent["config"]
        state_disp = result.get("intake_disposition")
        for act in cfg["post_call_actions"]:
            when = act["when"]
            if when == "completed" and row["disposition"] != "completed":
                continue
            if when == "qualified" and result.get("qualification_status") != "qualified":
                continue
            if when == "intake_complete" and state_disp != "intake_complete":
                continue
            if when == "transferred" and row["disposition"] != "transferred":
                continue
            if act["type"] == "leaddistro":
                integ, shape = cfg["leaddistro"].get("integration"), "leaddistro"
            else:
                integ = next((x for x in cfg["webhooks"] + cfg["crm"] if x["name"] == act["target"]), None)
                shape = "crm" if act["type"] == "crm" else "event"
            if not integ or not integ.get("enabled", True):
                outcomes.append({"action": act, "ok": False, "error": "integration missing or disabled"})
                continue
            res = self.deliver(sid, agent, integ, "call.ended", shape=shape)
            outcomes.append({"action": act, **{k: res.get(k) for k in ("ok", "status", "attempts", "error")}})
        for h in cfg["webhooks"]:
            if h["enabled"] and "call.ended" in h["events"] and not any(
                    o["action"].get("target") == h["name"] for o in outcomes):
                self.deliver(sid, agent, h, "call.ended")
        if outcomes:
            with self.connect() as con:
                con.execute("UPDATE call_results SET post_call = ? WHERE session_id = ?", (json.dumps(outcomes), sid))
        if row["record"]:
            self._import_recording(sid, agent)

    # -------------------------------------------------------- recording
    def _import_recording(self, sid: str, agent: dict) -> None:
        from .media_library import NewAsset  # noqa: PLC0415

        work = Path(self.library.root) / "tmp" / f"rec-{sid}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            deadline = self.clock() + 60
            while True:
                try:
                    view = self.client.request("GET", f"/v1/call/sessions/{sid}", timeout=10)
                except CallError:
                    return
                rec = view.get("recording") or {}
                if rec.get("ready") or self.clock() > deadline:
                    break
                time.sleep(2)
            if not rec.get("ready"):
                return
            for track in ("caller", "agent"):
                data = self.client.request("GET", f"/v1/call/sessions/{sid}/recording?track={track}", raw=True,
                                           timeout=120)
                (work / f"{track}.wav").write_bytes(data)
            mixed = self.mix_recording(work)
            if mixed is None:
                return
            tmp = self.library.tmp_file(".wav")
            shutil.copyfile(mixed, tmp)
            size = mixed.stat().st_size
            asset = self.library.add(NewAsset(
                type="audio", ext="wav", operation="recording", data_path=tmp,
                title=f"Call recording: {agent['config']['name']} ({sid[-8:]})",
                model_alias="gx-call", duration=round((size - 44) / 4 / 22050, 2), sample_rate=22050, channels=2,
                settings={"session_id": sid, "agent_id": agent["agent_id"], "agent_version": agent["version"],
                          "tracks": "left: caller, right: agent",
                          "consent_notice": agent["config"]["recording"]["notice"]},
                source_kind="call_session", source_ref=sid, is_test=agent.get("mode") == "test",
                tags=["call-recording"]))
            self._update_session(sid, recording_asset_id=asset["id"])
            self._store_event(sid, {"type": "recording.ready", "asset_id": asset["id"]})
            try:
                self.client.request("DELETE", f"/v1/call/sessions/{sid}/recording", timeout=10)
            except CallError:
                pass
        except Exception:  # noqa: BLE001
            log.exception("recording import failed for %s", sid)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def mix_recording(self, work: Path) -> Path | None:
        """caller.wav (16 kHz) + agent.wav (22.05 kHz) -> call.wav (stereo, 22.05 kHz).

        ffmpeg runs in the Library's network-less tools image (same as MediaTools)."""
        os.chmod(work, 0o777)  # noqa: S103 - a private temp dir the ffmpeg container runs against as another uid
        res = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--memory", "1g", "--cpus", "2",
             "-v", f"{work}:/m", "--entrypoint", "ffmpeg", self.ffmpeg_image, "-v", "error", "-y",
             "-i", "/m/caller.wav", "-i", "/m/agent.wav", "-filter_complex",
             "[0:a]aresample=22050[c];[c][1:a]amerge=inputs=2[a]", "-map", "[a]", "-ac", "2",
             "-c:a", "pcm_s16le", "/m/call.wav"],
            capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        mixed = work / "call.wav"
        if res.returncode != 0 or not mixed.is_file():
            log.error("recording mix failed: %s", res.stderr[-400:])
            return None
        return mixed

    # ------------------------------------------------------------ views
    def session_view(self, sid: str, *, include_content: bool = True) -> dict:
        row = self._session_row(sid)
        out = {k: row[k] for k in ("session_id", "agent_id", "agent_version", "via", "mode", "state",
                                   "disposition", "end_reason", "created_at", "live_at", "ended_at",
                                   "duration_s", "external_ref", "record", "recording_asset_id",
                                   "content_purged")}
        out["object"] = "call.session"
        out["error"] = row["error"]
        out["transfer"] = row["transfer"] or {"status": "none"}
        out["metrics"] = summarize_metrics(row["metrics"])
        try:
            agent = self._agent_for(row)
            out["agent_name"] = agent["config"]["name"]
        except ca.AgentError:
            out["agent_name"] = None
        if include_content and not row["content_purged"]:
            with self.connect() as con:
                out["transcript"] = [dict(r) for r in con.execute(
                    "SELECT seq, speaker, turn, text, stream_ms, at, interrupted FROM call_transcripts "
                    "WHERE session_id = ? ORDER BY seq", (sid,))]
                out["tools"] = [
                    {**dict(r), "arguments": json.loads(r["arguments"] or "{}")} for r in con.execute(
                        "SELECT call_id, name, arguments, result, ok, latency_ms, error, at, finished_at "
                        "FROM call_tool_events WHERE session_id = ? ORDER BY id", (sid,))]
                res = con.execute("SELECT * FROM call_results WHERE session_id = ?", (sid,)).fetchone()
            try:
                out["intake"] = self.get_state(sid)
            except CallError:
                out["intake"] = None
            if res is not None:
                r = dict(res)
                out["result"] = {"disposition": row["disposition"], "intake_disposition": r["disposition"],
                                 "qualification_status": r["qualification_status"], "completion": r["completion"],
                                 "missing_required": json.loads(r["missing_required"]),
                                 "structured": json.loads(r["structured"]), "summary": r["summary"],
                                 "post_call": json.loads(r["post_call"])}
            else:
                out["result"] = None
        return out

    def events(self, sid: str, after: int = 0, limit: int = 500) -> dict:
        """Persisted feed (metadata events + transcript lines), ordered by time.

        ``after`` is a gx-call sequence number. Events the Control Center
        recorded itself (integration deliveries, recording imports) have
        ``seq >= 1000000000`` and are always included.
        """
        self._session_row(sid)
        with self.connect() as con:
            items = [{"seq": r["seq"], "type": r["type"], "at": r["at"], **json.loads(r["data"])}
                     for r in con.execute("SELECT * FROM call_events WHERE session_id = ? AND (seq > ? OR seq >= ?) "
                                          "ORDER BY seq LIMIT ?", (sid, after, LOCAL_SEQ_BASE, limit))]
            items += [{"seq": r["seq"], "type": f"transcript.{r['speaker']}", "at": r["at"], "text": r["text"],
                       "turn": r["turn"], "interrupted": bool(r["interrupted"])}
                      for r in con.execute("SELECT * FROM call_transcripts WHERE session_id = ? AND seq > ? "
                                           "ORDER BY seq LIMIT ?", (sid, after, limit))]
        items.sort(key=lambda e: (e["at"], e["seq"]))
        node = [e["seq"] for e in items if e["seq"] < LOCAL_SEQ_BASE]
        return {"events": items[:limit], "next": max(node) if node else after}

    def list_sessions(self, *, owner: str | None = None, agent_id: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT session_id FROM call_sessions"
        where: list[str] = []
        args: list[Any] = []
        if owner:
            where.append("owner = ?")
            args.append(owner)
        if agent_id:
            where.append("agent_id = ?")
            args.append(agent_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self.connect() as con:
            ids = [r[0] for r in con.execute(sql, args)]
        return [self.session_view(s, include_content=False) for s in ids]

    def owner_of(self, sid: str) -> str:
        return self._session_row(sid)["owner"]

    def compare(self, a: tuple[str, int], b: tuple[str, int]) -> dict:
        va = self.agents.get(a[0], a[1])
        vb = self.agents.get(b[0], b[1])
        return {"a": {"agent_id": a[0], "version": va["version"], "name": va["config"]["name"],
                      "metrics": self.version_metrics(a[0], va["version"])},
                "b": {"agent_id": b[0], "version": vb["version"], "name": vb["config"]["name"],
                      "metrics": self.version_metrics(b[0], vb["version"])},
                "diff": ca.diff_configs(va["config"], vb["config"]),
                "prompt_diff": ca.diff_configs({"compiled_prompt": ca.compile_prompt(va["config"])},
                                               {"compiled_prompt": ca.compile_prompt(vb["config"])})}

    def version_metrics(self, agent_id: str, version: int) -> dict:
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT s.session_id, s.disposition, s.duration_s, s.metrics, r.completion, r.qualification_status "
                "FROM call_sessions s LEFT JOIN call_results r ON r.session_id = s.session_id "
                "WHERE s.agent_id = ? AND s.agent_version = ? AND s.state = 'ended'", (agent_id, version))]
            tool_rows = con.execute(
                "SELECT t.ok, t.latency_ms FROM call_tool_events t JOIN call_sessions s ON s.session_id = t.session_id "
                "WHERE s.agent_id = ? AND s.agent_version = ?", (agent_id, version)).fetchall()
        first, turn, turn_wall, intr = [], [], [], []
        for r in rows:
            m = json.loads(r["metrics"] or "{}")
            if m.get("first_audio_wall_ms") is not None:
                first.append(m["first_audio_wall_ms"])
            turn += [x for x in m.get("turn_latency_ms", []) if x is not None]
            turn_wall += [x for x in m.get("turn_latency_wall_ms", []) if x is not None]
            intr += [x for x in m.get("interruption_latency_ms", []) if x is not None]
        dispositions: dict[str, int] = {}
        for r in rows:
            dispositions[r["disposition"] or "unknown"] = dispositions.get(r["disposition"] or "unknown", 0) + 1
        completions = [r["completion"] for r in rows if r["completion"] is not None]
        return {"calls": len(rows), "dispositions": dispositions,
                "avg_duration_s": round(sum(r["duration_s"] or 0 for r in rows) / len(rows), 1) if rows else None,
                "avg_completion": round(sum(completions) / len(completions), 3) if completions else None,
                "qualified": sum(1 for r in rows if r["qualification_status"] == "qualified"),
                "first_audio_wall_ms": _stats(first), "turn_latency_ms": _stats(turn),
                "turn_latency_wall_ms": _stats(turn_wall), "interruption_latency_ms": _stats(intr),
                "tool_calls": len(tool_rows), "tool_failures": sum(1 for t in tool_rows if t["ok"] == 0),
                "tool_latency_ms": _stats([t["latency_ms"] for t in tool_rows if t["latency_ms"] is not None])}

    def activity(self, user: str, since: float, limit: int) -> list[dict]:
        owner = f"user:{user}"
        with self.connect() as con:
            rows = con.execute("SELECT s.session_id, s.state, s.disposition, s.created_at, s.duration_s, s.agent_id, "
                               "a.name FROM call_sessions s LEFT JOIN call_agents a ON a.agent_id = s.agent_id "
                               "WHERE s.owner = ? AND s.created_at >= ? ORDER BY s.created_at DESC LIMIT ?",
                               (owner, since, limit)).fetchall()
        status = {"completed": "ok", "transferred": "ok", "failed": "failed", "abandoned": "cancelled",
                  "timeout": "failed", "preempted": "failed", "dropped": "cancelled"}
        return [{"id": r["session_id"], "kind": "call", "title": f"Test call: {r['name'] or r['agent_id']}",
                 "status": status.get(r["disposition"] or "", "running" if r["state"] != "ended" else "ok"),
                 "at": r["created_at"], "duration_ms": int((r["duration_s"] or 0) * 1000) or None,
                 "error": None, "link": f"#/calls?session={r['session_id']}",
                 "detail": {"agent_id": r["agent_id"], "disposition": r["disposition"], "state": r["state"]}}
                for r in rows]

    # ------------------------------------------------------ maintenance
    def _startup(self) -> None:
        """A Control Center restart ends every realtime session (the tunnel registry is in memory)."""
        try:
            with self.connect() as con:
                open_ids = [r[0] for r in con.execute("SELECT session_id FROM call_sessions WHERE state != 'ended'")]
        except Exception:  # noqa: BLE001 - migration not applied yet (offline tests)
            return
        for sid in open_ids:
            try:
                self.client.request("POST", f"/v1/call/sessions/{sid}/end", {"reason": "control_center_restart"},
                                    timeout=30)
            except CallError:
                pass
            self._start_controller(sid)
        while True:
            try:
                self.purge_expired()
            except Exception:  # noqa: BLE001
                log.exception("call retention purge failed")
            time.sleep(3600)

    def purge_expired(self) -> int:
        now = self.clock()
        with self.connect() as con:
            ids = [r[0] for r in con.execute("SELECT session_id FROM call_sessions WHERE retain_until < ? "
                                             "AND content_purged = 0 AND state = 'ended'", (now,))]
            for sid in ids:
                con.execute("BEGIN IMMEDIATE")
                try:
                    for table in ("call_transcripts", "call_tool_events", "call_state", "call_state_changes",
                                  "call_results"):
                        con.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))  # noqa: S608
                    con.execute("DELETE FROM call_events WHERE session_id = ? AND type NOT IN "
                                "('session.ended', 'engine.stats', 'interruption', 'agent.speech.started')", (sid,))
                    con.execute("UPDATE call_sessions SET content_purged = 1, transfer = '{}' WHERE session_id = ?",
                                (sid,))
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
        return len(ids)

    def delete_session_content(self, sid: str, *, user_label: str) -> dict:
        row = self._session_row(sid)
        if row["state"] != "ended":
            raise CallError("end the call before deleting its content", 409, "call_live")
        with self.connect() as con:
            con.execute("UPDATE call_sessions SET retain_until = 0 WHERE session_id = ?", (sid,))
        self.purge_expired()
        self.audit(user=user_label, action="call.session.delete_content", outcome="ok", session=sid)
        return self.session_view(sid)


def _stats(values: list) -> dict | None:
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    return {"count": len(vals), "mean": round(sum(vals) / len(vals)), "p50": vals[len(vals) // 2],
            "p90": vals[min(len(vals) - 1, int(len(vals) * 0.9))], "max": vals[-1]}


def summarize_metrics(m: dict) -> dict:
    out = {k: v for k, v in m.items() if not isinstance(v, list)}
    for key in ("turn_latency_ms", "turn_latency_wall_ms", "interruption_latency_ms", "interruption_latency_wall_ms"):
        if isinstance(m.get(key), list):
            out[key] = _stats(m[key])
    return out


def _ascii(text: str) -> str:
    return ca._ascii(str(text))  # noqa: SLF001 - same package
