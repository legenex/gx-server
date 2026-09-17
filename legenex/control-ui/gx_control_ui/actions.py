"""The complete list of state-changing operations the UI may perform.

Design rules (see ARCHITECTURE.md "Control UI"):

* Every operation is a named entry in `build_registry()` with a fixed
  implementation. There is no generic "run a command" path; the only
  caller-controlled input is the operation name (and, for model operations,
  an alias from a fixed set).
* gx-max changes go ONLY through the orchestrator's sanctioned
  acquire/release API. No docker command here starts or stops a rank.
* Operations that could collide with gx-max refuse while gx-max is loading,
  serving or releasing, or while a rank container exists on either node.
* Everything is audited (user, client address, operation, outcome, elapsed)
  to /srv/logs/gx-control-ui/audit.log. Output is redacted.
"""

from __future__ import annotations

import collections
import json
import os
import secrets
import shlex
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable

from .config import UIConfig
from .models import ResultLog
from .redact import redact
from .services import SWAP_MODELS, Cluster
from .util import HTTPError, http, http_json, run, ssh_args

GXMAX_CONFIRM = "gx-max"
FORCE_CONFIRM = "FORCE RELEASE"


class ActionRefused(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Job:
    id: str
    action: str
    label: str
    user: str
    ip: str
    started: float
    state: str = "running"          # running | succeeded | failed
    ended: float | None = None
    output: list[str] = field(default_factory=list)
    result: dict = field(default_factory=dict)

    def log(self, line: str) -> None:
        for part in str(line).splitlines() or [""]:
            self.output.append(redact(part)[:2000])
        del self.output[:-500]

    def as_dict(self, with_output: bool = True) -> dict:
        d = {"id": self.id, "action": self.action, "label": self.label, "user": self.user,
             "started": self.started, "ended": self.ended, "state": self.state,
             "elapsed_seconds": round((self.ended or time.time()) - self.started, 1),
             "result": self.result}
        if with_output:
            d["output"] = list(self.output)
        return d


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    description: str
    danger: str                       # safe | caution | danger
    group: str | None                 # serialisation group (None = no lock)
    run: Callable[[Job], bool]
    precheck: Callable[[], str | None] = lambda: None
    confirm_phrase: str | None = None
    admin_only: bool = False

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "description": self.description,
                "danger": self.danger, "confirm_phrase": self.confirm_phrase,
                "needs_confirm": self.danger != "safe", "advanced": self.admin_only}


class ActionRunner:
    def __init__(self, cfg: UIConfig, cluster: Cluster, results: ResultLog) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.results = results
        self._jobs: collections.OrderedDict[str, Job] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._groups: dict[str, str] = {}   # group -> running job id
        #: Resource Control's Maintenance flag (set by the App; D-037)
        self.maintenance: Callable[[], bool] = lambda: False
        self.registry = build_registry(self)
        self.audit_path = cfg.log_dir / "audit.log"

    # ----------------------------------------------------------- auditing
    def audit(self, **fields) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}
        try:
            self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(redact(json.dumps(entry, sort_keys=True)) + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------- submission
    def submit(self, name: str, *, user: str, ip: str, confirm=None) -> Job:
        spec = self.registry.get(name)
        if spec is None:
            raise ActionRefused(f"unknown operation: {name}", 404)
        if spec.confirm_phrase is not None:
            if confirm != spec.confirm_phrase:
                self.audit(user=user, ip=ip, action=name, outcome="refused", reason="confirmation missing")
                raise ActionRefused(f"type '{spec.confirm_phrase}' to confirm this operation", 400)
        elif spec.danger != "safe" and confirm is not True:
            self.audit(user=user, ip=ip, action=name, outcome="refused", reason="confirmation missing")
            raise ActionRefused("this operation must be confirmed", 400)

        reason = spec.precheck()
        if reason:
            self.audit(user=user, ip=ip, action=name, outcome="refused", reason=reason)
            raise ActionRefused(reason)

        with self._lock:
            if spec.group and spec.group in self._groups:
                busy = self._jobs.get(self._groups[spec.group])
                label = busy.label if busy else "another operation"
                self.audit(user=user, ip=ip, action=name, outcome="refused", reason=f"busy: {label}")
                raise ActionRefused(f"'{label}' is still running; wait for it to finish")
            job = Job(secrets.token_hex(8), name, spec.label, user, ip, time.time())
            self._jobs[job.id] = job
            while len(self._jobs) > 60:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].state == "running":
                    break
                self._jobs.pop(oldest)
            if spec.group:
                self._groups[spec.group] = job.id

        self.audit(user=user, ip=ip, action=name, outcome="started", job=job.id)
        threading.Thread(target=self._execute, args=(spec, job), daemon=True,
                         name=f"action-{name}").start()
        return job

    def _execute(self, spec: ActionSpec, job: Job) -> None:
        ok = False
        try:
            ok = bool(spec.run(job))
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            job.log(f"error: {type(exc).__name__}: {exc}")
        finally:
            job.state = "succeeded" if ok else "failed"
            job.ended = time.time()
            with self._lock:
                if spec.group and self._groups.get(spec.group) == job.id:
                    del self._groups[spec.group]
            self.cluster.invalidate()
            self.audit(user=job.user, ip=job.ip, action=spec.name, outcome=job.state, job=job.id,
                       elapsed=round(job.ended - job.started, 1))

    def jobs(self) -> list[dict]:
        with self._lock:
            return [j.as_dict(with_output=False) for j in reversed(self._jobs.values())]

    def job(self, job_id: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return j.as_dict() if j else None

    def running(self) -> list[dict]:
        with self._lock:
            return [j.as_dict(with_output=False) for j in self._jobs.values() if j.state == "running"]

    # ------------------------------------------------------ preconditions
    def _facts(self):
        return self.cluster.node1.get() or {}, self.cluster.node2.get() or {}

    def rank_containers(self) -> list[str]:
        n1, n2 = self._facts()
        found = []
        for facts, name in ((n1, "gx-max-rank0"), (n2, "gx-max-rank1")):
            for c in ((facts.get("docker") or {}).get("containers") or []):
                if c.get("name") == name:
                    found.append(f"{name} ({c.get('state')})")
        return found

    def gxmax_state(self) -> str:
        self.cluster.lifecycle.invalidate()
        return self.cluster.gxmax_state()

    def require_gxmax_quiet(self) -> str | None:
        state = self.gxmax_state()
        if state != "down":
            return f"refused: gx-max is {state}; normal workloads stay drained until it is released"
        ranks = self.rank_containers()
        if ranks:
            return f"refused: gx-max rank container present: {', '.join(ranks)}"
        return None

    def require_no_maintenance(self) -> str | None:
        if self.maintenance():
            return "refused: Maintenance mode is on; new heavy work starts again when it ends"
        return None

    def require_no_transition(self) -> str | None:
        state = self.gxmax_state()
        if state in ("acquiring", "releasing", "unknown"):
            return f"refused: gx-max lifecycle is {state}"
        return None

    def require_node2(self) -> str | None:
        n2 = self.cluster.node2.get() or {}
        return None if n2.get("reachable") else "refused: gx10-02 is not reachable over SSH"

    def media_busy(self) -> str | None:
        self.cluster.services.invalidate()
        media = (self.cluster.services.get() or {}).get("media") or {}
        body = media.get("body") if media.get("ok") else None
        if isinstance(body, dict) and (body.get("busy") or body.get("video_queue_depth")):
            return "refused: a media generation is in progress"
        return None

    # --------------------------------------------------------- primitives
    def ssh(self, command: str, timeout: float) -> tuple[bool, str]:
        res = run(ssh_args(self.cfg.node2_ssh, 10) + [command], timeout=timeout)
        return res.ok, res.out

    def wait_http(self, url: str, seconds: float, headers=None) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if 200 <= http("GET", url, headers=headers, timeout=4).status < 300:
                    return True
            except HTTPError:
                pass
            time.sleep(2)
        return False


def _all_checks(*checks: Callable[[], str | None]) -> Callable[[], str | None]:
    def inner() -> str | None:
        for check in checks:
            reason = check()
            if reason:
                return reason
        return None
    return inner


def build_registry(r: ActionRunner) -> dict[str, ActionSpec]:
    cfg, cl = r.cfg, r.cluster
    specs: list[ActionSpec] = []
    orch = cfg.orchestrator_base

    # ============================ gx-max (orchestrator lifecycle only) ====
    def gxmax_acquire(job: Job) -> bool:
        job.log("POST gx-orchestrator /lifecycle/gx-max/acquire (sanctioned lifecycle)")
        job.log("sequence: drain -> admission -> rank1 -> rank0 -> health; cold load ~9 minutes")
        t0 = time.time()
        try:
            status, body = http_json("POST", f"{orch}/lifecycle/gx-max/acquire",
                                     body={"timeout": 1800}, timeout=1900)
        except HTTPError as exc:
            job.log(exc.message)
            r.results.record("gx-max", "load", False, exc.message)
            return False
        elapsed = round(time.time() - t0)
        ok = status == 200
        detail = json.dumps(body)[:1500]
        job.log(f"HTTP {status} after {elapsed}s: {detail}")
        startup = body.get("last_startup_seconds") if isinstance(body, dict) else None
        job.result = {"http_status": status, "elapsed_seconds": elapsed, "startup_seconds": startup}
        r.results.record("gx-max", "load", ok, "ready" if ok else detail, seconds=startup or elapsed)
        return ok

    def gxmax_release(force: bool) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            job.log(f"POST gx-orchestrator /lifecycle/gx-max/release force={force} (restore normal workloads)")
            t0 = time.time()
            try:
                status, body = http_json("POST", f"{orch}/lifecycle/gx-max/release",
                                         body={"force": force, "restore": True}, timeout=1200)
            except HTTPError as exc:
                job.log(exc.message)
                return False
            job.log(f"HTTP {status} after {round(time.time() - t0)}s: {json.dumps(body)[:1000]}")
            ok = status == 200 and isinstance(body, dict) and body.get("state") == "down"
            # Verify, do not assume: no rank container may remain.
            # invalidate() makes the next get() block for a fresh reading.
            cl.node1.invalidate()
            cl.node2.invalidate()
            ranks = r.rank_containers()
            if ranks:
                job.log(f"WARNING: rank container still present after release: {ranks}")
                ok = False
            else:
                job.log("verified: no gx-max rank container on either node")
            r.results.record("gx-max", "unload", ok, "released" if ok else "release incomplete")
            return ok
        return inner

    def gxmax_restart(job: Job) -> bool:
        if r.gxmax_state() == "ready" and not gxmax_release(False)(job):
            job.log("release failed; not re-acquiring")
            return False
        return gxmax_acquire(job)

    def pre_gxmax_load() -> str | None:
        state = r.gxmax_state()
        if state == "ready":
            return "gx-max is already READY"
        if state != "down":
            return f"refused: gx-max is {state}"
        return r.require_no_maintenance() or r.require_node2() or r.media_busy()

    def pre_gxmax_unload() -> str | None:
        state = r.gxmax_state()
        return None if state == "ready" else f"refused: gx-max is {state}, not ready"

    def pre_gxmax_restart() -> str | None:
        state = r.gxmax_state()
        if state not in ("ready", "down"):
            return f"refused: gx-max is {state}"
        return r.require_node2()

    def pre_force() -> str | None:
        if r.gxmax_state() == "down" and not r.rank_containers():
            return "nothing to release: gx-max is down and no rank container exists"
        return None

    specs += [
        ActionSpec("model.gx-max.load", "Load gx-max (two-node takeover)",
                   "Calls the orchestrator's sanctioned acquire. Drains gx-mini, gx-fast, gx-reason and "
                   "the media stack on both nodes, then starts rank 1 and rank 0. ~9 minutes cold.",
                   "danger", "cluster", gxmax_acquire, pre_gxmax_load, confirm_phrase=GXMAX_CONFIRM),
        ActionSpec("model.gx-max.unload", "Release gx-max (graceful)",
                   "Orchestrator graceful release: waits for in-flight requests, stops both ranks, "
                   "verifies memory and restores normal workloads.",
                   "caution", "cluster", gxmax_release(False), pre_gxmax_unload),
        ActionSpec("model.gx-max.restart", "Restart gx-max",
                   "Graceful release followed by a fresh sanctioned acquire (~10 minutes).",
                   "danger", "cluster", gxmax_restart, pre_gxmax_restart, confirm_phrase=GXMAX_CONFIRM),
        ActionSpec("model.gx-max.force_release", "Force-release gx-max (advanced)",
                   "Emergency only: orchestrator release with force=true. Does not wait for in-flight "
                   "requests. Use when a rank is wedged or a load is stuck.",
                   "danger", None, gxmax_release(True), pre_force, confirm_phrase=FORCE_CONFIRM,
                   admin_only=True),
    ]

    # ================================ llama-swap tiers ====================
    load_timeouts = {"gx-mini": 600, "gx-fast": 1800, "gx-reason": 2400}

    def swap_load(alias: str) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            preview = cl.admission_preview(alias)
            job.log(f"admission preview: {preview.get('reason')}")
            if not preview.get("allowed"):
                return False
            job.log(f"GET llama-swap {SWAP_MODELS[alias]} /upstream/{alias}/health (load on demand)")
            t0 = time.time()
            ok, msg = cl.swap_load(alias, load_timeouts[alias])
            secs = round(time.time() - t0, 1)
            job.log(f"{msg} ({secs}s)")
            job.result = {"load_seconds": secs}
            r.results.record(alias, "load", ok, msg, seconds=secs)
            return ok
        return inner

    def swap_unload(alias: str) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            job.log(f"POST llama-swap {SWAP_MODELS[alias]} /api/models/unload/{alias}")
            ok, msg = cl.swap_unload(alias)
            job.log(msg)
            r.results.record(alias, "unload", ok, msg)
            return ok
        return inner

    def swap_restart(alias: str) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            return swap_unload(alias)(job) and swap_load(alias)(job)
        return inner

    def pre_swap_load(alias: str) -> Callable[[], str | None]:
        def inner() -> str | None:
            checks = [r.require_gxmax_quiet]
            if SWAP_MODELS[alias] == "node2":
                checks += [r.require_node2, r.require_no_maintenance]
            reason = _all_checks(*checks)()
            if reason:
                return reason
            preview = cl.admission_preview(alias)
            if not preview.get("allowed"):
                return f"refused by the admission guard: {preview.get('reason')}"
            return None
        return inner

    for alias in ("gx-mini", "gx-fast", "gx-reason"):
        node = "gx10-01" if SWAP_MODELS[alias] == "node1" else "gx10-02"
        specs += [
            ActionSpec(f"model.{alias}.load", f"Load {alias}",
                       f"Asks llama-swap on {node} to start {alias} now (the same on-demand path a "
                       "request takes), after the 30 GiB admission check.",
                       "safe", "cluster", swap_load(alias), pre_swap_load(alias)),
            ActionSpec(f"model.{alias}.unload", f"Unload {alias}",
                       f"Asks llama-swap on {node} to stop {alias}. In-flight requests to it fail.",
                       "caution", "cluster", swap_unload(alias), r.require_no_transition),
            ActionSpec(f"model.{alias}.restart", f"Restart {alias}",
                       "Unload, then load again through llama-swap.",
                       "caution", "cluster", swap_restart(alias), pre_swap_load(alias)),
        ]

    # ================================ media ===============================
    def media_unload(job: Job) -> bool:
        # Through the router (never ComfyUI directly): the router refuses while a
        # job runs and resets its resident-model record, so the next job is
        # admitted as cold (D-036).
        job.log("POST media router /v1/admin/free (router-mediated ComfyUI free)")
        try:
            res = http("POST", f"{cfg.media_base}/v1/admin/free", body={}, headers=cl.media_headers(), timeout=60)
            text = res.text(500)
            ok = res.status == 200
        except HTTPError as exc:
            ok, text = False, exc.message
        job.log(text)
        for alias in ("gx-image", "gx-video"):
            r.results.record(alias, "unload", ok, "ComfyUI models freed" if ok else "free refused")
        return ok

    for alias in ("gx-image", "gx-video"):
        specs.append(ActionSpec(
            f"model.{alias}.unload", f"Unload media models ({alias})",
            "Frees ComfyUI's loaded image AND video weights on gx10-02 (they share one engine). "
            "The next generation reloads them.",
            "caution", "media", media_unload, _all_checks(r.require_node2, r.media_busy)))

    # ================================ music ===============================
    def music_op(op: str) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            key = cfg.music_key_file.read_text(encoding="utf-8").strip() if cfg.music_key_file.exists() else ""
            job.log(f"POST gx-music supervisor /v1/music/{op} (fabric)")
            t0 = time.time()
            try:
                res = http("POST", f"{cfg.music_base}/v1/music/{op}", body={},
                           headers={"Authorization": f"Bearer {key}"}, timeout=1200 if op == "load" else 120)
                text, ok = res.text(800), res.status == 200
            except HTTPError as exc:
                text, ok = exc.message, False
            secs = round(time.time() - t0, 1)
            job.log(f"{text} ({secs}s)")
            job.result = {"seconds": secs}
            r.results.record("gx-music", op, ok, text[:200], seconds=secs)
            return ok
        return inner

    specs += [
        ActionSpec("model.gx-music.load", "Load gx-music",
                   "Asks the gx-music supervisor on gx10-02 to load ACE-Step now (admission-guarded; about "
                   "90 s). Refused while gx-max owns the cluster or in Maintenance.",
                   "safe", "music", music_op("load"),
                   _all_checks(r.require_gxmax_quiet, r.require_node2, r.require_no_maintenance)),
        ActionSpec("model.gx-music.unload", "Unload gx-music",
                   "Stops the ACE-Step engine on gx10-02 and returns its memory. Refused while a track is "
                   "generating.", "caution", "music", music_op("unload"), r.require_node2),
    ]

    # ================================ system ==============================
    def refresh(job: Job) -> bool:
        cl.invalidate()
        cl.services.get(max_age=0)
        job.log("caches cleared; health re-probed")
        return True

    def verify_summary(text: str) -> str:
        for line in reversed(text.splitlines()):
            if "passed" in line and "failed" in line:
                return line.strip(" -")
            if "result:" in line:
                return line.strip(" =")
        return "no summary line"

    def both_nodes(script_rel: str, timeout: float, label: str) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            local = run([str(cfg.repo_root / script_rel)], timeout=timeout)
            job.log(f"===== gx10-01: {label} (exit {local.rc}) =====")
            job.log(local.out.strip()[-6000:])
            remote_path = shlex.quote(f"{cfg.node2_repo}/{script_rel}")
            ok2, out2 = r.ssh(remote_path, timeout)
            job.log(f"===== gx10-02: {label} ({'ok' if ok2 else 'FAILED'}) =====")
            job.log(out2.strip()[-6000:])
            job.result = {"gx10-01": {"ok": local.ok, "summary": verify_summary(local.out)},
                          "gx10-02": {"ok": ok2, "summary": verify_summary(out2)}}
            return local.ok and ok2
        return inner

    def reconcile_node2(job: Job) -> bool:
        job.log("gx10-02: systemctl --user start gx-git-reconcile.service")
        ok, out = r.ssh("systemctl --user start gx-git-reconcile.service && "
                        f"git -C {shlex.quote(cfg.node2_repo)} rev-parse HEAD", 180)
        job.log(out.strip()[-2000:])
        head = out.strip().splitlines()[-1] if ok and out.strip() else ""
        remote = cl._remote_head()
        job.result = {"node2_head": head, "origin_main": remote.get("head"),
                      "match": bool(head) and head == remote.get("head")}
        job.log(f"node2 HEAD {head[:12]} vs origin/main {str(remote.get('head'))[:12]}")
        return ok

    def hostwatch_now(job: Job) -> bool:
        a = run(["systemctl", "--user", "start", "gx-hostwatch.service"], timeout=120)
        job.log(f"gx10-01 hostwatch: exit {a.rc}")
        ok2, out2 = r.ssh("systemctl --user start gx-hostwatch.service", 120)
        job.log(f"gx10-02 hostwatch: {'ok' if ok2 else out2.strip()[-300:]}")
        cl.invalidate()
        return a.ok and ok2

    def restart_ui(job: Job) -> bool:
        job.log("restarting gx-control-ui.service in 2 s; the page reconnects automatically")

        def later() -> None:
            time.sleep(2)
            run(["systemctl", "--user", "restart", "--no-block", "gx-control-ui.service"], timeout=15)
        threading.Thread(target=later, daemon=True).start()
        return True

    specs += [
        ActionSpec("system.refresh", "Refresh health", "Clears every cache and re-probes the cluster now.",
                   "safe", None, refresh),
        ActionSpec("system.integrity_audit", "Run integrity audit (both nodes)",
                   "ops/git-sync/integrity-audit.sh on gx10-01 and gx10-02. Read-only.",
                   "safe", "audit", both_nodes("ops/git-sync/integrity-audit.sh", 300, "integrity audit")),
        ActionSpec("system.kernel_verify", "Run kernel-lock verifier (both nodes)",
                   "legenex/host/kernel-lock/verify-kernel-lock.sh on both nodes. Read-only simulation; "
                   "installs nothing.",
                   "safe", "kernel", both_nodes("legenex/host/kernel-lock/verify-kernel-lock.sh", 300,
                                                "kernel-lock verifier")),
        ActionSpec("system.reconcile_node2", "Reconcile gx10-02 from origin/main",
                   "Starts gx10-02's pull-only reconcile unit (fetch, save drift evidence, reset to "
                   "origin/main).", "caution", "git", reconcile_node2, r.require_node2),
        ActionSpec("system.hostwatch", "Run hostwatch now (both nodes)",
                   "One read-only host resilience check cycle on each node.", "safe", "hostwatch",
                   hostwatch_now),
        ActionSpec("system.restart_ui", "Restart the control UI",
                   "systemctl --user restart gx-control-ui.service. Sessions survive only until restart.",
                   "caution", "ui", restart_ui),
    ]

    # ======================= non-model infrastructure =====================
    def docker_restart_local(container: str, health_url: str | None, headers=None) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            job.log(f"gx10-01: docker restart {container}")
            res = run(["docker", "restart", "-t", "30", container], timeout=120)
            job.log(res.out.strip())
            if not res.ok:
                return False
            if health_url:
                ok = r.wait_http(health_url, 120, headers)
                job.log(f"health {'OK' if ok else 'NOT answering after 120 s'}: {health_url}")
                return ok
            return True
        return inner

    def docker_restart_node2(container: str, health_url: str | None) -> Callable[[Job], bool]:
        def inner(job: Job) -> bool:
            job.log(f"gx10-02: docker restart {container}")
            ok, out = r.ssh(f"docker restart -t 30 {shlex.quote(container)}", 150)
            job.log(out.strip())
            if ok and health_url:
                ok = r.wait_http(health_url, 180, None)
                job.log(f"health {'OK' if ok else 'NOT answering after 180 s'}: {health_url}")
            return ok
        return inner

    def restart_orchestrator(job: Job) -> bool:
        job.log("gx10-01: systemctl --user restart gx-orchestrator.service")
        res = run(["systemctl", "--user", "restart", "gx-orchestrator.service"], timeout=90)
        job.log(res.out.strip() or f"exit {res.rc}")
        ok = res.ok and r.wait_http(f"{orch}/health", 60)
        job.log("orchestrator health OK" if ok else "orchestrator did not answer")
        return ok

    def restore_normal(job: Job) -> bool:
        job.log("legenex/lifecycle/restore-normal.sh (control planes only; models stay on demand)")
        res = run(["bash", str(cfg.lifecycle_dir / "restore-normal.sh")], timeout=600)
        job.log(res.out.strip()[-4000:])
        return res.ok

    specs += [
        ActionSpec("infra.restart_litellm", "Restart LiteLLM gateway",
                   "docker restart gx-litellm. Requests in flight through the gateway fail.",
                   "caution", "cluster",
                   docker_restart_local("gx-litellm", f"{cfg.litellm_base}/health/liveliness"),
                   r.require_no_transition),
        ActionSpec("infra.restart_swap_node1", "Restart llama-swap (gx10-01)",
                   "docker restart gx-llama-swap-node01. Stops gx-mini/gx-fast; they reload on demand.",
                   "caution", "cluster",
                   docker_restart_local("gx-llama-swap-node01", f"{cfg.node1_swap_base}/health"),
                   r.require_gxmax_quiet),
        ActionSpec("infra.restart_swap_node2", "Restart llama-swap (gx10-02)",
                   "docker restart gx-llama-swap-node02 on gx10-02. Stops gx-reason.",
                   "caution", "cluster",
                   docker_restart_node2("gx-llama-swap-node02", f"{cfg.node2_swap_base}/health"),
                   _all_checks(r.require_gxmax_quiet, r.require_node2)),
        ActionSpec("infra.restart_media_router", "Restart media router (gx10-02)",
                   "docker restart gx-media-router. Refused while a generation is running.",
                   "caution", "media",
                   docker_restart_node2("gx-media-router", f"{cfg.media_base}/health"),
                   _all_checks(r.require_gxmax_quiet, r.require_node2, r.media_busy)),
        ActionSpec("infra.restart_orchestrator", "Restart gx-orchestrator",
                   "systemctl --user restart gx-orchestrator.service. Refused while gx-max is "
                   "loading or releasing; a serving gx-max is re-adopted on start.",
                   "caution", "cluster", restart_orchestrator, r.require_no_transition),
        ActionSpec("infra.restore_normal", "Restore normal workloads",
                   "Runs lifecycle/restore-normal.sh: brings the gateway, orchestrator, node 2 "
                   "llama-swap and media control planes back up. Loads no model.",
                   "caution", "cluster", restore_normal, r.require_gxmax_quiet),
    ]
    return {s.name: s for s in specs}
