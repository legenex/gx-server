"""The Creative Flows execution engine (gx10-01).

A run is a snapshot of the flow graph plus a plan (which nodes take part and
which must execute even if cached). One coordinator thread per run:

1. validates the graph (done before the run is created) and computes the
   topological order (cycles are refused at save time);
2. repeatedly takes the runnable set (pending nodes whose in-run
   dependencies are terminal);
3. for each: bypass / skip / blocked / locked-reuse / cache hit, or starts a
   worker thread when a slot is free;
4. workers call the node executor (which submits to the existing queues:
   media jobs, gx-music, gx-voice, the gateway, FFmpeg) and persist outputs;
5. outputs feed downstream nodes; the run continues automatically and ends
   when every node is terminal; the summary is stored as history.

Resource safety: engine-wide slots per service class, at most one gx10-02
generation (image / video / music / voice) per run at a time, and a per-run
parallelism cap. The existing queues still apply their own admission (the
Resource Controller's creative gate, the router's and the supervisors'
memory checks); nothing here bypasses them.

List semantics: a port carries a list of values. A node whose single-value
input receives N items runs N times (mapped, N <= 16); multi-value inputs
receive all items. Iterator / Batch / Script Writer scenes use this.

Restart: runs are persisted continuously. On start-up every run that was
active is marked ``interrupted`` (with its nodes) and can be resumed with
Rerun failed, which reuses every finished node through the cache.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..redact import redact
from . import catalog as cat
from . import ffmpeg as ff
from .graph import PRODUCED, Graph
from .hashing import node_key
from .nodes import EXECUTORS, NodeContext, NodeResult
from .schema import readiness
from .services import Cancelled, NodeFailure, Services
from .store import FlowError

log = logging.getLogger("gx.ui.flows")

MAX_ITERATIONS = 16
FINAL_OK = frozenset({"succeeded", "cached", "bypassed", "reused", "skipped"})
FAILED_STATES = frozenset({"failed", "cancelled", "interrupted", "blocked"})
NODE2_SERVICES = frozenset({"image", "video", "music", "voice"})
CLASS_LIMITS = {"llm": 2, "image": 2, "video": 2, "music": 1, "voice": 1, "ffmpeg": 2, "http": 4, "local": 16}
MAX_ACTIVE_RUNS = 4
PER_RUN_PARALLEL = 4


@dataclass
class RunState:
    run_id: str
    flow: dict[str, Any]
    graph: dict[str, Any]
    owner: str
    user: str
    members: set[str]
    forced: set[str]
    order: list[str]
    g: Graph
    nodes: dict[str, dict[str, Any]]
    states: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)
    node_cancel: dict[str, threading.Event] = field(default_factory=dict)
    workers: dict[str, threading.Thread] = field(default_factory=dict)
    models: set[str] = field(default_factory=set)
    assets: list[str] = field(default_factory=list)
    final_assets: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    waits: list[dict[str, Any]] = field(default_factory=list)
    node2_busy: str | None = None
    ready: dict[str, tuple[dict[str, list[dict[str, Any]]], set[str], str | None]] = field(default_factory=dict)
    cv: threading.Condition = field(default_factory=threading.Condition)
    started: float = field(default_factory=time.time)


class FlowEngine:
    def __init__(self, services: Services, *, class_limits: dict[str, int] | None = None,
                 max_active_runs: int = MAX_ACTIVE_RUNS, per_run_parallel: int = PER_RUN_PARALLEL) -> None:
        self.services = services
        self.store = services.store
        limits = {**CLASS_LIMITS, **(class_limits or {})}
        self._slots = {k: threading.BoundedSemaphore(v) for k, v in limits.items()}
        self.max_active_runs = max_active_runs
        self.per_run_parallel = per_run_parallel
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()
        self.recovered = self.store.mark_interrupted()
        if self.recovered:
            log.warning("marked %d flow run(s) interrupted after a restart", len(self.recovered))

    # ============================================================ public
    def busy(self) -> bool:
        with self._lock:
            return bool(self._runs)

    def start(self, *, flow: dict[str, Any], graph: dict[str, Any], members: set[str], forced: set[str],
              mode: str, target: str | None, owner: str, user: str, parent_run: str | None = None) -> str:
        with self._lock:
            if len(self._runs) >= self.max_active_runs:
                raise FlowError(f"{self.max_active_runs} flow runs are already active; wait or cancel one", 429,
                                "too_many_runs")
            if any(st.flow["id"] == flow["id"] for st in self._runs.values()):
                raise FlowError("this flow is already running; cancel it or wait for it to finish", 409,
                                "already_running")
        nodes = {n["id"]: n for n in graph["nodes"]}
        g = Graph(list(nodes), [(e["source"], e["target"]) for e in graph["edges"]])
        order = [n for n in g.topo_order() if n in members]
        run_id = self.store.create_run(flow=flow, owner=owner, user=user, mode=mode, target=target, graph=graph,
                                       nodes=[(n, nodes[n]["type"]) for n in order], parent_run=parent_run)
        state = RunState(run_id=run_id, flow=flow, graph=graph, owner=owner, user=user, members=set(order),
                         forced=forced, order=order, g=g, nodes=nodes,
                         states={n: "pending" for n in order},
                         node_cancel={n: threading.Event() for n in order})
        with self._lock:
            self._runs[run_id] = state
        thread = threading.Thread(target=self._coordinate, args=(state,), name=f"flow-{run_id[-6:]}", daemon=True)
        thread.start()
        return run_id

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
        if state is None:
            return False
        with state.cv:
            state.cancel.set()
            for ev in state.node_cancel.values():
                ev.set()
            state.cv.notify_all()
        return True

    def cancel_node(self, run_id: str, node_id: str) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
        if state is None or node_id not in state.node_cancel:
            return False
        with state.cv:
            state.node_cancel[node_id].set()
            state.cv.notify_all()
        return True

    def wait(self, run_id: str, timeout: float = 60.0) -> bool:
        """Test helper: block until the run is finished."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if run_id not in self._runs:
                    return True
            time.sleep(0.05)
        return False

    # ======================================================= coordination
    def _set(self, st: RunState, node_id: str, status: str, detail: str = "", **values: Any) -> None:
        st.states[node_id] = status
        now = time.time()
        if status in ("succeeded", "cached", "failed", "cancelled", "skipped", "bypassed", "reused", "blocked"):
            values.setdefault("finished_at", now)
        self.store.update_node(st.run_id, node_id, status=status, detail=detail, **values)

    def _coordinate(self, st: RunState) -> None:
        self.store.update_run(st.run_id, status="running", started_at=st.started)
        self.services.metric("flow.run", flow_id=st.flow["id"], run_id=st.run_id, nodes=len(st.order),
                             outcome="started", user=f"user:{st.user}")
        try:
            self._loop(st)
        except Exception as exc:  # noqa: BLE001 - never leave a run "running"
            log.exception("flow run %s crashed", st.run_id)
            st.errors.append({"node_id": None, "message": f"engine error: {type(exc).__name__}"})
            st.cancel.set()
            for ev in st.node_cancel.values():
                ev.set()
            for t in list(st.workers.values()):
                t.join(timeout=60)
            for n, s in st.states.items():
                if s in ("pending", "queued", "running", "waiting"):
                    self._set(st, n, "failed", "the engine stopped unexpectedly", error="engine error")
        finally:
            self._finish(st)
            with self._lock:
                self._runs.pop(st.run_id, None)

    def _loop(self, st: RunState) -> None:
        while True:
            with st.cv:
                if st.cancel.is_set():
                    for n, s in st.states.items():
                        if s in ("pending", "queued"):
                            self._set(st, n, "cancelled", "the run was cancelled before this node started")
                active = [n for n, t in st.workers.items() if t.is_alive()]
                pending = [n for n, s in st.states.items() if s in ("pending", "queued")]
                if not pending and not active:
                    return
                progressed = False
                for node_id in st.g.runnable({n: ("pending" if s == "queued" else s)
                                              for n, s in st.states.items()}, st.members):
                    if st.cancel.is_set():
                        break
                    if self._decide(st, node_id):
                        progressed = True
                if not progressed:
                    st.cv.wait(timeout=0.5)

    def _incoming(self, st: RunState, node_id: str) -> tuple[dict[str, list[dict]], set[str], list[str], bool]:
        """(inputs by port, ports with values, failed upstreams, any edge whose branch was not taken)."""
        inputs: dict[str, list[dict]] = {}
        connected: set[str] = set()
        failed: list[str] = []
        not_taken = False
        for e in st.graph["edges"]:
            if e["target"] != node_id or e["source"] not in st.members:
                continue
            src_state = st.states.get(e["source"])
            if src_state in FAILED_STATES:
                failed.append(e["source"])
                continue
            if src_state == "skipped":
                not_taken = True
                continue
            if src_state not in PRODUCED:
                continue
            values = st.outputs.get(e["source"], {}).get(e["source_port"])
            if not values:
                not_taken = True
                continue
            inputs.setdefault(e["target_port"], []).extend(values)
            connected.add(e["target_port"])
        return inputs, connected, failed, not_taken

    def _decide(self, st: RunState, node_id: str) -> bool:
        """Settle or start one runnable node. True when something changed."""
        node = st.nodes[node_id]
        nt = cat.NODES[node["type"]]
        if node_id in st.ready:  # decided before; only waiting for a slot
            inputs, connected, key = st.ready[node_id]
            return self._launch(st, node_id, nt, inputs, connected, key)
        inputs, connected, failed, not_taken = self._incoming(st, node_id)
        if failed:
            names = ", ".join(sorted({st.nodes[f].get("label") or cat.NODES[st.nodes[f]["type"]].label
                                      for f in failed}))
            self._set(st, node_id, "blocked", f"not run: {names} did not finish")
            return True
        if node.get("disabled"):
            outputs = self._bypass(nt, inputs)
            st.outputs[node_id] = outputs
            self._set(st, node_id, "bypassed", "bypassed: inputs passed through" if outputs else "bypassed",
                      outputs=outputs)
            return True
        incoming_edges = any(e["target"] == node_id and e["source"] in st.members for e in st.graph["edges"])
        if not_taken and incoming_edges and not connected:
            self._set(st, node_id, "skipped", "skipped: its input branch was not taken")
            return True
        missing = self._missing(st, node, nt, connected)
        if missing:
            if not_taken:
                self._set(st, node_id, "skipped", f"skipped: {missing}")
            else:
                self._set(st, node_id, "failed", missing, error=missing)
                st.errors.append({"node_id": node_id, "message": missing})
            return True
        if node.get("locked") and node_id not in st.forced:
            last = self.store.last_outputs(st.flow["id"], node_id)
            if last is not None and self._outputs_exist(last[0]):
                st.outputs[node_id] = last[0]
                self._set(st, node_id, "reused", f"locked: reused the result of run {last[1]}", outputs=last[0])
                return True
        key = self._cache_key(st, node, nt, inputs)
        if nt.cacheable and node_id not in st.forced and key is not None:
            hit = self.store.cache_get(key)
            if hit is not None and self._outputs_exist(hit["outputs"]):
                st.outputs[node_id] = hit["outputs"]
                st.models.update(m for m in [hit["meta"].get("model")] if m)
                st.final_assets += hit["meta"].get("final_assets") or []
                self._set(st, node_id, "cached", f"unchanged: reused the result of run {hit['run_id']}",
                          outputs=hit["outputs"], cache_key=key, cached=True, model=hit["meta"].get("model"))
                return True
            if hit is not None:
                self.store.cache_drop(key)
        st.ready[node_id] = (inputs, connected, key)
        return self._launch(st, node_id, nt, inputs, connected, key)

    def _missing(self, st: RunState, node: dict, nt: cat.NodeType, connected: set[str]) -> str | None:
        trial = {"edges": [{"target": node["id"], "target_port": p} for p in connected], "nodes": [node]}
        issues = readiness(trial)
        return issues[0]["message"] if issues else None

    @staticmethod
    def _bypass(nt: cat.NodeType, inputs: dict[str, list[dict]]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for port in nt.outputs:
            want = port.types[0]
            for values in inputs.values():
                usable = [v for v in values if want == "any" or v.get("type") == want]
                if usable:
                    out[port.id] = usable
                    break
        return out

    def _outputs_exist(self, outputs: dict[str, list[dict]]) -> bool:
        ids = [v["asset_id"] for vals in outputs.values() for v in vals if v.get("asset_id")]
        if not ids:
            return True
        found = self.store.asset_sha(ids)
        return all(found.get(i) for i in ids)

    def _cache_key(self, st: RunState, node: dict, nt: cat.NodeType, inputs: dict[str, list[dict]]) -> str | None:
        ids = [v["asset_id"] for vals in inputs.values() for v in vals if v.get("asset_id")]
        identity = {a: self.services.model_identity(a) for a in nt.aliases}
        identity["backend"] = nt.backend
        try:
            return node_key(nt.type, nt.version, node.get("config") or {}, identity, inputs,
                            self.store.asset_sha(ids), st.graph.get("variables") or {})
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------- workers
    def _launch(self, st: RunState, node_id: str, nt: cat.NodeType, inputs: dict[str, list[dict]],
                connected: set[str], key: str | None) -> bool:
        running = [n for n, t in st.workers.items() if t.is_alive()]
        cls = nt.service if nt.service in self._slots else "local"
        if len(running) >= self.per_run_parallel:
            return self._queue(st, node_id, "waiting for a free slot in this run")
        if nt.service in NODE2_SERVICES and st.node2_busy and st.node2_busy != node_id:
            busy = st.nodes[st.node2_busy].get("label") or cat.NODES[st.nodes[st.node2_busy]["type"]].label
            return self._queue(st, node_id, f"waiting: {busy} is using gx10-02 (one generation at a time per run)")
        if not self._slots[cls].acquire(blocking=False):
            return self._queue(st, node_id, f"waiting for a free {cls} slot (other flows are using it)")
        if nt.service in NODE2_SERVICES:
            st.node2_busy = node_id
        st.ready.pop(node_id, None)
        self._set(st, node_id, "running", "starting", started_at=time.time(), cache_key=key, progress=None)
        worker = threading.Thread(target=self._work, args=(st, node_id, nt, inputs, connected, key, cls),
                                  name=f"flow-node-{node_id[:12]}", daemon=True)
        st.workers[node_id] = worker
        worker.start()
        return True

    def _queue(self, st: RunState, node_id: str, detail: str) -> bool:
        if st.states.get(node_id) != "queued":
            self._set(st, node_id, "queued", detail)
            return True
        return False

    def _iterations(self, nt: cat.NodeType, inputs: dict[str, list[dict]]) -> int:
        lengths = {p.id: len(inputs.get(p.id, [])) for p in nt.inputs if not p.multiple and inputs.get(p.id)}
        n = max(lengths.values(), default=1)
        if n > MAX_ITERATIONS:
            raise NodeFailure(f"{n} items arrived; a node runs at most {MAX_ITERATIONS} times per run",
                              code="too_many_items")
        for port, length in lengths.items():
            if length not in (1, n):
                raise NodeFailure(f"'{port}' has {length} items but another input has {n}; lists must match",
                                  code="list_mismatch")
        return n

    def _work(self, st: RunState, node_id: str, nt: cat.NodeType, inputs: dict[str, list[dict]],
              connected: set[str], key: str | None, cls: str) -> None:
        node = st.nodes[node_id]
        started = time.time()
        jobs: list[dict[str, Any]] = []
        config = {f.id: f.default for f in nt.fields if f.default is not None}
        config.update(node.get("config") or {})

        def on_log(msg: str) -> None:
            self.store.append_log(st.run_id, node_id, msg)

        last_status: dict[str, Any] = {}

        def on_status(state: str, detail: str = "", *, resource: dict | None = None,
                      progress: float | None = None) -> None:
            status = state if state in ("running", "waiting", "queued") else "running"
            if resource and (not st.waits or st.waits[-1].get("node_id") != node_id):
                st.waits.append({"node_id": node_id, "code": resource.get("code"),
                                 "reason": str(resource.get("reason") or "")[:200], "at": time.time()})
            current = {"status": status, "detail": detail, "resource": resource, "progress": progress}
            if current == last_status:
                return
            last_status.clear()
            last_status.update(current)
            st.states[node_id] = status
            self.store.update_node(st.run_id, node_id, status=status, detail=detail, resource=resource,
                                   progress=progress)

        def on_job(kind: str, job_id: str) -> None:
            jobs.append({"kind": kind, "id": job_id, "at": time.time()})
            self.store.update_node(st.run_id, node_id, jobs=jobs)

        status, detail, error = "failed", "", None
        result = NodeResult()
        payloads: list[dict] = []
        models: list[str] = []
        try:
            if not nt.available:
                raise NodeFailure(nt.unavailable_reason, code="unavailable")
            executor = EXECUTORS[nt.type]
            count = self._iterations(nt, inputs)
            merged: dict[str, list[dict]] = {}
            for i in range(count):
                cancel = st.node_cancel[node_id]
                if cancel.is_set():
                    raise Cancelled()
                these = {p: (vals if (nt.input(p) and nt.input(p).multiple) or len(vals) == 1  # type: ignore[union-attr]
                             else [vals[i]]) for p, vals in inputs.items()}
                if count > 1:
                    on_log(f"item {i + 1} of {count}")
                    on_status("running", f"item {i + 1} of {count}", progress=i / count)
                ctx = NodeContext(
                    services=self.services, run_id=st.run_id, flow_id=st.flow["id"],
                    flow_name=st.graph.get("name") or st.flow.get("name") or "Flow", node_id=node_id, node=node,
                    nt=nt, config=config, inputs=these, connected=connected,
                    variables=dict(st.graph.get("variables") or {}), user=st.user, owner=st.owner,
                    cancel=cancel, force=node_id in st.forced, iteration=i, iterations=count,
                    on_log=on_log, on_status=on_status, on_job=on_job)
                part = executor(ctx)
                for port, values in part.outputs.items():
                    merged.setdefault(port, []).extend(values)
                if part.model:
                    models.append(part.model)
                if part.payload:
                    payloads.append(part.payload)
                result.final_assets += part.final_assets
                result.meta.update(part.meta)
            result.outputs = merged
            status = "succeeded"
            detail = self._describe(merged)
        except Cancelled:
            status, detail = "cancelled", "cancelled"
        except NodeFailure as exc:
            status, detail, error = "failed", str(exc)[:300], redact(str(exc))[:1000]
            result.meta["error_code"] = exc.code
            result.meta["retryable"] = exc.retryable
        except ff.ComposeError as exc:
            status, detail, error = "failed", str(exc)[:300], redact(str(exc))[:1000]
        except Exception as exc:  # noqa: BLE001 - an executor bug must not kill the run
            log.exception("flow node %s (%s) crashed", node_id, nt.type)
            status, detail = "failed", f"internal error ({type(exc).__name__}); see the Control Center log"
            error = detail
        finally:
            self._slots[cls].release()
        model = " | ".join(dict.fromkeys(models)) or None
        produced = [v["asset_id"] for vals in result.outputs.values() for v in vals if v.get("asset_id")]
        with st.cv:
            if st.node2_busy == node_id:
                st.node2_busy = None
            if status == "succeeded":
                st.outputs[node_id] = result.outputs
                st.assets += [a for a in produced if a not in st.assets]
                st.final_assets += [a for a in result.final_assets if a not in st.final_assets]
                if model:
                    st.models.add(model)
            elif status == "failed":
                st.errors.append({"node_id": node_id, "message": error, "code": result.meta.get("error_code")})
            self._set(st, node_id, status, detail, outputs=result.outputs, error=error, model=model, jobs=jobs,
                      payload={"iterations": payloads[:16], "config": config, "meta": _small(result.meta)},
                      progress=1.0 if status == "succeeded" else None)
            st.cv.notify_all()
        if status == "succeeded" and nt.cacheable and key is not None:
            meta = {"model": model, "final_assets": result.final_assets}
            self.store.cache_put(key, node_type=nt.type, outputs=result.outputs, meta=meta, flow_id=st.flow["id"],
                                 node_id=node_id, run_id=st.run_id)
        self.services.metric("flow.node", flow_id=st.flow["id"], run_id=st.run_id, node_id=node_id,
                             node_type=nt.type, outcome={"succeeded": "ok"}.get(status, status),
                             duration_ms=round((time.time() - started) * 1000),
                             error_code=result.meta.get("error_code"), user=f"user:{st.user}")

    @staticmethod
    def _describe(outputs: dict[str, list[dict]]) -> str:
        parts = []
        for port, values in outputs.items():
            kinds = {v.get("type") for v in values}
            parts.append(f"{len(values)} {'/'.join(sorted(str(k) for k in kinds))} on {port}")
        return "done: " + ", ".join(parts) if parts else "done"

    def _finish(self, st: RunState) -> None:
        states = dict(st.states)
        counts: dict[str, int] = {}
        for s in states.values():
            counts[s] = counts.get(s, 0) + 1
        if st.cancel.is_set():
            status = "cancelled"
        elif any(s in FAILED_STATES for s in states.values()):
            status = "failed"
        elif all(s in FINAL_OK for s in states.values()):
            status = "succeeded"
        else:
            status = "failed"
        finished = time.time()
        summary = {"counts": counts, "models": sorted(st.models), "assets": st.assets,
                   "final_assets": st.final_assets or st.assets[-1:], "errors": st.errors[:20],
                   "resource_waits": st.waits[:40], "nodes": len(states),
                   "cached": counts.get("cached", 0) + counts.get("reused", 0),
                   "executed": counts.get("succeeded", 0)}
        error = None
        if status == "failed":
            first = next((e for e in st.errors if e.get("message")), None)
            error = (first or {}).get("message") or "one or more nodes did not finish"
        self.store.update_run(st.run_id, status=status, finished_at=finished, summary=summary, error=error)
        self.services.metric("flow.run", flow_id=st.flow["id"], run_id=st.run_id, nodes=len(states),
                             outcome={"succeeded": "ok"}.get(status, status),
                             duration_ms=round((finished - st.started) * 1000), user=f"user:{st.user}",
                             error_code=(st.errors[0].get("code") if st.errors else None))


def _small(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v if not isinstance(v, str) else v[:300]
        elif isinstance(v, (list, dict)) and len(str(v)) < 2000:
            out[k] = v
    return out


def plan_for(graph: dict[str, Any], mode: str, node_id: str | None,
             failed: list[str] | None = None) -> tuple[set[str], set[str]]:
    g = Graph([n["id"] for n in graph["nodes"]], [(e["source"], e["target"]) for e in graph["edges"]])
    return g.plan(mode, node_id, failed or [])


RunStarter = Callable[..., str]
