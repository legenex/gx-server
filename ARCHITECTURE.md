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
