"""The complete list of state-changing operations the UI may perform.

Design rules (see ARCHITECTURE-V41.md section 6 and section 12):

* Every operation is a named entry in `build_registry()` with a fixed
  implementation. There is no generic "run a command" path; the only
  caller-controlled input is the operation name and, where documented, a
  value from a fixed set (a serving profile, a request id, a bench name).
* gx-max changes go ONLY through the orchestrator's sanctioned lifecycle
  API (/lifecycle/gx-max/acquire|release|restart|drain). No docker command
  here starts or stops a rank.
* Everything is audited (user, client address, operation, outcome, elapsed)
  to /srv/logs/gx-control-ui/audit.log. Output is redacted.
* No auto-updates anywhere: `update_check` reports drift, nothing more.

V4.1 action set: gxmax_start(profile), gxmax_stop, gxmax_restart(profile),
gxmax_drain, health_check, benchmark_run(name), scheduler_cancel(request_id),
scheduler_retry(request_id), trash_restore(id), purge_trash, update_check —
plus the retained system/infra operations.
"""

from __future__ import annotations

import collections
import json
import os
import re
import secrets
import shlex
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from .config import UIConfig
from .models import profiles as registry_profiles, read_registry, reasoning as registry_reasoning
from .redact import redact
from .services import Cluster
from .util import HTTPError, bearer, http, http_json, run, ssh_args

GXMAX_CONFIRM = "gx-max"
PURGE_CONFIRM = "PURGE TRASH"
_BENCH_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,40}$")


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
    run: Callable[..., bool]
    precheck: Callable[[], str | None] = lambda: None
    confirm_phrase: str | None = None
    admin_only: bool = False
    #: Names of extra string arguments the caller may pass (validated below).
    args: tuple[str, ...] = ()

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "description": self.description,
                "danger": self.danger, "confirm_phrase": self.confirm_phrase,
                "needs_confirm": self.danger != "safe", "advanced": self.admin_only,
                "args": list(self.args)}


class ActionRunner:
    def __init__(self, cfg: UIConfig, cluster: Cluster, results) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.results = results
        self._jobs: collections.OrderedDict[str, Job] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._groups: dict[str, str] = {}   # group -> running job id
        #: Resource Control's Maintenance flag (set by the App; D-037 protocol kept)
        self.maintenance: Callable[[], bool] = lambda: False
        #: late-bound partners (set by the App after construction)
        self.filemanager: Any = None          # filemanager.FileManager
        self.updates: Any = None              # updates_view.UpdatesView
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
    def submit(self, name: str, *, user: str, ip: str, confirm=None, args: dict | None = None) -> Job:
        spec = self.registry.get(name)
        if spec is None:
            raise ActionRefused(f"unknown operation: {name}", 404)
        args = args or {}
        clean: dict[str, str] = {}
        for key, value in args.items():
            if key not in spec.args:
                raise ActionRefused(f"unknown argument for {name}: {key}", 400)
            if not isinstance(value, str) or len(value) > 200:
                raise ActionRefused(f"argument {key} must be a short string", 400)
            clean[key] = value
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

        self.audit(user=user, ip=ip, action=name, outcome="started", job=job.id, args=clean or None)
        threading.Thread(target=self._execute, args=(spec, job, clean), daemon=True,
                         name=f"action-{name}").start()
        return job

    def _execute(self, spec: ActionSpec, job: Job, args: dict) -> None:
        ok = False
        try:
            ok = bool(spec.run(job, **args)) if args else bool(spec.run(job))
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
    def gxmax_state(self) -> str:
        self.cluster.lifecycle.invalidate()
        return self.cluster.gxmax_state()

    def require_no_transition(self) -> str | None:
        state = self.gxmax_state()
        if state in ("acquiring", "releasing", "unknown"):
            return f"refused: gx-max lifecycle is {state}"
        return None

    def require_node2(self) -> str | None:
        n2 = self.cluster.node2.get() or {}
        return None if n2.get("reachable") else "refused: gx10-02 is not reachable over SSH"

    def require_no_maintenance(self) -> str | None:
        if self.maintenance():
            return "refused: Maintenance mode is on; new heavy work starts again when it ends"
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


def serving_profile(r: ActionRunner, wanted: str | None) -> str:
    """Validate a serving profile against the registry (fast/balanced/...)."""
    reg = read_registry(r.cfg.registry_path)
    names = tuple(registry_profiles(reg)) or ("fast", "balanced", "swarm", "deep", "long")
    if wanted is None or wanted == "":
        return "balanced"
    if wanted not in names:
        raise ActionRefused(f"unknown profile {wanted!r}; registry offers: {', '.join(names)}", 400)
    return wanted


def build_registry(r: ActionRunner) -> dict[str, ActionSpec]:
    cfg, cl = r.cfg, r.cluster
    specs: list[ActionSpec] = []
    orch = cfg.orchestrator_base
    orch_h = bearer(cfg.secret("GX_ORCHESTRATOR_API_KEY"))

    def orch_call(job: Job, method: str, path: str, body: dict | None, timeout: float,
                  ok_states: tuple[str, ...] = ("ready", "down")) -> tuple[bool, dict]:
        t0 = time.time()
        try:
            status, out = http_json(method, f"{orch}{path}", body=body, timeout=timeout, headers=orch_h)
        except HTTPError as exc:
            job.log(exc.message)
            return False, {"error": exc.message}
        elapsed = round(time.time() - t0)
        job.log(f"HTTP {status} after {elapsed}s: {json.dumps(out)[:1500]}")
        ok = status == 200 and (not ok_states or isinstance(out, dict) and out.get("state") in ok_states)
        return ok, out if isinstance(out, dict) else {"raw": str(out)[:500]}

    # ============================ gx-max (orchestrator lifecycle only) ====
    def gxmax_start(job: Job, profile: str = "balanced") -> bool:
        prof = serving_profile(r, profile)
        job.log(f"POST gx-orchestrator /lifecycle/gx-max/acquire (profile {prof}; sanctioned lifecycle)")
        job.log("sequence: preflight -> rank1 -> rank0 -> health; the V4.1 pack is about 105 GiB per node")
        ok, body = orch_call(job, "POST", "/lifecycle/gx-max/acquire",
                             {"profile": prof, "timeout": 1800}, 1900, ok_states=("ready",))
        startup = body.get("last_startup_seconds") if isinstance(body, dict) else None
        job.result = {"http_status": 200 if ok else 0, "profile": prof, "startup_seconds": startup,
                     **{k: body.get(k) for k in ("state", "error") if isinstance(body, dict) and k in body}}
        r.results.record("gx-max", "load", ok, "ready" if ok else json.dumps(body)[:300],
                         profile=prof, seconds=startup)
        return ok

    def gxmax_stop(job: Job) -> bool:
        job.log("POST gx-orchestrator /lifecycle/gx-max/release (graceful; restore)")
        ok, body = orch_call(job, "POST", "/lifecycle/gx-max/release",
                             {"force": False, "restore": True}, 1200, ok_states=("down",))
        r.results.record("gx-max", "unload", ok, "released" if ok else json.dumps(body)[:300])
        return ok

    def gxmax_restart(job: Job, profile: str = "balanced") -> bool:
        prof = serving_profile(r, profile)
        state = r.gxmax_state()
        if state == "ready":
            job.log("releasing first (graceful)")
            ok, _ = orch_call(job, "POST", "/lifecycle/gx-max/release",
                              {"force": False, "restore": True}, 1200, ok_states=("down",))
            if not ok:
                job.log("release failed; not re-acquiring")
                return False
        return gxmax_start(job, profile=prof)

    def gxmax_drain(job: Job) -> bool:
        job.log("POST gx-orchestrator /lifecycle/gx-max/drain (stop accepting; in-flight finish)")
        # Any 2xx is success: the orchestrator answers "draining" (a normal
        # mid-transition state), not ready/down.
        ok, body = orch_call(job, "POST", "/lifecycle/gx-max/drain", {}, 600, ok_states=())
        job.result = body if isinstance(body, dict) else {}
        r.results.record("gx-max", "drain", ok, json.dumps(body)[:300])
        return ok

    def pre_gxmax_start() -> str | None:
        state = r.gxmax_state()
        if state == "ready":
            return "gx-max is already READY (use RESTART to change its profile)"
        return _all_checks(r.require_no_transition, r.require_no_maintenance, r.require_node2)()

    def pre_gxmax_stop() -> str | None:
        state = r.gxmax_state()
        return None if state == "ready" else f"refused: gx-max is {state}, not ready"

    specs += [
        ActionSpec("gxmax_start", "Start gx-max (profile)",
                   "Acquires the cluster for DeepSeek V4.1 Flash through the orchestrator lifecycle, "
                   "with the chosen serving profile (fast/balanced/swarm/deep/long). About 105 GiB per "
                   "node; never starts at boot.",
                   "danger", "cluster", gxmax_start, pre_gxmax_start, confirm_phrase=GXMAX_CONFIRM,
                   args=("profile",)),
        ActionSpec("gxmax_stop", "Stop gx-max (graceful release)",
                   "Orchestrator graceful release: waits for in-flight requests, stops both ranks, "
                   "verifies memory return.",
                   "caution", "cluster", gxmax_stop, pre_gxmax_stop),
        ActionSpec("gxmax_restart", "Restart gx-max (profile)",
                   "Graceful release followed by a fresh acquire with the chosen profile.",
                   "danger", "cluster", gxmax_restart, _all_checks(r.require_node2, r.require_no_maintenance),
                   confirm_phrase=GXMAX_CONFIRM, args=("profile",)),
        ActionSpec("gxmax_drain", "Drain gx-max",
                   "Asks the orchestrator to stop accepting new requests and let in-flight work finish.",
                   "caution", "cluster", gxmax_drain, r.require_no_transition),
    ]

    # ================================ scheduler (relayed controls) ======
    def scheduler_cancel(job: Job, request_id: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", request_id):
            job.log(f"invalid request id: {request_id[:40]!r}")
            return False
        job.log(f"POST gx-orchestrator /scheduler/cancel (request {request_id})")
        try:
            status, body = http_json("POST", f"{orch}/scheduler/cancel", body={"id": request_id},
                                     timeout=30, headers=orch_h)
        except HTTPError as exc:
            job.log(exc.message)
            return False
        job.log(f"HTTP {status}: {json.dumps(body)[:500]}")
        return 200 <= status < 300

    def scheduler_retry(job: Job, request_id: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", request_id):
            job.log(f"invalid request id: {request_id[:40]!r}")
            return False
        job.log(f"POST gx-orchestrator /scheduler/retry (request {request_id})")
        try:
            status, body = http_json("POST", f"{orch}/scheduler/retry", body={"id": request_id},
                                     timeout=60, headers=orch_h)
        except HTTPError as exc:
            job.log(exc.message)
            return False
        job.log(f"HTTP {status}: {json.dumps(body)[:500]}")
        return 200 <= status < 300

    specs += [
        ActionSpec("scheduler_cancel", "Cancel a queued/active request",
                   "Relays a cancel to the orchestrator scheduler (queued always, active best-effort).",
                   "caution", "scheduler", scheduler_cancel, r.require_no_transition,
                   args=("request_id",)),
        ActionSpec("scheduler_retry", "Retry a failed request",
                   "Relays a retry to the orchestrator scheduler.", "safe", "scheduler", scheduler_retry,
                   args=("request_id",)),
    ]

    # ================================ health / benchmarks ===============
    def health_check(job: Job) -> bool:
        job.log("GET orchestrator /health/detailed")
        findings: dict[str, Any] = {}
        try:
            status, body = http_json("GET", f"{orch}/health/detailed", headers=orch_h, timeout=8)
            job.log(f"orchestrator: HTTP {status}: {json.dumps(body)[:800]}")
            findings["orchestrator"] = {"ok": 200 <= status < 300, "status": status}
            if isinstance(body, dict):
                findings["orchestrator"]["body"] = body
        except HTTPError as exc:
            job.log(f"orchestrator unreachable: {exc.message}")
            findings["orchestrator"] = {"ok": False, "error": exc.message}
        job.log("GET Mia runtime :8888/health (loopback; only while READY)")
        try:
            status, body = http_json("GET", f"{cfg.mia_base}/health", timeout=5)
            job.log(f"mia: HTTP {status}: {json.dumps(body)[:400]}")
            findings["mia"] = {"ok": 200 <= status < 300, "status": status}
        except HTTPError as exc:
            state = r.gxmax_state()
            job.log(f"mia health not answering (gx-max is {state}): {exc.message}")
            findings["mia"] = {"ok": state == "down", "state": state, "note": "not serving while down"}
        n1, n2 = cl.node1.get() or {}, cl.node2.get() or {}
        for name, facts in (("gx10-01", n1), ("gx10-02", n2)):
            mem = ((facts or {}).get("memory") or {}).get("MemAvailable")
            if mem:
                job.log(f"{name}: MemAvailable {mem / 2**30:.1f} GiB")
        job.result = findings
        ok = bool(findings.get("orchestrator", {}).get("ok"))
        r.results.record("gx-max", "health", ok, json.dumps(findings)[:300])
        return ok

    def benchmark_run(job: Job, name: str = "startup") -> bool:
        if not _BENCH_NAME_RE.fullmatch(name or ""):
            job.log(f"invalid benchmark name: {(name or '')[:40]!r}")
            return False
        script = cfg.bench_dir / "run_bench.py"
        if not script.is_file():
            job.log(f"the benchmark suite is not present yet ({script}); refusing honestly")
            return False
        job.log(f"python3 ops/bench/run_bench.py {name}")
        res = run(["python3", str(script), name], timeout=3600)
        job.log(res.out.strip()[-8000:])
        job.result = {"exit": res.rc, "name": name}
        r.results.record("bench", name, res.ok, "completed" if res.ok else f"exit {res.rc}")
        return res.ok

    specs += [
        ActionSpec("health_check", "Run a cluster health check",
                   "Orchestrator /health/detailed + Mia :8888 health + both nodes' memory. Read-only.",
                   "safe", None, health_check),
        ActionSpec("benchmark_run", "Run a benchmark",
                   "Runs ops/bench/run_bench.py <name> (startup/load/ttft/tps/memory suite). "
                   "Results land in the bench history JSONL.",
                   "caution", "bench", benchmark_run, _all_checks(r.require_no_transition,
                                                                  r.require_no_maintenance),
                   args=("name",)),
    ]

    # ================================ trash / updates ===================
    def trash_restore(job: Job, trash_id: str = "") -> bool:
        if r.filemanager is None:
            job.log("file manager not initialised")
            return False
        try:
            info = r.filemanager.restore(trash_id, user=job.user)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            job.log(f"restore failed: {type(exc).__name__}: {exc}")
            return False
        job.log(f"restored {info.get('original')} from trash ({info.get('id')})")
        job.result = info
        return True

    def purge_trash(job: Job) -> bool:
        if r.filemanager is None:
            job.log("file manager not initialised")
            return False
        try:
            info = r.filemanager.purge(user=job.user)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            job.log(f"purge failed: {type(exc).__name__}: {exc}")
            return False
        job.log(f"purged {info.get('count')} trash entries ({info.get('bytes')} bytes freed)")
        job.result = info
        return True

    def update_check(job: Job) -> bool:
        if r.updates is None:
            job.log("updates view not initialised")
            return False
        job.log("checking pins against upstream (GitHub + Hugging Face); nothing is auto-updated")
        report = r.updates.check()
        job.log(json.dumps(report)[:4000])
        job.result = report
        drift = [d for d in report.get("pins", []) if not d.get("match")]
        job.log(f"drift: {len(drift)} of {len(report.get('pins', []))} pins")
        return True

    specs += [
        ActionSpec("trash_restore", "Restore an item from trash",
                   "Moves a trashed item back to its original path.", "safe", None, trash_restore,
                   args=("trash_id",)),
        ActionSpec("purge_trash", "Purge the trash (permanent)",
                   "PERMANENTLY deletes every trashed item. Separate from delete; typed confirmation "
                   "required; audit-logged.",
                   "danger", None, purge_trash, confirm_phrase=PURGE_CONFIRM, admin_only=True),
        ActionSpec("update_check", "Check for updates",
                   "Compares registry pins against upstream (Mia repo HEAD on GitHub, model revisions on "
                   "Hugging Face). Reports drift; NEVER updates anything.",
                   "safe", None, update_check),
    ]

    # ================================ system =============================
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
                        f"git -C {shlex.quote(str(cfg.node2_repo))} rev-parse HEAD", 180)
        job.log(out.strip()[-2000:])
        head = out.strip().splitlines()[-1] if ok and out.strip() else ""
        remote = cl.remote_git.get() or {}
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

    def restart_orchestrator(job: Job) -> bool:
        job.log("gx10-01: systemctl --user restart gx-orchestrator.service")
        res = run(["systemctl", "--user", "restart", "gx-orchestrator.service"], timeout=90)
        job.log(res.out.strip() or f"exit {res.rc}")
        ok = res.ok and r.wait_http(f"{orch}/health", 60)
        job.log("orchestrator health OK" if ok else "orchestrator did not answer")
        return ok

    def restart_litellm(job: Job) -> bool:
        job.log("gx10-01: docker restart gx-litellm")
        res = run(["docker", "restart", "-t", "30", "gx-litellm"], timeout=120)
        job.log(res.out.strip())
        if not res.ok:
            return False
        ok = r.wait_http(f"{cfg.litellm_base}/health/liveliness", 120)
        job.log(f"health {'OK' if ok else 'NOT answering after 120 s'}")
        return ok

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
        ActionSpec("infra.restart_litellm", "Restart LiteLLM gateway",
                   "docker restart gx-litellm. Requests in flight through the gateway fail.",
                   "caution", "cluster", restart_litellm, r.require_no_transition),
        ActionSpec("infra.restart_orchestrator", "Restart gx-orchestrator",
                   "systemctl --user restart gx-orchestrator.service. Refused while gx-max is loading or "
                   "releasing; a serving gx-max is re-adopted on start.",
                   "caution", "cluster", restart_orchestrator, r.require_no_transition),
    ]
    return {s.name: s for s in specs}
