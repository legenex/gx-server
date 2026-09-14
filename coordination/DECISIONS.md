# Decision log

Append-only. Each entry records what was decided, why, and what evidence
supported it. Decisions that change LOCKED architecture require a human.

---

## D-001 — Keep both nodes on kernel 6.17.0-1032-nvidia
**Date:** 2026-09-14 (pre-existing, confirmed this session)
**Decision:** Neither node is upgraded past `6.17.0-1032-nvidia`.
**Why:** Kernel 7.0 on gx10-02 caused `ibv_reg_mr_iova2 failed with error Cannot
allocate memory` during FlashInfer autotune, killing gx-max startup.
**Evidence:** The run on the pinned kernel this session passed autotune on rank1
and reached "server is fired up and ready to roll". The failure did not recur.
**Status:** CONFIRMED WORKING. See BLOCKERS.md B-001 — the pin is weaker than it
looks and needs an `apt-mark hold`.

## D-002 — Add `--enable-metrics` to the gx-max argument vector
**Date:** 2026-09-14
**Decision:** The gx-max launch gains `--enable-metrics`, controlled by
`GXMAX_ENABLE_METRICS` in `legenex/lifecycle/gx-max.conf` (default 1).
**Why:** The graceful-drain requirement ("allow current jobs to finish") needs a
real in-flight request count. Without `/metrics` the server exposes no queue
depth, so drain would be guesswork.
**Why this is not an architecture change:** It is purely additive observability.
It does not touch the model, engine, parallelism, memory fractions, or any
`SGLANG_*`/`B12X_*` flag. Setting `GXMAX_ENABLE_METRICS=0` reproduces the
verified argument vector exactly.
**Status:** Implemented in config; **NOT YET VERIFIED** against a live start —
the currently-running engine predates it and correctly reports in-flight as
`unknown`. Must be confirmed on the next gx-max start.

## D-003 — Orchestrator is stdlib-only
**Date:** 2026-09-14
**Decision:** `gx_orchestrator` uses only the Python standard library.
**Why:** It gates gx-max. A component on the recovery path must not depend on
pip, a venv, a wheel build, or a container registry being reachable. Python
3.12.3 is present on both nodes.
**Trade-off:** No FastAPI/pydantic ergonomics; request parsing and the SSE proxy
are hand-written. Accepted — the surface is small and fully tested.

## D-004 — Orchestrator binds loopback + docker bridge, never 0.0.0.0
**Date:** 2026-09-14
**Decision:** Default bind is `127.0.0.1,172.17.0.1` (`GX_ORCH_HOSTS`).
**Why:** LiteLLM runs in a container, so `127.0.0.1` alone is unreachable from
it. Binding `0.0.0.0` would expose a surface that can start and stop
cluster-wide jobs, with no authentication, to the LAN and the tailnet.
**Evidence:** Verified 200 from inside a container via `host.docker.internal`,
and connection refused from the LAN address `10.60.21.37`.

## D-005 — gx-max is not reachable by keyword accumulation in gx-auto
**Date:** 2026-09-14
**Decision:** gx-auto escalates to gx-max only on an explicit "extreme" marker
(the `hard` pattern category, score ≥ 3) or a context no single-node tier can
hold. Ordinary reasoning keywords cap out at gx-reason.
**Why:** The first implementation summed all complexity signals, and an ordinary
request ("debug this stack trace and derive the time complexity, then refactor")
scored 9 and selected gx-max. gx-max evicts every model on **both** nodes, so
reaching it by accident is expensive and disruptive.
**Evidence:** `tests/test_classifier.py::test_hard_reasoning_to_reason_not_max`.

## D-006 — Verified model checkpoints for gx-fast and gx-reason
**Date:** 2026-09-14
**Decision:** gx-fast = `nvidia/Qwen3.6-35B-A3B-NVFP4`;
gx-reason = `et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10`.
**Why:** Both verified HTTP 200 and ungated against the live HF API. See
MODELS.md for the full comparison and the rejected alternatives.
**Notable correction:** the repo's `Alibaba/Qwen3.5-35B-A3B-Uncensored-HauhauCS-*`
reference is a **local folder path, not an upstream ID** — fetching it returns
HTTP 401. Anything treating it as a remote source will fail.

## D-007 — gx-reason owns node 2 exclusively
**Date:** 2026-09-14
**Decision:** gx-reason (~86 GiB) is never co-scheduled with ComfyUI on node 2.
**Why:** Node 2 has 121 GiB. Media pipelines need 30–44 GiB resident. Together
they would thrash, and a ComfyUI model-set eviction costs 270–430 s to reload.
**Consequence:** The media router and llama-swap node 2 need a shared mutex.

## D-008 — gx-video is asynchronous, gx-image is synchronous
**Date:** 2026-09-14
**Decision:** `gx-image` uses OpenAI `/v1/images/generations` (synchronous);
`gx-video` uses the asynchronous `/v1/videos` create/status/content shape.
**Why:** Measured/estimated generation times on GB10 are seconds for distilled
image models but **minutes** for video. A synchronous video endpoint would just
be a timeout. LiteLLM speaks the async video shape natively.

## D-009 — gx-reason moves from vLLM to llama.cpp (engine change, same tier)
**Date:** 2026-09-14
**Decision:** gx-reason is served by **llama.cpp** with a GGUF checkpoint, not
vLLM with the NVFP4 safetensors checkpoint. The *tier intent* is unchanged: it
remains a ~122B-class sparse MoE reasoning model on node 2.

**Why — measured, not assumed.** `et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10`
(73.31 GiB) **cannot be loaded by this vLLM build on a 128 GB unified-memory
node.** Four attempts, with `--gpu-memory-utilization` at 0.61, 0.66 and 0.78,
with and without `--enforce-eager`, and with the MoE backend forced from
`FLASHINFER_CUTLASS` to `MARLIN`. Every attempt stalled on shard 1 of 3.

The decisive measurement, taken from `/proc/<pid>/status` mid-load:

```
VmRSS:    36742440 kB
RssAnon:  36612040 kB      <-- weights are ANONYMOUS memory
RssFile:     48620 kB      <-- almost nothing is file-backed
```

At that moment the node was at 111 GiB used of 121 GiB with only **half** the
weights loaded. vLLM reserves its pool up front (≈77 GiB at 0.66) and then
loads weights into *additional* anonymous memory rather than into the reserved
pool. On unified memory both come from the same 121 GiB, so the working set is
roughly `pool + checkpoint`, and the node runs out at about half of a 73 GiB
checkpoint. Swap absorbed some of it and the loader simply thrashed.

Practical ceiling for this vLLM build on these nodes: a checkpoint of roughly
**40-55 GiB**. That is consistent with gx-fast succeeding — its checkpoint is
21.8 GiB, and 2 × 21.8 is comfortably inside 121 GiB.

**Why llama.cpp fixes it.** llama.cpp mmaps GGUF weights, so they are
file-backed (`RssFile`) and reclaimable, with no second anonymous copy. This is
the standard way 100B+ models are run on a single DGX Spark.

**Is this a locked-architecture change?** ARCHITECTURE.md L-9 fixes the *stack*
(llama.cpp is part of it) and says vLLM serves "appropriate single-node Qwen
tiers **unless benchmarking gives a concrete reason otherwise**". The
measurement above is that concrete reason. The model class, the node, and the
alias are all unchanged, so this is an engine choice inside the locked
architecture, not a redesign. Flagged to the human anyway because it deviates
from the original intent.

**Checkpoint:** `unsloth/Qwen3.5-122B-A10B-GGUF`, verified HTTP 200, ungated,
quant `UD-Q4_K_XL` (77.0 GB across 3 shards) plus `mmproj-F16.gguf` (0.91 GB)
for vision. Fallback if 77 GB proves tight: `UD-IQ4_XS` (60.2 GB).

**Superseded:** D-006's gx-reason half. The gx-fast half of D-006 stands.
