# Architecture

## Architecture

**Two separate computers, not one big one.** Each GX10 has 128 GB of
*unified* memory (about 121 GiB usable) shared by its CPU and its GB10 GPU.
The two nodes do **not** pool memory. There is no 256 GB machine here: every
model is budgeted against the memory of the node it runs on.

```
            Tailscale (management only: SSH, UI, client access)
   ┌──────────────────────── 100.105.214.61 ─ 100.73.238.4 ─────────────────────┐
   │                                                                             │
┌──┴───────────────────────────┐   RoCE rail 1  192.168.100.10 ↔ .11   ┌─────────┴────────────────────┐
│ gx10-01  (control node)      │══════════════════════════════════════│ gx10-02  (compute/media)     │
│  LiteLLM gateway :4000       │   RoCE rail 2  192.168.101.10 ↔ .11   │  llama-swap (gx-reason)       │
│  gx-orchestrator :18900      │══════════════════════════════════════│  media router :18800          │
│  llama-swap (gx-mini, fast)  │      200 Gb/s each, ConnectX-7        │  ComfyUI (loopback)           │
│  control UI :8088            │                                       │  gx-max rank 1 (when loaded)  │
│  gx-max rank 0 (when loaded) │                                       │                               │
└──────────────────────────────┘                                       └───────────────────────────────┘
```

| Layer | Component | Job |
|---|---|---|
| Gateway | LiteLLM (gx10-01) | The single OpenAI-compatible API, keys, limits, no silent fallbacks |
| Routing + lifecycle | gx-orchestrator (gx10-01) | `gx-auto` routing; `gx-max` acquire/release |
| Model processes | llama-swap (one per node) | Starts/stops gx-mini, gx-fast (node 1) and gx-reason (node 2) on demand |
| Engines | llama.cpp, vLLM, SGLang | gx-mini on llama.cpp, gx-fast/gx-reason on vLLM, gx-max on SGLang |
| Media | gx-media-router + ComfyUI (gx10-02) | gx-image and gx-video |
| Host safety | admission guard, hostwatch, gx-max safety, deadman, watcher | Keep both nodes alive |
| Management | this control UI | Observe and run sanctioned operations |

**Where distributed inference is used:** only for `gx-max`. It runs DeepSeek
V4 Flash as two tensor-parallel shards (TP=2): rank 0 on gx10-01 and rank 1
on gx10-02. The ranks exchange activations over **both** ConnectX-7 RoCE
rails. Every other alias runs entirely on one node.

**Network rule:** model and NCCL traffic uses only the RoCE fabric.
Tailscale carries management and client traffic only.

**Locked decisions** (change only with a human decision): kernel
`6.17.0-1032-nvidia` on both nodes; gx-max is SGLang TP=2 across two nodes
with DeepSeek-V4-Flash-0731 (`dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4`, D-032); node roles as above; no GPUDirect
RDMA; the stack is LiteLLM + llama-swap + llama.cpp + vLLM + SGLang + ComfyUI.

## Resource safety

Several independent layers keep a 121 GiB node from being over-committed.

1. **llama-swap groups.** Node 1 keeps `gx-mini` resident and allows only one
   heavy model (`gx-fast`) at a time. Node 2 allows one heavy model
   (`gx-reason`).
2. **Admission guard** (`gx_orchestrator/resource_guard.py`). Before a large
   launch it refuses unless both the residency ledger and the live
   `MemAvailable` leave a **30 GiB reserve**. It holds a per-node `flock` so
   two launches cannot race. The control UI shows the same verdict before it
   lets you load a model.
3. **gx-max takeover policy.** gx-max is an exclusive two-node takeover, so
   instead of "estimate + 30 GiB" it requires a clean start: both nodes
   drained, `/swapfile-sglang` active, at least 40 GiB swap free, at least
   100 GiB `MemAvailable`, no memory pressure and a healthy management plane.
4. **Live safety** (`gx-max-safety.sh`) on both nodes during a gx-max load:
   aborts immediately on a kernel OOM kill or a hard driver OOM, and on
   exhaustion or thrashing only when it is sustained.
5. **Deadman and watcher.** `rank1-deadman.sh` on gx10-02 removes rank 1 if
   rank 0 disappears. `rank0-watch.sh` on gx10-01 unwinds both ranks if
   rank 0 dies while serving.
6. **hostwatch** runs every minute on both nodes (read-only) and records
   memory, PSI, responsiveness and Tailscale health.
7. **OOM priority.** Model containers carry `--oom-score-adj` 700–950 so the
   kernel kills a model before sshd, tailscaled or the gateway.

> **Warning** `docker stats` and container memory limits do **not** show a
> model's real footprint on this hardware: the GPU pool is not charged to the
> container cgroup. Always judge by the node's `MemAvailable`.

## Queueing

* **LiteLLM** rejects excess work with HTTP 429 above 32 parallel requests
  instead of queueing into a cold-start stampede.
* **llama-swap** serialises model starts. A request to an unloaded model
  waits while it loads (seconds for gx-mini, minutes for gx-fast/gx-reason).
* **gx-max** has a single-writer lifecycle. Concurrent requests queue behind
  one acquisition; the Jobs page shows the number of waiting requests and the
  current phase. Only one acquisition ever runs.
* **Media** has one global generation slot. Images wait up to 15 minutes for
  it (then HTTP 503 with `Retry-After`). Videos queue behind a single worker.
* **Control UI operations** that change model state are serialised: while one
  runs, the others are refused with a clear message.
