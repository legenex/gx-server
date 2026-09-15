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

## D-010 — Media traffic uses the ConnectX fabric, not Tailscale
**Date:** 2026-09-14
**Decision:** the media router listens on `192.168.100.11:18800` (ConnectX
fabric). `gx-image`/`gx-video` traffic from the node-1 gateway crosses the RoCE
rails like every other model tier.

**Why this needed deciding:** the node-2 worker's `COMFYUI_API.md` asserts the
opposite — that media should use Tailscale/LAN because "the 192.168.100.x fabric
is reserved for distributed inference". That constraint does not come from the
locked architecture.

**Resolution:** ARCHITECTURE.md L-3 says Tailscale is **management and remote
access only**, and that model traffic runs on the fabric. Generated images and
video frames are model traffic — a 1328² PNG or a 49-frame clip is exactly the
kind of payload L-3 exists to keep off a userspace WireGuard mesh. The fabric is
also idle whenever gx-max is not running, which is precisely when media runs.

If this is ever reversed it is one line in `docker-compose.media.yml` `ports:`
plus the `api_base` for gx-image/gx-video in `litellm/config.yaml`.

## D-011 — Media router is stdlib-only and builds the ComfyUI graph itself
**Date:** 2026-09-14
**Decision:** `gx-media-router` has zero third-party dependencies, and never
forwards caller-supplied graph structure, node ids, model filenames or paths to
ComfyUI.

**Why:** ComfyUI's `POST /prompt` executes an arbitrary graph — it is remote code
execution by design — and `GET /view` is an unauthenticated file-read primitive.
So ComfyUI binds loopback only and the router is the sole ingress. The router
builds the graph from vetted templates in `legenex/media/workflows/`, each
carrying a `_gx` binding block that declares exactly which node inputs a request
may influence; everything else is unreachable from the network.

Stdlib-only for the same reason as the orchestrator (D-003): no aarch64 wheel
risk, nothing to pin, and it starts on a node where pip has never run.

## D-012 — Resource admission control shares one formula across bash and Python
**Date:** 2026-09-14
**Decision:** `legenex/orchestrator/gx_orchestrator/resource_guard.py` is the
single source of truth for workload sizing and admission math.
`legenex/lifecycle/resource-guard.sh` shells out to the same module rather
than re-implementing the arithmetic in bash.
**Why:** B-012 happened because a bash script (a one-off diagnostic) bypassed
the Python-side lifecycle logic entirely. Any design with two independent
implementations of "is this launch safe" would eventually drift, and a
launch-time safety check that can silently disagree with itself is worse
than none. One shared module, called from both languages, cannot drift.
**Evidence:** `legenex/lifecycle/tests/test_resource_guard_sh.py::
test_bash_flock_and_python_nodelock_contend_for_the_identical_lock` proves a
bash `flock` and Python's `fcntl.flock` on the same path block each other in
both directions.

## D-013 — gx-reason's admission-control sizing uses the measured B-011
footprint (95 GiB), not the original 78 GiB design budget
**Date:** 2026-09-14
**Decision:** `resource_guard.WORKLOAD_SIZING["gx-reason"]` is set to 95 GiB.
**Why:** BLOCKERS.md B-011 measured `VmRSS` at 99,766,300 kB (~95.1 GiB) for
the actual resident gx-reason process, but `node02.yaml`'s own written
memory-budget comment still says 78 GB. Sizing the admission guard at the
smaller, stale figure would let the guard itself under-estimate gx-reason's
footprint — exactly the kind of quiet drift this whole layer exists to
prevent. The discrepancy is flagged, not silently resolved: whoever next
fixes B-011 (gx-reason's garbage-output bug) should reconcile which number
is right once the model is actually working correctly again, since a broken
model's memory footprint may not be its fixed one.
**Status:** the admission guard uses 95 GiB; `node02.yaml`'s comment still
says 78 GB and was deliberately left alone pending that reconciliation.

## D-014 — Tier health reflects each tier's real upstream, not gateway liveness
**Date:** 2026-09-14
**Decision:** `TierHealth` (now `gx_orchestrator/health.py`) probes node 1's
and node 2's own llama-swap `/v1/models` endpoints per tier, instead of a
single LiteLLM `/models` probe applied to every tier.
**Why:** LiteLLM's `/models` answers from static config regardless of
whether the real upstream is reachable. Live effect before this fix:
`/health/detailed` reported `gx-reason: true` while node 2 was completely
wedged — a model that could not possibly serve a request was reported
healthy. Node 2's probe uses a short 2s timeout with no retry, since a
"kernel alive, userspace starved" node (B-012's exact symptom) would
otherwise stall every cached health-check refresh for however long the
default timeout is.
**Evidence:** live-verified against the actually-wedged node 2:
`/health/detailed` now returns `gx-reason: {"state":"unavailable",
"reason":"node2_offline"}`, whole probe bounded at ~2.0s.

## D-015 — Qwen3.8 permanently retired; not gx-fast
**Date:** 2026-09-14
**Decision:** `vllm-qwen38-uncensored` (a standalone, unmanaged, always-on
vLLM container, ~80 GiB resident) and every active runtime/download/routing/
lifecycle reference to Qwen3.8 in this repo are removed.
**Why:** it was never part of the seven-alias tier set (L-10), held ~80 GiB
with no lifecycle management or admission control, and left as little as
~9 GiB available system-wide — a direct memory-safety risk discovered
alongside the node-2 incident. The human operator removed the container and
its checkpoint directly.
**Evidence:** `c0076f8`. Historical/comparative mentions in CHANGELOG.md and
MODELS.md (explaining why the 100-125B class has no Qwen3.6/Qwen3.8
checkpoint) are preserved as context, not active support.

## D-016 — Two real gx-auto classifier bugs fixed; D-005 re-verified, not
relitigated
**Date:** 2026-09-14
**Decision:** fixed a regex-scoping bug where the bare word "exhaustive"
alone reached the gx-max threshold (worse than the keyword-accumulation D-005
already forbids — one word, no accumulation needed), and a vision-override
bug where an oversized multimodal prompt could be routed to a tier whose
`max_context` couldn't hold it.
**Why not a redesign:** D-005's escalation rule itself (explicit "extreme"
marker or unfitting context, not keyword accumulation) was re-verified and
holds; these were implementation bugs in that rule's execution, not a reason
to change the rule.
**Evidence:** `legenex/orchestrator/tests/test_classifier.py` (30 new tests,
51 total in that file); `tests/CLASSIFIER_TEST_MATRIX.md` for the full
rule-coverage table.

## D-017 — gx-max's failed-acquire path now unwinds partially-started ranks
**Date:** 2026-09-14
**Decision:** `GxMaxLifecycle._do_acquire()` runs a best-effort
`gx-max-stop.sh --force` on every failure path (non-zero exit, health-probe
timeout, or the acquire's own subprocess timeout) before reporting DOWN, and
appends a note rather than masking the original error if that cleanup itself
cannot be confirmed.
**Why:** rank0/rank1 are `docker run -d`, detached from gx-max-start.sh's own
process — a script failing or being killed does not stop them. Traced (not
assumed) after this session's admission-control work landed: the new guard
prevents a *second* large load from ever starting, but does nothing about a
rank *already* started by the current attempt being orphaned by a *later*
failure (e.g. node 2 wedging mid-acquisition, after rank0 is already up).
Left unfixed, that is an unmanaged ~80-90 GiB leak with no lease on it — the
same shape as B-012, just triggered by a failed acquire instead of a manual
second launch.
**Evidence:** `legenex/orchestrator/tests/test_lifecycle.py::
test_failed_acquisition_after_partial_start_unwinds_via_stop_script` and
`::test_cleanup_failure_is_appended_not_swallowed`.

## D-018 — ChatGPT project-seed files integrated; root pointer docs added
instead of a second decision/task log
**Date:** 2026-09-15
**Decision:** The ChatGPT-Project bundle uploaded to `_project_seed/` was
merged into the canonical repo, then `_project_seed/` was removed. Root-level
`DECISIONS.md` and `TASKS.md` were added as required by the repo layout, but
`DECISIONS.md` is a short pointer/summary — this file (`coordination/
DECISIONS.md`) remains the single, canonical, numbered decision log. The
seed's own `D001`–`D010` numbering was **not** merged into this file's
`D-001`–`D-017` sequence (they use a different numbering scheme for a
different, smaller set of decisions, and every existing reference to `D-009`,
`D-013` etc. throughout `ARCHITECTURE.md`/`BLOCKERS.md` points here — renumbering
would have broken them for no benefit).
**Why:** avoids two competing "decision log" files silently drifting apart,
which is exactly the class of bug this project's own admission-control work
(D-012) was built to prevent in code.
**What was actually new from the seed and got merged:** node LAN/Tailscale IP
addresses (now in `OPERATIONS.md` and `CURRENT_STATE.md`), a corroborating
NCCL `all_gather_perf` benchmark (`TEST_RESULTS.md`), the finding that no
BMC/IPMI/Redfish/MCTP remote-power path exists on either node
(`coordination/BLOCKERS.md` B-016), and the GDM-auto-login/RDP stale-session
procedure (`OPERATIONS.md`, `RECOVERY.md`).
**What was deliberately NOT merged:** the seed's `CURRENT_STATE.md`,
`TEST_RESULTS.md`, `TASKS.md` and `HANDOFF.md` describe an earlier, less
verified snapshot (e.g. "gx-fast/gx-reason checkpoint still requires
verified selection", "node 2 later recovered" with no forensic detail) that
this repo's own same-day docs had already superseded with more current,
evidence-backed content — see `CURRENT_STATE.md`'s "Verified live this
session" table for what was actually re-checked against the real machines
during this integration, including finding and fixing doc drift the seed
had nothing to do with (node 2's actual recovery state, the gateway needing
a restart).
