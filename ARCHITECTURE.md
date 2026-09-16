# gx-cluster architecture

Two-node NVIDIA DGX Spark / ASUS GX10 local AI cluster.
Last reviewed: 2026-09-14.

---

## 1. Locked decisions

These are settled. A future agent MUST NOT change any of them without an
explicit human decision. If a task seems to require changing one, stop and ask.

| # | Decision | Why |
|---|---|---|
| L-1 | **Two separate 128 GB nodes.** They are NOT a coherent 256 GB pool. | Physical reality. Distributed frameworks may shard across both, but memory budgeting is always per-node. |
| L-2 | **Node roles are fixed.** gx10-01 = control/dev/gateway/lifecycle/gx-mini/gx-fast. gx10-02 = compute/gx-reason/media/ComfyUI/rank1. | Keeps the control plane off the node that gets evicted for media work. |
| L-3 | **Tailscale is management only.** Model and distributed traffic run ONLY on the ConnectX/RoCE fabric. | Tailscale is a userspace WireGuard mesh; routing NCCL over it would collapse throughput. |
| L-4 | **Kernel pinned to `6.17.0-1032-nvidia` on both nodes.** Never upgrade to 7.0. | Kernel 7.0 caused `ibv_reg_mr_iova2 failed: Cannot allocate memory` during FlashInfer autotune on gx10-02. See D-001. |
| L-5 | **Do not attempt GPUDirect RDMA**, `nvidia-peermem`, GDRCopy, or `NCCL_NET_GDR_LEVEL` hacks. | DGX Spark does not support GPUDirect RDMA in this topology. NET/IB with staged pinned memory is the expected and working path. |
| L-6 | **gx-max = SGLang, TP=2, 2 nodes, `nvidia/DeepSeek-V4-Flash-0731-NVFP4`.** Never vLLM. Never a different model. Never a silent downgrade. | This is the flagship tier and the only reason the second node exists in the inference path. |
| L-7 | **Do not modify** MTU, Netplan, RDMA setup, ConnectX firmware, or routing without concrete evidence of a fault. | The fabric is measured-good (~21.3 GB/s bus bandwidth, zero errors). |
| L-8 | **`/swapfile-sglang` (48 G) stays on both nodes.** | Load-time OOM mitigation for gx-max weight loading. |
| L-9 | **The stack is LiteLLM + llama-swap + llama.cpp + vLLM + SGLang + ComfyUI.** Do not replace it with Ollama. | Each engine is chosen per tier for a concrete reason; see MODELS.md. |

## 2. Physical layout

```
                    ┌──────────────── Tailscale (management only) ────────────────┐
                    │                                                             │
            ┌───────┴────────┐                                     ┌──────────────┴─┐
            │   gx10-01      │                                     │    gx10-02      │
            │  control node  │                                     │  compute node   │
            │  128 GB unified│                                     │  128 GB unified │
            ├────────────────┤                                     ├─────────────────┤
            │ LiteLLM  :4000 │                                     │ gx-reason (vLLM)│
            │ orchestrator   │                                     │ ComfyUI (media) │
            │           :18900                                     │ gx-max rank 1   │
            │ llama-swap:8080│                                     │ llama-swap :8080│
            │ gx-mini   :19001                                     │                 │
            │ gx-fast        │                                     │                 │
            │ gx-max rank 0  │                                     │                 │
            │           :30000                                     │                 │
            └───────┬────────┘                                     └────────┬────────┘
                    │                                                       │
      rail A  192.168.100.10 ◄────────── ConnectX / RoCE ──────────► 192.168.100.11
      rail B  192.168.101.10 ◄────────── ConnectX / RoCE ──────────► 192.168.101.11
                         (all model + NCCL traffic, both rails active)
```

**Verified 2026-09-14:** a single 400-token gx-max generation moved **772 MB** of
RDMA traffic, split near-evenly across both rails (391.6 MB + 380.8 MB measured
on `port_xmit_data`). Tailscale carried only SSH during the same window.

## 3. Layer separation

The system separates three concerns that are easy to conflate:

| Layer | Component | Responsibility |
|---|---|---|
| **Gateway** | LiteLLM `:4000` | One OpenAI-compatible entry point. Auth, accounting, alias table. Knows nothing about processes. |
| **Routing** | orchestrator `:18900` (`gx-auto`) | Chooses a *tier* for a request. Pure decision logic. Starts nothing. |
| **Lifecycle** | orchestrator + llama-swap | Ensures the *process* for a tier exists. On-demand load, idle TTL, drain, unload. Chooses nothing. |

Routing and lifecycle are deliberately NOT the same thing. `gx-auto` decides
*which* tier; llama-swap and the gx-max lifecycle decide *whether the engine for
that tier is currently running* and start it if not.

## 4. Request paths

```
client ──► LiteLLM :4000
             ├── gx-mini   ──► llama-swap node1 ──► llama.cpp   :19001
             ├── gx-fast   ──► llama-swap node1 ──► vLLM
             ├── gx-reason ──► llama-swap node2 ──► vLLM        (node 2)
             ├── gx-image  ──► media router     ──► ComfyUI     (node 2)
             ├── gx-video  ──► media router     ──► ComfyUI     (node 2, async)
             ├── gx-auto   ──► orchestrator :18900 ──► (classify) ──► back to LiteLLM
             └── gx-max    ──► orchestrator :18900 ──► acquire both nodes ──► SGLang :30000
```

`gx-max` deliberately does NOT point at `:30000` directly. Everything goes
through the orchestrator so that acquisition, draining and the "never downgrade"
guarantee are enforced in exactly one place.

## 5. gx-max lifecycle

gx-max is the only tier that takes over the whole cluster.

```
  DOWN ──acquire()──► ACQUIRING ──health ok──► READY ──idle > TTL──► RELEASING ──► DOWN
    ▲                     │                                              ▲
    └─────failure─────────┘                          release()───────────┘
```

Acquisition sequence (`legenex/lifecycle/gx-max-start.sh`):

1. **preflight** — model dirs present on both nodes, image present on both,
   both ConnectX rails answer, enough free memory.
2. **drain** — stop conflicting GPU workloads gracefully (SIGTERM, 60 s grace)
   on both nodes. In-flight work is allowed to finish.
3. **start rank 1** on gx10-02 (it retries against the rank-0 bootstrap store).
4. **start rank 0** on gx10-01.
5. **wait for health** on `:30000/health`, budget 1800 s.

Release (`gx-max-stop.sh`) drains the queue first (default 300 s), then tears
down rank 0 before rank 1, then restores normal single-node workloads.

Concurrency is serialised by a condition variable in `GxMaxLifecycle`: five
simultaneous callers produce exactly one invocation of the start script
(covered by `tests/test_lifecycle.py::test_concurrent_acquire_starts_script_once`).

**Memory policy (2026-09-16, `coordination/DECISIONS.md` D-025, supersedes
D-022 and B-022).** gx-max serves on this hardware with the verified `4b96e49`
launch vector, which matches the official SGLang DGX Spark NVFP4 cell. Two
things make that work, and both are architectural:

* **No cgroup memory cap on the rank containers.** `--memory X --memory-swap X`
  gives the container zero swap. The load transient (weights staged in host
  memory while the ~84 GiB parameter store is already resident) must be able
  to spill into `/swapfile-sglang` (L-8), and then it drains. Measured peaks:
  64 GiB of swap on node 1 and 53 GiB on node 2. Steady state is about 15 and
  17 GiB MemAvailable, with swap flat.
* **gx-max is admitted as a cluster takeover, not as a workload.** The
  ordinary `estimate + 30 GiB reserve <= node` check still governs every
  single-node tier. gx-max separates three phases:

  | Phase | How it is handled |
  |---|---|
  | Pre-launch clean state (admission, per node, under the node lock) | drained; no other large or exclusive resident; swapfile active; ≥ 40 GiB swap free; ≥ 100 GiB MemAvailable; no PSI pressure; management plane healthy |
  | Startup transient (~117 GiB) | policed live, never admitted against |
  | Steady-state residency | 105 GiB per rank in the ledger |

**Live safety is node-local on both nodes** (`gx-max-safety.sh`):

* **Abort immediately** on a kernel OOM kill or a hard `NV_ERR_NO_MEMORY`.
* **Abort only when sustained:** memory and swap both exhausted, swap
  thrashing, or fork/exec starvation.
* **Steady phase only:** a sustained MemAvailable floor.

A single deep MemAvailable sample is expected during a healthy load and is
not an abort.

Two structural fixes from the same investigation are in place and do not depend
on that decision:

* **Failure unwind.** Any failure after a rank has been launched runs
  `gx-max-unwind.sh` on both nodes from an EXIT trap that no path can miss
  (rank died, readiness timeout, `set -e`, SIGINT/SIGTERM). A launch that is
  *refused* — which now means every gx-max launch — restores the workloads its
  drain stopped, instead of leaving the cluster with no service.
* **Node-2 deadman.** `rank1-deadman.sh` runs on node 2, is armed before rank0
  starts, and force-removes rank1 when rank0 disappears. It needs nothing from
  outside the host, which is the whole point: every previous cleanup path
  required ssh to node 2 at exactly the moment node 2 was starved (B-020).
* **Node-1 watcher.** `rank0-watch.sh` is armed when the engine becomes READY.
  It unwinds both nodes if either rank disappears, if node 2 is unreachable
  for 180 s, or if `/health` fails for 180 s. Graceful stop and unwind disarm
  it first.

**Never-downgrade rule.** A request that explicitly names `gx-max` and cannot be
served returns HTTP 503 with an explicit message. It is never answered by a
smaller model. `gx-auto` is the only path allowed to route around a busy gx-max,
and when it does so it records `downgraded_from` in the routing log.

## 6. gx-auto routing

Deterministic, no ML, no network calls. Fully unit-tested as a pure function.

Signals: estimated prompt tokens (pessimistic, 3.2 chars/token), requested
`max_tokens`, presence of image parts, presence of tool definitions, a
keyword-derived complexity score split into four categories (`reasoning`,
`tool`, `trivial`, `hard`), and a latency preference.

Tier selection:

| Condition | Tier |
|---|---|
| context > largest single-node window | gx-max |
| `hard` category score ≥ 3 (explicitly extreme markers) | gx-max |
| total complexity ≥ 4 | gx-reason |
| total complexity ≥ 1, or tool definitions present | gx-fast |
| otherwise | gx-mini |

Two deliberate design points:

* **gx-max is not reachable by accumulating ordinary reasoning keywords.** It
  evicts every other model on both nodes, so escalation requires an explicit
  "extreme" marker or a context nothing else can hold. A hard debugging or
  refactoring request is a **gx-reason** task.
* **Vision is a capability, not a tier.** If a request carries images, the
  router lands on a tier whose model actually accepts them. There is no
  `gx-vision` alias.

The escalation threshold is *derived* from the tier table
(`MAX_SINGLE_NODE_CONTEXT`), so it cannot drift out of sync when a tier's served
context changes.

Every decision is logged as JSON with the features and the reasons that produced
it, under logger `gx.routing`.

## 7. Security boundaries

* LiteLLM `:4000` is the only intended client-facing surface.
* The orchestrator binds `127.0.0.1:18900` by default — it is a control surface
  (it can start and stop cluster-wide jobs) and must never be exposed
  unauthenticated.
* ComfyUI must bind loopback only: its `/prompt` endpoint executes arbitrary
  graphs and `/view` is a file-read primitive, both unauthenticated.
* Secrets come from the environment only. No credentials in URLs, source,
  logs, or git history.
* **Known gap:** SGLang `:30000` currently binds `0.0.0.0` with no auth. See
  BLOCKERS.md B-003.

## 8. Node roles under each operating state

| State | gx10-01 | gx10-02 |
|---|---|---|
| Normal | LiteLLM, orchestrator, llama-swap, gx-mini hot, gx-fast on demand | gx-reason on demand, ComfyUI on demand |
| gx-max active | rank 0 + control plane only; gx-mini/gx-fast evicted | rank 1 only; gx-reason and ComfyUI evicted |
| Recovering | gateway + orchestrator restart first, then gx-mini | media/reason start on demand |

gx-max is never started at boot.

## 9. Resource ownership (added 2026-09-14, after the gx10-02 mmap-thrash incident)

**Incident that motivated this section:** node 2 was wedged by a second
~77 GB mmap'd model landing on top of an already-resident ~77 GB gx-reason —
started with a bare `docker run` that bypassed llama-swap's own model-group
exclusivity entirely. Because mmap pages are reclaimable the OOM killer did
not fire; the node thrashed into userspace-starvation instead of shedding
load. Full account: `coordination/BLOCKERS.md` B-012.

**Principle:** no workload — human, agent, or script — may load a
medium/large/exclusive model onto a node without first clearing a hard,
non-bypassable admission check. This is enforced in code, in exactly one
place per language, not left as a convention:

- `legenex/orchestrator/gx_orchestrator/resource_guard.py` (Python) and
  `legenex/lifecycle/resource-guard.sh` (bash) share **one** admission
  formula — bash shells out to the same Python module for the arithmetic,
  so there is never a second implementation to drift out of sync.
- **Workload classes:** `small` / `medium` / `large` / `exclusive`. gx-mini
  is small (~10 GiB), gx-fast medium (~25 GiB), gx-reason large (~95 GiB —
  the measured B-011 footprint, not the older 78 GiB design estimate;
  reconcile this discrepancy when gx-reason is next touched), gx-max's rank0
  and rank1 are each exclusive (~90 GiB, generous ceiling above the ~90-93
  GiB measured working set). `exclusive` also covers gx-reason (D-007: it
  owns node 2 alone) and any future large media workload.
- **Hard admission guard:** before any large/exclusive launch, both a
  JSON-persisted residency ledger AND a live read of `/proc/meminfo`
  (`MemAvailable`, not process RSS — unified-memory CUDA/vLLM allocations do
  not reliably show up in RSS, see D-009) must independently show the launch
  leaves at least a **30 GiB reserve**. Either check failing refuses the
  launch outright; nothing downstream ever runs.
- **Cross-process/cross-language locking:** a real `flock`-backed
  `NodeLock`, one per node. A bash `flock` and Python's `fcntl.flock` on the
  identical path contend correctly with each other — proven by test, not
  asserted (`legenex/orchestrator/tests/test_resource_guard.py`,
  `legenex/lifecycle/tests/test_resource_guard_sh.py`).
- **Never kill active work casually:** `gx-max-stop.sh`'s default is a
  graceful drain (wait for in-flight requests, or a fixed quiet period if
  `/metrics` isn't exposed) before teardown; `--force` skips this only for
  an already-failed/partial acquisition being unwound.
- **Stale-state recovery:** a `ResidencyLedger` entry left by a killed
  process, a container that no longer exists, or a node that rebooted is
  reconciled against `docker inspect` rather than trusted blindly; a
  SIGKILLed lock holder never leaves a permanently stuck lock (proven by
  test).
- **The sanctioned launch path:** `legenex/lifecycle/gx-safe-run.sh` is the
  documented replacement for a bare `docker run` on any medium/large/
  exclusive container — including one-off diagnostics, which is exactly
  what caused the incident. `gx-max-start.sh`/`gx-max-stop.sh` route both
  rank launches through the identical guard (`GXMAX_FORCE_DRAIN` can no
  longer bypass it, unlike before this section was added).
- **gx-max acquires both leases atomically or unwinds.** If node 2 is
  unavailable, acquisition fails fast at preflight (verified live: a real
  attempt against the actually-wedged node 2 died at the SSH-timeout step in
  ~10s, before any `docker run`). If a rank is left running by a *later*
  failure (e.g. the health probe never answers after rank0 already
  started), `GxMaxLifecycle._do_acquire()` now unwinds it via a best-effort
  `gx-max-stop.sh --force` call before reporting DOWN, rather than leaking
  an ~80-90 GiB resident rank with no lease on it.

**Host-level backstop, independent of the admission guard above:** every
single-node model container also carries a static Docker `--memory` cap
(gx-max ranks deliberately do not; see §5 and D-025) and a positive
`--oom-score-adj` (700-950; the gateway/db/llama-swap get 100)
so the kernel's OOM killer sacrifices these before host daemons, even if a
container is ever started outside the guarded path. `legenex/host/
gx-hostwatch.sh` is a dependency-free watchdog (systemd `--user` timer)
checking sshd at the banner level (not just TCP-accept — the exact
distinction that would have caught B-012's symptom), tailscaled, general
responsiveness, and memory pressure (`MemAvailable` + PSI); it only logs and
alerts, it does not remediate.

**What this does NOT do, on purpose:**
- It does not protect sshd/tailscaled/systemd/NetworkManager *directly* —
  their cgroups are root-owned (`memory.max` is `root:root 644`), confirmed
  by inspection, not assumed. That needs a human with sudo; see
  `coordination/BLOCKERS.md`.
- It does not arm the hardware watchdog (`/dev/watchdog`, currently
  unarmed) — arming an automatic-reboot mechanism is a hardware-safety
  decision, not a routine one. Exact config for a human is in
  `coordination/BLOCKERS.md`.
- Node 2 has no deployed ledger yet (unreachable as of this writing); its
  rank1 launch uses a real remote `flock` as a documented convention, not
  yet backed by residency-ledger arithmetic. Full parity needs this
  session's `legenex/lifecycle/` and `legenex/orchestrator/` deployed there
  once it's reachable.
- It cannot stop a human or agent from still typing `docker run` directly.
  `gx-safe-run.sh` is a documented, one-line-longer sanctioned alternative,
  not a kernel-enforced prohibition.


## 10. Source control and configuration distribution (D-026)

| Node or service | Role |
|---|---|
| gx10-01 | **Sole Git writer.** `ops/git-sync/node1-autosync.sh` commits after 45 quiet seconds, behind a secret-scan and path gate. |
| GitHub `legenex/gx-server` `main` | Canonical remote and off-machine backup. **Public.** |
| gx10-02 | **Pull-only mirror.** Reconciled to `origin/main` immediately after every push (SSH trigger) and every minute (timer). Drift is saved as evidence, then reset. Its push URL is disabled. |

A daily integrity audit on both nodes checks HEAD equality across all three,
byte-identical critical files, and that no secrets, weights or large binaries
are tracked.

**Boundaries.** Git carries code, configuration templates and documentation
only. Everything else lives outside the checkout:

| What | Where |
|---|---|
| Weights | `/srv/models` |
| Logs | `/srv/logs` |
| Locks, ledgers and the sync role | `/srv/projects/gx-cluster/state` |
| Secrets | `/srv/projects/gx-cluster/secrets`, or ignored `.env` files |

Live service copies outside the checkout (`~/gx-gateway`, `~/gx-media`,
`~/.gx-guard` on node 2) are compared against the repo by the audit, but
deploying them stays a deliberate operator step.
