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

## D-019 — gx-orchestrator boot-time bind race found and fixed; gx-auto was silently unreachable from the gateway all session
**Date:** 2026-09-15
**Decision:** Added `ExecStartPre=.../wait-for-docker0.sh` to
`gx-orchestrator.service` (now also checked into the repo at
`legenex/orchestrator/systemd/gx-orchestrator.service` — it previously
existed only as a live file in `~/.config/systemd/user/`, not in git).

**Why — found while investigating an apparent gx-auto classifier bug.**
`legenex/tests/acceptance.sh`'s gx-auto routing test failed with "expected
gx-reason, got gx-mini". Investigation showed **zero** `gx.routing` log
entries had been written all day: every `gx-auto` request from the LiteLLM
container was failing with `Connection error` before ever reaching the
orchestrator's classifier. The test's `tail -1` on the routing log was
picking up a stale entry from the previous day, making all three of its
comparisons coincidental rather than real — the reported "classifier bug"
was a complete false positive.

**Root cause:** `gx-orchestrator.service`'s startup log showed every prior
start (2026-09-14, through 23:54:57) successfully bound both
`127.0.0.1:18900` and `172.17.0.1:18900` (the docker bridge address
`host.docker.internal` resolves to from any container, confirmed — this is
the correct, D-004-documented design, not itself wrong). **This morning's
boot (09:33:30) failed the second bind** with `Cannot assign requested
address` (`EADDRNOTAVAIL`) and — because `build_servers()` treats each bind
as best-effort and only logs a warning, not fatal — the process kept
running anyway, silently degraded to loopback-only for the rest of its
life (over 2.5 hours, spanning this entire session, until found). Cause:
`After=docker.service` guarantees dockerd has *started*, not that `docker0`
already has its IPv4 address assigned — a real, if narrow, boot-order race.

**Fix:** wait for `docker0` to actually have an address (poll up to 30s)
before `ExecStart`, so the orchestrator either starts fully bound or fails
its `ExecStartPre` (which `Restart=on-failure`/`RestartSec=5` retries,
rather than starting degraded and silently). Verified: `systemctl --user
restart gx-orchestrator.service` now binds both addresses every time;
`ExecStartPre` shows `code=exited, status=0/SUCCESS`.

**Verified the actual fix, not just the restart:** with both addresses
bound, re-ran the exact prompt from the failing test directly —
`"Debug this stack trace and derive the time complexity, then refactor the
algorithm."` — and the classifier correctly produced
`{"tier": "gx-reason", "complexity": 9, "reasoning_score": 10, ...}`. The
classifier itself was never broken; D-005/D-016's escalation logic is
intact.

**Impact while broken, corrected 2026-09-15 (a reviewer caught the first
wording as ambiguous):** every `gx-auto` request for this entire session
(and probably since this morning's boot, before this session started)
failed to reach the orchestrator's classifier at all -- but this was NOT a
silent substitution. `legenex/gateway/litellm/config.yaml`'s
`litellm_settings`/`router_settings` set `num_retries: 0`, `fallbacks: []`,
`context_window_fallbacks: []`, `content_policy_fallbacks: []` globally,
with its own comment: *"a failure surfaces as an error to the caller."*
So every `gx-auto` request during the outage returned a loud connection
error to the client, never a silent answer from some other model -- the
safe failure mode, confirmed by re-reading the config this session already
relied on, not assumed. Still a real, live production gap (gx-auto simply
did not work for 2.5+ hours), just not the scarier "silently wrong tier"
shape. `gx-max`'s `/lifecycle/gx-max/acquire` (also served by this same
process) was unaffected by *this* bug specifically, since
`legenex/tests/gx-max-validate.sh` calls the orchestrator from node 1's own
shell (`127.0.0.1:18900`), not from inside a container — B-017 is a
separate, still-open issue.

## D-020 — B-017 resolved: gx-max gets its own smaller admission-guard reserve, not the generic 30 GiB floor

**Decision, 2026-09-15:** applied option 1 of the three B-017 named (see
`coordination/BLOCKERS.md` B-017 for the full three-way choice). gx-max's
own admission check now uses `GXMAX_GUARD_RESERVE_GIB` (default 5 GiB)
instead of the generic `GX_GUARD_RESERVE_GIB` (30 GiB, unchanged for every
other workload class). Implemented as a local override in
`legenex/lifecycle/gx-max-start.sh` (`GX_GUARD_RESERVE_GIB="${GXMAX_GUARD_RESERVE_GIB}"`,
set right after sourcing `resource-guard.sh`, scoped to that script's own
two rank admission checks only) plus the new macro and its rationale in
`legenex/lifecycle/gx-max.conf`.

**Why this was mine to decide, not a re-opening of the human-decision
requirement:** the constraint that motivated leaving it open was "maintain
the 30 GiB floor for normal operation" — and gx-max's active state is
explicitly documented (`ARCHITECTURE.md` §8) as NOT normal operation: it
evicts every other workload on both nodes by design (L-2/L-6). The
`gx-max.conf` comment that predates this decision already named exactly
this option as the most direct reading of the locked design's own intent.

**Verified live, same session:** `legenex/tests/gx-max-validate.sh` was run
through the real orchestrator HTTP API immediately after this change.
Previously (first attempt, pre-fix) this failed at step 1 — admission
refused before either rank started. This time, rank1 (node2) and rank0
(node1) BOTH passed admission and rank1 actually started for the first
time ever through the real production path. This is genuine, partial
progress: the specific bug B-017 named (permanent, structural refusal) is
fixed. **It surfaced a second, distinct problem — rank0 was then OOM-killed
during weight loading — tracked separately as B-020, not swept into this
decision.** Do not read this entry as "gx-max fully validated"; see B-020
and `TASKS.md` for what is still open.

**Follow-up applied same session:** `GXMAX_RANK_ESTIMATED_GIB` raised
90 -> 95 GiB (in `gx-max-start.sh` and the matching `WORKLOAD_SIZING` entry
in `resource_guard.py`) after the B-020 OOM, to make the admission
arithmetic reflect the documented ~93-95 GiB measured working set rather
than the older, more optimistic 90 GiB ceiling. This does not eliminate the
underlying tension (gx-max is locked to run at the very edge of a 121 GiB
node); it makes the guard's own numbers more honest about it.

## D-021 — gx-reason replaced: Qwen3.5-122B-A10B/llama.cpp → nvidia/Qwen3.6-27B-NVFP4/vLLM

**Decision, 2026-09-15.** `coordination/BLOCKERS.md` B-011 concluded the
existing GGUF checkpoint on the existing `legenex/llama-cpp-spark` build is
not the problem — the bug is in that build's CUDA kernel path for the
`qwen3_5_moe` hybrid (linear-attention/GDN) architecture on this hardware,
confirmed by both a GPU-vs-CPU comparison (GPU garbage, CPU coherent, same
weights) and a rebuild from current upstream `llama.cpp` master (identical
garbage). Retrying the same engine/architecture combination was explicitly
rejected as a next step.

**Chosen replacement:** `nvidia/Qwen3.6-27B-NVFP4` (Apache-2.0, ungated,
verified via the live HuggingFace API before this decision — real repo,
real file sizes, not guessed) served by vLLM using the exact same image
already proven working on this hardware for gx-fast
(`jstarkg/vllm-gb10-flashnext:0.28-sm121-r6`).

**Why this specific model, not a different family:** `config.json` for the
new checkpoint shows `model_type: qwen3_5`, the identical hybrid-attention
architecture family (alternating `linear_attention`/`full_attention`
layers) as the broken llama.cpp checkpoint. That is deliberate, not an
oversight: gx-fast (`nvidia/Qwen3.6-35B-A3B-NVFP4`) already runs this exact
architecture family correctly on vLLM on this exact hardware, verified live
this session (real completion, correct multi-step-reasoning answer, see
`TEST_RESULTS.md`). Reusing a confirmed-good engine/architecture pairing is
lower-risk than introducing an unverified one, and it directly explains
*why* the old combination failed (llama.cpp's CUDA kernels for this
architecture, not the architecture or the checkpoint itself) rather than
just picking something different and hoping.

**Why dense 27B, not another MoE:** gx-fast is already a `qwen3_5`-family
MoE (35B total / ~3B active). A dense 27B model activates its full
parameter count on every token — a real, meaningfully larger amount of
compute per token than gx-fast's ~3B active, which is the actual lever for
harder step-by-step reasoning/coding quality, not just a bigger number in
the model name. It is explicitly not Qwen3.8 (forbidden), and it is a
distinct checkpoint from gx-fast, so it cannot become a silent duplicate of
that tier.

**Sizing:** ~21.9 GiB of safetensors (measured via HTTP HEAD content-length
on all three shards before download — not a guess), comfortably under
B-009's ~55 GiB vLLM-on-this-hardware ceiling, unlike the old checkpoint's
class (GGUF/llama.cpp was chosen originally specifically because vLLM
COULD NOT load a 95 GiB checkpoint here at all — D-009). `resource_guard.py`'s
`WORKLOAD_SIZING["gx-reason"]` lowered 95.0 -> 45.0 GiB (generous ceiling
above the expected working set); `node02.yaml`'s heavy-tier budget comment
and `gx_reason_mem_limit` cgroup cap lowered to match (45g).

**Status: VALIDATED LIVE 2026-09-16. B-011 is closed.** The checkpoint was
downloaded to `/srv/models/vllm/Qwen3.6-27B-NVFP4` on node 2 (21,921,697,184
bytes across 3 shards, byte-for-byte the sizes the HF API advertises), the
config was deployed and validated against the llama-swap binary
(`-validate`: "config is valid: 1 model(s)"), and the full A-E sequence
passed — including the original B-011 repro prompt, which now answers
" Paris." where it previously emitted `////////////////////`. See
`coordination/BLOCKERS.md` B-011 and `TEST_RESULTS.md` for the evidence.

**Two corrections to this decision's own assumptions, from the live run:**

1. **Sizing was right, for a partly wrong reason.** The predicted ~22 GiB of
   weights is really 20.42 GiB, and the 45 GiB admission estimate held up:
   the measured node-level footprint is ~44 GiB (MemAvailable 114 -> 70 GiB).
   But the `--memory 45g` cgroup cap this decision leaned on does **not**
   enforce that — the container's own `memory.current` was 10.92 GiB while
   the node lost 44 GiB, because the CUDA pool is not charged to the
   container cgroup on this hardware. `--gpu-memory-utilization 0.35` is the
   real bound. See B-021.
2. **The checkpoint is multimodal, which this decision did not note.** Both
   it and gx-fast's are `*ForConditionalGeneration` with a `vision_config`
   and image/video processors. This changes nothing about the text tier and
   is consistent with L-10 (vision is a model capability, not a separate
   alias), but it means gx-reason can accept images; `supports_vision: true`
   has been set on its gateway entry accordingly — and then **verified live
   rather than assumed**: a generated image of three blue circles, sent
   through the gateway, was described correctly as "3 blue"
   (`TEST_RESULTS.md` §14.4).

## D-022 — SUPERSEDED 2026-09-16 by D-025 — gx-max memory: the 30 GiB reserve is not reachable by tuning; the engine was retuned anyway and the real floor measured

**Date:** 2026-09-16
**Status:** ACCEPTED for the tuning; the reserve value itself is ESCALATED to
the human (see `coordination/BLOCKERS.md` B-022).

**Context.** The operator explicitly delegated gx-max *runtime* tuning
(`--mem-fraction-static`, KV/context/CUDA-graph sizing, startup sequencing,
memory estimates, lifecycle timeouts, failure-unwind logic) while keeping the
architecture locked (SGLang, `nvidia/DeepSeek-V4-Flash-0731-NVFP4`, TP=2,
rank0 on gx10-01, rank1 on gx10-02, no silent fallback), and required
`MemAvailable >= 30 GiB` on both nodes with no use of swap as capacity.

**What was measured (four real two-node runs, 2026-09-16).**

| Fact | Evidence |
|---|---|
| Node total as the engine sees it | `124546 MiB = 121.63 GiB` (rank0 log, `torch.cuda.mem_get_info`) |
| Checkpoint on disk | 163.48 GiB over 48 shards; 155.77 GiB of that is MoE expert weights |
| Per-rank weight residency | ~82–94 GiB — i.e. roughly two thirds of a node, before any KV cache |
| Load-phase trough, node1 | 8–12 GiB MemAvailable |
| Load-phase trough, node2 | **1 GiB** MemAvailable |
| Effect of `--mem-fraction-static` on the trough | **None.** Measured identical troughs at 0.50 and at 0.70 |
| Where the memory goes | ~94 GiB held by the NVIDIA driver, invisible to every `/proc/meminfo` LRU counter (B-021). At the trough: `MemFree 725 MiB`, `Cached 15.5 GiB`, `AnonPages 13.1 GiB` — ~31 GiB visible out of 121.63 GiB |

**Decision 1 — the tuning is applied.** `--mem-fraction-static` 0.80 -> 0.70,
`--context-length` 327680 -> 32768, `--chunked-prefill-size` 8192 -> 4096
(with `SGLANG_B12X_MAX_TOKENS` kept in step), `--cuda-graph-max-bs-decode`
32 -> 8, `--max-running-requests` 32 -> 8. These are real improvements to the
*steady state*: the 2026-09-14 run at 0.80 ended with `available_gpu_mem=14.93
GB`, and 0.70 releases a further 12.2 GiB of static pool. They do **not**
change the load transient, because the transient is the model weights.

**Decision 2 — the memory guards are phase-aware.** A single floor is wrong,
because the two phases have genuinely different physics:

* **Load phase** (engine not yet answering `/health`): only a last-resort
  `GXMAX_LOAD_FLOOR_GIB` (2 GiB) tripwire, so *this tooling* tears the cluster
  down deliberately rather than leaving it to the kernel OOM killer. The real
  protection here is rank-liveness (`rank1-deadman.sh`) plus
  `--oom-score-adj 950`, which is verified to make SGLang the kernel's chosen
  victim rather than sshd/tailscaled.
* **Steady state** (engine healthy): `GXMAX_ABORT_FLOOR_GIB` — the real
  reserve.

Enforcing the steady floor during load is not conservative, it is simply
broken: it aborted two otherwise-healthy launches before the trough was
measured.

**Decision 3 — the 30 GiB number is escalated, not silently redefined.**
`GXMAX_GUARD_RESERVE_GIB` stays at 30 in the committed config. The consequence
is honest and visible: the admission guard refuses gx-max, because 92 GiB
(estimated rank footprint) + 30 GiB (reserve) = 122 GiB on a 121.63 GiB node.
Lowering the floor to make the arithmetic work would be exactly the silent
downgrade this project forbids, and the floor is a human-set policy. B-022
carries the numbers and the options.

**What was explicitly NOT done.** No engine change, no model change, no
topology change, no swap increase, no use of swap as capacity, no silent
fallback, and no quiet edit of the reserve to whatever value happened to fit.

## D-023 — gx-auto may use gx-max, but never acquire it

**Date:** 2026-09-16. **Status:** ACCEPTED (implements an explicit human
requirement).

**Decision.** `gx-auto` routes to `gx-max` only when gx-max is already
`READY`. If it is not, gx-auto routes to the best available tier and logs
`downgraded_from: gx-max`. It never triggers an acquisition.

**Why.** Acquiring gx-max is not an ordinary operation that might fail — it is
a cluster-wide takeover. `gx-max-start.sh` drains gx-mini, gx-fast, gx-reason,
ComfyUI, the media router and both llama-swaps *before* it does anything else,
because gx-max needs both whole nodes. Letting a routed request trigger that is
wrong even in the success case: a user asking a hard question has not asked to
evict every other tier.

Observed live on 2026-09-16, which is what made this concrete: a single
`gx-auto` prompt containing the word "exhaustive" tore down node 1's resident
models and both llama-swaps, was refused by the admission guard, and spent
~12 s restoring everything — all invisible to the caller, who just saw a slow
answer from gx-reason.

**What did not change.** A DIRECT `gx-max` request still acquires, and still
fails loudly with 503 rather than being served by a smaller model
(ARCHITECTURE.md §5's never-downgrade rule). The explicit
`/lifecycle/gx-max/acquire` endpoint still acquires. Both are
operator-initiated, which is the distinction that matters.

Covered by three regression tests in `tests/test_server.py`
(`TestGxAutoNeverAcquiresGxMax`), including one asserting the direct path
*still* acquires, so this is not "fixed" later by removing acquisition
everywhere.

## D-024 — gx-max's health reflects admission, not just process state

**Date:** 2026-09-16. **Status:** ACCEPTED.

**Decision.** `GET /health/detailed` runs the same read-only
`compute_admission` arithmetic the launch paths use, and reports gx-max as
`unavailable`/`usable: false` with the refusal reason when the guard would
refuse it.

**Why.** Every other tier's health is a question of whether its process is
running. gx-max's is not: it is a question of whether it is *allowed to start*.
Reporting `stopped, usable: true` for a tier the guard refuses on every single
attempt is a fake healthy state, which this project forbids — and it is exactly
what `gx status` showed before this.

**Guard-rails that are part of the decision**, not incidental:

* The probe is **read-only** and takes no lock. Health must not be able to
  perturb the thing it reports on.
* A probe failure returns `""` and falls back to the old behaviour. A broken
  probe must never invent a fault on a healthy tier.
* `READY` is never second-guessed. A running engine is healthy regardless of
  what admission would say about starting a *new* one.
* node-2-offline still outranks admission in the reported reason: it is the
  more actionable answer, and the arithmetic is moot if the node is gone.

## D-025 — gx-max restored: known-good launch, no cgroup cap, three-phase memory policy

**Date:** 2026-09-16. **Status:** ACCEPTED, verified live. **Supersedes** the
D-022 retuning and B-022's conclusion that "gx-max cannot fit".

**What was wrong.** B-022 concluded that the locked model could not fit.
That conclusion rested on runs that differed from the verified 2026-09-14
launch (commit `4b96e49`) in two ways that matter:

1. **`--memory 106g --memory-swap 106g`.** When the two values are equal,
   Docker sets the container's `memory.swap.max` to 0. The loader's ~26 GiB
   of anonymous staging pages then cannot be swapped out. Every failed run
   shows the same pattern: 0 MiB MemAvailable with 63 GiB of swap untouched.
   The verified run had no cap; it filled swap and then recovered.
2. **`--mem-fraction-static 0.70` (and 0.50).** A TP=2 shard of this
   checkpoint is about 73% of a GB10 node; SGLang's own figure is "minimum
   viable = 0.731". Both values are below what can hold the weights plus any
   KV cache.

The admission formula then made gx-max unlaunchable by arithmetic:
117 GiB (load peak) + 30 GiB (reserve) is more than a 121.63 GiB node.

**Decision.**

* **Launch vector.** Byte-for-byte the `4b96e49` vector. It also matches the
  current official SGLang cookbook cell
  `dgx-spark/flash-official/nvfp4/balanced/multi-2` (fetched 2026-09-16;
  unchanged since 2026-09-03), plus the three loader flags that DGX Spark
  needs for the load spike, `--enable-metrics` (D-002) and
  `--oom-score-adj 950`.
* **No `--memory` and no `--memory-swap`** on the rank containers.
* **gx-max admission is a cluster-takeover policy.** The ordinary 30 GiB
  reserve is unchanged for every single-node tier. The takeover policy
  separates three phases:

  | Phase | What applies |
  |---|---|
  | Pre-launch clean state (admission) | both nodes drained; no other large or exclusive resident; `/swapfile-sglang` active; at least 40 GiB swap free; at least 100 GiB MemAvailable; PSI full at most 5; management plane healthy. Re-checked under each node's lock. |
  | Startup transient (~117 GiB) | Documented and monitored, never admitted against. |
  | Steady-state residency | The ledger records 105 GiB per rank. |

* **Phase-aware live safety** (`gx-max-safety.sh`) runs node-locally on both
  nodes. It replaces the instantaneous 2 GiB tripwire, which would have
  killed the verified launch.
  * Immediate abort: a kernel OOM kill, or a hard `NV_ERR_NO_MEMORY`.
  * Abort only when sustained: memory and swap both exhausted, swap
    thrashing, or fork/exec starvation.
  * Steady phase only: a sustained MemAvailable floor.
  * The driver's `nvCheckOkFailedNoLog … NV_ERR_NO_MEMORY` lines are counted
    but not fatal. Both nodes emit them at "Load weight begin" of a healthy
    load. Treating them as fatal caused one false abort on the first
    attempt, which also served as a real-workload unwind test.
* **Steady-state watchdogs on both nodes.**
  * `rank1-deadman.sh` now probes rank0's `/health` over the fabric. It used
    to probe node 2's loopback, which never answers, so it never left the
    load phase.
  * New `rank0-watch.sh` unwinds both nodes if rank1 or rank0 disappears,
    if node 2 is unreachable for 180 s, or if `/health` fails for 180 s.

**Measured (2026-09-16, attempt 2).**

| | node 1 | node 2 |
|---|---|---|
| Time to ready | 539 s | — |
| Minimum MemAvailable during load | 2,653 MiB | 7,434 MiB |
| Peak swap used | 65,535 MiB (all of it, for about 5 s) | 51,584 MiB |
| Steady MemAvailable | 14.9 GiB | 16.5 GiB |
| Steady swap used | 8.1 GiB, flat | 5.4 GiB, flat |

Other measurements from the same run:

* **Engine:** `available_gpu_mem=14.93 GB`, identical to 2026-09-14.
* **Load pressure:** peak PSI full 22% on node 1; fork+exec at most 6 ms.
* **Inference:** 8/8 checks pass, directly and through the gateway alias.
* **Fabric:** about 1.1 GB of RDMA traffic across both active rails per
  300-token generation, against 0.08 MB on Tailscale.

**Residual risk, stated plainly.** Node 1 touched 100% swap for one 5 s
sample during load. The same thing happened on 2026-09-14 ("63/63 GB"). Node
1 also carries the gateway, Postgres, Open WebUI, AgentOS and a desktop
session, so its load-time headroom is the smallest in the cluster. The
exhaustion rule (MemAvailable under 512 MiB **and** swap free under 2 GiB,
for 30 s) is what stands between that and a kernel OOM kill. Reducing node
1's background residency before a gx-max launch is the lever. It needs no
change to any locked value.

## D-026 — Source control: gx10-01 is the only Git writer; GitHub `legenex/gx-server` is canonical; gx10-02 is a pull-only mirror

**Date:** 2026-09-16. **Status:** ACCEPTED (implements an explicit human
requirement).

**Decision.**

| Node | Role | Mechanism |
|---|---|---|
| gx10-01 | the **only** writer | `ops/git-sync/node1-autosync.sh` (watcher every 15 s, commit after a 45 s quiet period, 1-minute timer fallback), versioned hooks in `.githooks/` (`core.hooksPath`) |
| GitHub `legenex/gx-server` `main` | canonical remote, off-machine backup | pushed by gx10-01 over HTTPS with the existing `gh` credential helper |
| gx10-02 | pull-only production mirror | `ops/git-sync/node2-reconcile.sh`, triggered over SSH after every push and by a 1-minute timer; push URL set to `DISABLED-gx10-02-is-pull-only` |

A daily `integrity-audit.sh` on both nodes checks that all three HEADs match,
that critical files are byte-identical, and that no secrets, weights or large
binaries are tracked.

**Why a single writer.** Two machines auto-committing to one branch will
eventually conflict, and a robot resolving merge conflicts in lifecycle
scripts is worse than no sync at all. With one writer, gx10-02 can never
diverge: anything unexpected there is configuration drift. The reconciler
saves the evidence to `/srv/logs/gx-git-sync/drift/<ts>/` (mode 0700, with
secret-like values masked) and resets to `origin/main`. It uses
`git clean -fd`, not `-fdx`, so ignored machine-local files survive.

**The repository is PUBLIC**, so every automatic commit is gated:

* forbidden paths (secrets, keys, weights, archives, runtime state) and files
  over 5 MiB are unstaged and logged by name only;
* a gitleaks staged scan (fallback: a regex scan) must be clean. Otherwise
  nothing is committed, and a broken scanner also blocks the commit;
* staged conflict markers block the commit.

**Runtime state moved out of the checkout.** Guard locks and ledgers now live
in `/srv/projects/gx-cluster/state/guard`, previously
`legenex/lifecycle/.state`. `/srv` itself is root-owned and there is no sudo,
so `/srv/projects/gx-cluster/{state,runtime,secrets,backups}` is the
user-owned substitute for the requested `/srv/gx-cluster/*` and
`/srv/backups/*` paths.

**History was not rewritten.** The pre-push audit (gitleaks 8.30.1, 128
commits, all refs) found no credential of ours. The one live-looking key is
an upstream author's, and it is already public in that upstream repository.
The pre-migration bundle is at
`/srv/projects/gx-cluster/backups/gx-server/pre-github-migration.bundle`.

## D-027 — Kernel-lock verifier: check the installed state, and classify apt proposals

**Date:** 2026-09-16. **Status:** ACCEPTED.

`verify-kernel-lock.sh` (now versioned at
`legenex/host/kernel-lock/verify-kernel-lock.sh`) reported the correctly held
6.17 kernel as `MISSING` on both nodes. It also failed the lock on any
simulated kernel install. Both were verifier bugs, not lock failures:

1. It required dpkg's abbreviated status to start with `ii`. A held,
   installed package reports `hi`. The verifier now checks
   `db:Status-Status == installed` and reports the hold selection separately.
   It also checks the modules package and the `/boot` image and initrd.
2. `apt-get -s dist-upgrade` proposes installing eight **older-ABI**
   `6.8.0-1062-nvidia*` flavours. These are new packages that touch no held
   package, no part of the locked 6.17 set, and not the name-based
   `GRUB_DEFAULT` pin. Proposals are now classified:
   * **THREAT (FAIL):** any removal, or any change to a held, locked or
     HWE-meta package.
   * **NEWER (WARN):** a newer kernel ABI.
   * **OTHER (INFO):** anything else.

Result: both nodes report 13 passed, 0 warnings, 0 failed. Nothing was
reinstalled, upgraded or removed, and GRUB was not touched (L-4).

## D-028 — Management web UI: a thin, authenticated interface on gx10-01 (`legenex/control-ui`)

**Date:** 2026-09-16. **Status:** ACCEPTED.

**Decision.** The cluster gets one management web UI, `gx-control-ui`, on
gx10-01 port **8088**, bound to **127.0.0.1 and the Tailscale address only**.
It is an interface to the existing control plane, not a second one:

* **Reads:** `/proc`, `/sys`, `docker ps`, `systemctl --user`, Git, the
  orchestrator, LiteLLM, both llama-swaps, the media router and SGLang health.
  gx10-02 is read with one SSH call per refresh that streams the same
  collector (`hostfacts.py`) to `python3 -`.
* **Writes:** only the fixed operations in `gx_control_ui/actions.py`.
  gx-max load/unload/restart/force-release call the orchestrator's
  acquire/release API. llama-swap tiers use llama-swap's own on-demand load
  and per-model unload, after the same 30 GiB admission formula
  (`resource_guard.compute_admission`). Media "unload" is ComfyUI's own
  `/free` on node 2's loopback. There is no shell, no generic command, no
  file browser, and no upgrade, kernel or firmware operation.
* **Safety interlocks:** model and infrastructure operations refuse while
  gx-max is acquiring, ready or releasing, or while a rank container exists;
  one state-changing operation runs at a time; gx-max load needs the typed
  phrase `gx-max`, force release needs `FORCE RELEASE`.

**Why this shape.**

* **Stdlib Python backend** (like the orchestrator, D-003): it sits beside
  the recovery path and must not depend on pip or a registry.
* **Dependency-free ES-module frontend:** no bundler and no runtime npm
  packages, so the strict CSP (`script-src 'self'`, no inline code) holds
  and there is no frontend supply chain. Playwright and axe-core are dev-only.
* **A user systemd unit, not a container:** the backend needs the host's
  `/proc`, the user's SSH key for gx10-02, `systemctl --user` and the Docker
  CLI. A container would need the Docker socket and SSH keys mounted, which
  is a larger privilege surface than the unit (`NoNewPrivileges`,
  `ProtectSystem=strict`, `MemoryMax=512M`).

**Authentication.** One local admin account. The password is stored as a
scrypt hash (`hashlib.scrypt`, N=2^15, r=8, p=1, random salt) in
`/srv/projects/gx-cluster/secrets/control-ui/auth.json` (0600 in a 0700
directory), set only by `legenex/control-ui/scripts/gx-ui-passwd`. Sessions
are server-side: opaque tokens in `HttpOnly; SameSite=Strict` cookies, 1 h
idle and 12 h absolute. Every POST needs the session's CSRF token and a
same-origin request. Five failed logins lock an address out for 15 minutes.
Upstream keys come from the existing ignored `legenex/gateway/.env` via the
unit's `EnvironmentFile`; the browser never receives one.

**Transport.** Plain HTTP inside Tailscale (WireGuard-encrypted). Tailscale
HTTPS certificates are not enabled on this tailnet (`CertDomains` is empty),
so the cookie `Secure` flag is set only when a request arrives with
`X-Forwarded-Proto: https`. Enabling tailnet HTTPS and fronting the UI with
`tailscale serve` is a possible later hardening step. It needs a tailnet
admin setting, not code.

## D-029 — Orchestrator: read-only gx-max lifecycle events and job history

**Date:** 2026-09-16. **Status:** ACCEPTED.

The Jobs page needs real phase data. Instead of a second scheduler, the
orchestrator now publishes what it already runs:

* `gx-max-start.sh` and `gx-max-stop.sh` are run through a streaming
  `Popen` with the same timeout semantics as the `subprocess.run` it
  replaces. Each output line goes to a 400-line buffer and to
  `/srv/logs/gx-max-lifecycle.log`.
* A `phase` is derived from the section markers the scripts already print
  (preflight, draining, admission, loading_rank1, loading_rank0, warming,
  ready, serving; and draining_requests, stopping_ranks, memory_recovery,
  restoring, released for a release). Phase never drives a transition:
  `State` (down / acquiring / ready / releasing) is unchanged.
* A job history (last 25 acquire/release jobs with phases, outcome, error
  and the measured startup seconds) is kept in
  `/srv/projects/gx-cluster/state/orchestrator/gx-max-history.json`.
* New endpoint `GET /lifecycle/gx-max/events?after=&limit=`, read-only.
  `/lifecycle/gx-max/status` gains `phase`, `phase_seconds`,
  `last_startup_seconds` and `idle_ttl`.

The launch vector, the scripts, admission and the unwind paths are
unchanged. The job record is finalised before the READY/DOWN transition
wakes waiters, so a status read never sees a half-written job. Covered by
`legenex/orchestrator/tests/test_lifecycle_events.py`, and all 138 existing
orchestrator tests still pass.

## D-030 — gx-auto understands Kilo Code; gx-mini and gx-fast become uncensored

**Date:** 2026-09-17. **Status:** ACCEPTED.

**Routing.** Kilo Code sends every request with a large system prompt, about
20 tool schemas and an `<environment_details>` block. The old classifier
counted all of that and sent even "is the server up?" to gx-reason. The
classifier (`legenex/orchestrator/gx_orchestrator/classifier.py`) now:

* extracts the real task from `<task>`, `<user_message>`, `<feedback>` and
  `<answer>`, and ignores envelopes such as `<environment_details>`;
* recognises continuations (a `tool` role, a `[x] Result:` turn, or
  assistant `tool_calls`) and routes them by the original task;
* sorts intents into conversational, simple, action and continuation. Tool
  schemas count toward the context size only. The tool-capable floor
  applies only to action and continuation requests;
* clamps output planning to 16 384 tokens and per-tier `max_output`;
* never starts gx-max. If only gx-max can hold the context and it is not
  READY, it returns 503 `gx_max_not_running` rather than silently
  downgrading;
* writes each decision and its completion to
  `/srv/logs/gx-auto-routing.jsonl` with a request id and a message
  fingerprint (`GET /routing/decisions`), so tests match their own
  decision.

**Models.** gx-mini is now
`HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive` @c09cdbcd (Q4_K_M plus
mmproj, 131k context, parallel 2). gx-fast is now
`kyaky/Qwen3.6-35B-A3B-Uncensored-NVFP4` @33d5cf83 (vLLM 0.28, 131k,
`gpu_memory_utilization 0.34`, `HF_HUB_OFFLINE=1`). Both are preloaded by
llama-swap and verified with pinned sha256 manifests. The
previous checkpoints stay on disk as the rollback until a human deletes them
(B-026).

## D-031 — Media v2: edit, variation, image-to-video and video edit

**Date:** 2026-09-17. **Status:** ACCEPTED.

gx-media-router 2.0.0 adds OpenAI-shaped `/v1/images/edits`,
`/v1/images/variations`, `/v1/videos` (JSON, or multipart with
`input_reference` → i2v), `/v1/videos/{id}/remix` and `/v1/videos/edits`.
Uploads are MIME-sniffed, size-capped and written to
`/srv/comfy-input/gx-in/<uuid>` (purged after use). The router hands out
LiteLLM-encoded video ids, because LiteLLM drops `model_id` on edit
requests and status polling would otherwise fail.

The video edit is **keyframe propagation**. Qwen-Image-Edit-2511 edits the first frame,
then Wan 2.2 I2V re-renders the clip from the source latent. The
`strength` value maps to the start step: ≥0.75 keyframe start 0; 0.5–0.75
keyframe start 1; 0.25–0.5 light start 2; otherwise light start 3. Partial
denoise alone could not change global attributes such as day to night; that
was measured, not assumed.

Uncensored adapters (pinned): `perpetual3x/Tumblr-NudeShot-NSFW-LoRA-v1` at 0.6
for generation, and `rzgar/Wan2.2_LightX2V_4Step_Uncensored` for video. ComfyUI
runs with `--reserve-vram 40`, and the router frees it after 600 s idle or on a
model-set change, so node 2 keeps headroom for gx-reason.

**Amendment (router 2.1.0, same day).** `--reserve-vram` does not bound
ComfyUI's host-side cache. A video edit left node 2 with 14 GiB
MemAvailable, and a cold gx-reason start then failed. gx-reason's start
command now first asks the router to free ComfyUI (`POST /v1/admin/free`,
refused while a generation runs), so node 2 is handed over explicitly
instead of waiting for the idle timer.

**Amendment (router 2.2.0).** Measured cold footprints on node 2: image
about 57 GiB, t2v/i2v about 72 GiB, keyframe video edit about 107 GiB. gx-reason
leaves about 67–70 GiB, so the two tiers cannot run video together. The
router therefore does memory admission from `/proc/meminfo` (image 60,
video 76, keyframe 110, warm 8 GiB). It waits for a just-freed ComfyUI and
otherwise fails the job with a message telling the user to unload gx-reason.
It never evicts gx-reason itself, because that would kill a user's reasoning
request.

## D-032 — gx-max serves the CRACK abliterated DeepSeek-V4-Flash (amends L-6)

**Date:** 2026-09-17. **Status:** ACCEPTED (the migration request asked for
an uncensored gx-max).

gx-max stays SGLang, TP=2, two nodes. Only the checkpoint changes, to
`dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4` @c66fe384 (155.44 GiB,
sha256-verified on both nodes and copied over the fabric). That checkpoint
matches the cookbook cell `dgx-spark/flash-official/fp4` (`--moe-runner-backend
b12x`), selected by `GXMAX_QUANT_CELL=fp4` in `legenex/lifecycle/gx-max.conf`.
Rollback is `GXMAX_MODEL_DIR=/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4
GXMAX_QUANT_CELL=nvfp4`. Acceptance: a full UI load → inference → release
cycle, 8/8 direct and 8/8 gateway checks, 46.95 tok/s through the gateway, 0
refusals (TEST_RESULTS §18). CLAUDE.md L-6 still names the NVIDIA checkpoint
as the architecture lock. The engine, topology and "no silent downgrade" rule
are unchanged.

## D-033 — gx-reason stays on the interim 27B until the gated iSkye model can be downloaded

**Date:** 2026-09-17. **Status:** ACCEPTED as interim.

The required model is `iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` (gated=auto,
105 935 758 025 bytes). The HF API returns 401 without a token, and neither node has
one. No substitute was chosen: the registry records the target and marks
`nvidia/Qwen3.6-27B-NVFP4` as `interim`, and the UI shows that. Once a token is
saved (Model Manager → Hugging Face token), the Model Manager stages,
verifies, tests and assigns the target on gx10-02, with automatic rollback. See B-025.

## D-034 — Control UI: Create, Media Library and Model Manager

**Date:** 2026-09-17. **Status:** ACCEPTED.

* **Media Library:** SQLite (`PRAGMA user_version` migrations) plus files
  under `/srv/projects/gx-cluster/media`. Items are never overwritten; edits
  are children with lineage. The library supports search, filters, favourites,
  rename, range streaming, single-use ZIP links and confirmed deletes. ffprobe
  and thumbnail extraction run in a throwaway container with no network.
* **Create:** a job worker runs generate, edit, variation, t2v, i2v and v2v
  through the media router and saves the results into the library.
* **Model Manager:** a registry-driven inventory for both nodes (node 2 via a
  fixed SSH script). It supports HF search and URL lookup, adapter
  classification and a staging download with pinned revision and sha256
  manifest. Test-serve runs in a temporary container on 127.0.0.1:19098 behind
  the admission guard. Assign edits only the binding macro, restarts
  llama-swap, runs a real gateway completion and rolls back automatically on
  failure. It also supports accept, and delete only when nothing references
  the files.
* **Supply chain:** repository files are data. No model-card command is run,
  `trust_remote_code` is never enabled, and small files are capped at 8 MB.
  The HF token lives in `secrets/hf/token` (0600) and is passed by
  environment or stdin, never on a command line.

## D-035 — API keys in the UI and a local acceptance account

**Date:** 2026-09-17. **Status:** ACCEPTED.

Settings → API Keys manages LiteLLM virtual keys (create, list, test, replace,
revoke) using the master key on the server side only. The browser sees the new
secret once and after that only a masked form. Automated tests showed the
master key is absent from 8 API responses. A second account, `acceptance`,
exists for live automated tests. Its password is in
`secrets/control-ui/acceptance-password` (0600). It can log in only from
127.0.0.1, and `gx-ui-passwd --remove-acceptance` deletes it.

## D-036 — gx-music is the eighth permanent alias (amends L-10)

**Date:** 2026-09-17. **Status:** ACCEPTED. The user explicitly approved
growing the alias list from seven to eight.

The public aliases are now `gx-mini`, `gx-fast`, `gx-reason`, `gx-max`,
`gx-auto`, `gx-image`, `gx-video` and `gx-music`. There is still no
`gx-vision`, and no alias was repurposed.

**Model and runtime.** gx-music is ACE-Step 1.5 XL turbo on gx10-02:

| Component | Revision |
|---|---|
| `ACE-Step/acestep-v15-xl-turbo` | `d4a0b288` |
| `ACE-Step/acestep-5Hz-lm-4B` | `0a3ec94b` |
| `ACE-Step/Ace-Step1.5` (VAE / text encoder) | `19671f40` |
| Runtime `ace-step/ACE-Step-1.5` | `ca1e85fe` |

It runs in image `gx-music-engine:acestep15-ca1e85f-t214`, which uses torch
2.14/cu130; upstream's torch 2.10 fails cuBLAS on GB10. It is not a LiteLLM
chat model.

**Access path.** The node-2 supervisor is private (fabric
192.168.100.11:18820, bearer key). gx10-01 exposes it:

* to the browser through GX-Playground;
* to API clients at `http://100.105.214.61:8090/v1/music/*`, with a gateway
  key that allows `gx-music`.

**Source and deployment.** `legenex/music/` is the canonical source,
integrated from the Stage A handoff. It includes the router-mediated eviction
patch. The node-2 unit is a symlink into the checkout; `GX_MUSIC_HOME` is no
longer overridden. `/srv/projects/gx-music-staging` is Review-class history.

**gx-max drain (`legenex/lifecycle/node2-holds.sh`).**

* **Before rank 1:** gx-max-start sets `state/guard/node2.gxmax-hold`, then
  waits for the supervisor to unload the engine. It stops the engine container
  itself only as a fallback. It verifies that the container is gone, that
  gx-music is not in the ledger and that no ACE-Step process remains, and only
  then starts rank 1.
* **On release:** stop, unwind and restore-normal clear the hold and make sure
  the supervisor runs.
* **Jobs:** music jobs submitted while gx-max owns the cluster wait with a
  gx-max reason. A render interrupted by the reclaim is re-queued (bounded)
  instead of failed.

**ComfyUI eviction.** Music frees idle ComfyUI weights only through
`docker exec gx-media-router python -m gx_media_router.free_node`, the path
gx-reason's start already uses. The router clears its resident-model record,
so its next job is admitted as cold (60/76 GiB), never as warm (8 GiB).
`GX_MUSIC_EVICT_COMFY_WEIGHTS` is back to its default (on) after the live
proof (TEST_RESULTS §20).

**Media router 2.3.0.**

* It mounts node 2's guard directory read-only.
* It refuses new jobs while the gx-max hold or Maintenance is active.
* It honours pins only above the 30 GiB reserve.
* It reports the resident alias and its last memory refusal.
* The refusal message names both possible tenants.

## D-037 — GX-Playground, Resource Control, Storage & Cleanup, client Setup

**Date:** 2026-09-17. **Status:** ACCEPTED.

**GX-Playground (`legenex/playground/`, `gx-playground.service`).**

* **Host and binds:** gx10-01 only, port 8090, bound to 127.0.0.1 and the
  Tailscale address.
* **Architecture:** a static single-page app plus an allow-listed streaming
  reverse proxy to the Control Center backend. That keeps one Library (SQLite
  schema 2 adds audio), one job queue and one session store. The session
  cookie is host-scoped, so one sign-in covers both ports.
* **Proxy trust:** the proxy adds `X-GX-Proxy-Token` (0600 file
  `secrets/control-ui/proxy-token`) and `X-GX-Forwarded-For`. The backend
  trusts the forwarded address only with that token and only from loopback.
* **Refused through the Playground:** runtime controls, storage cleanup and
  Maintenance.
* **Control Center:** Create and Media Library moved out of it and are
  replaced by a link.

**Resource Control (`gx_control_ui/resources.py`).** It explains and
coordinates the components that already enforce memory safety; it replaces
none of them.

* **Profiles:** Auto (default), Text, Media, Music, Max and Maintenance. They
  are priority and preemption preferences, persisted in
  `state/guard/profile.json` on both nodes.
* **Max** runs the existing gx-max acquire. Leaving Max releases gx-max, and
  the profile returns to Auto once gx-max is down.
* **Maintenance** writes `state/guard/node{1,2}.maintenance-hold`. The hold is
  honoured by:
  * `resource_guard.check_admission` and the takeover check;
  * gx-reason's start command, with the guard directory mounted into node 2's
    llama-swap;
  * media router 2.3;
  * the music supervisor;
  * the Control Center creative queue.
* **Pins** live in `state/guard/pins.json`:
  * music and media honour them only above the reserve;
  * gx-reason is kept alive by a proxied health request while it is READY and
    the pin is honoured;
  * a pin never overrides admission, gx-max or Maintenance.
* **Manual controls:** LOAD, UNLOAD, DRAIN, PIN and UNPIN go through
  ActionRunner, the router's free path or the supervisor's load/unload. None
  of them issues an arbitrary docker command.
* **Creative queue:** it asks the controller before submitting. Jobs wait with
  a plain-language reason, and the queue may free idle, unpinned tenants as
  the profile allows.
* **Compatibility:** computed from placement, live MemAvailable, the measured
  footprints and the enforced admission numbers. It is not a static matrix.

**Storage & Cleanup (`storage.py`, `storage_scan.py`).**

* **Classes:** a two-node scanner sorts candidates into SAFE, REVIEW and
  PROTECTED.
* **Opaque ids:** the browser sends only ids, which are HMACs over the scan
  result.
* **Re-check:** the owning node re-classifies each item right before it
  deletes it.
* **REVIEW items** need a typed confirmation and Maintenance.
* **Never offered:** a Docker `prune -a`, anything inside the Git checkout, or
  Library assets.
* **Health thresholds:** CRITICAL below 30 GiB free, LOW below 75 GiB, WATCH
  below 150 GiB.
* **Model Manager disk preflight:** download, staging, peak and headroom
  (50 GiB). An install that would not fit is blocked before it starts.

**Client Setup.** Kilo Code, Open WebUI and generic OpenAI clients each get a
page:

* the labels come from the installed Kilo Code 7.7.2 and Open WebUI 0.11.3;
* the page shows live values and a complete Kilo config file;
* its connection test uses a pasted key and reports the gx-auto routing
  decision;
* gx-auto is the recommended Kilo model;
* API keys may now also allow gx-music.

## D-038 — Media and music keep the locked 30 GiB reserve; Open WebUI identity comes from the registry

**Date:** 2026-09-17 (final cleanup pass). **Status:** ACCEPTED. Enforces the
existing rule (normal single-node operation keeps at least 30 GiB
MemAvailable); it does not change it.

**Why.** The final Playground run started a cold gx-video load while gx-music
was loaded, and gx10-02 fell to about 18.6 GiB MemAvailable (TEST_RESULTS
§20.4). That was recorded as acceptable. It was not: media router 2.3's
"cold 60 / 76 / 110 GiB" thresholds were the measured footprints plus
3-4 GiB, with **no** reserve. The Control Center used the same numbers, and
nothing stopped the router and the music supervisor from loading at the same
time.

**Admission rule (media router 2.4.0, gx-music 1.1.0, Control Center).**

    MemAvailable - memory other tenants have been granted but not taken yet
                 - the job's growth                               >= 30 GiB

* **Growth (measured 2026-09-17):**
  * cold image or edit: 57 GiB; cold t2v, i2v or v2v: 72 GiB; keyframe
    video edit: 107 GiB;
  * warm jobs: the footprint minus what the resident weights hold. The router
    measures that after each cold job, and the measurement is discarded if
    gx-music changed state in the meantime. The floor is 8 GiB. If the held
    amount is unknown, the full footprint is used; if that does not fit, the
    router frees its own weights and judges the job cold.
* **Pending memory:**
  * the router publishes `memory.pending_gib` on its open `/health`: the
    running job's growth minus what has already left MemAvailable;
  * the music supervisor publishes the same: while loading, 32 GiB minus
    what has gone; when ready, 32 GiB minus the measured resident size;
  * each side subtracts the other's pending memory, so two loads that start
    together cannot both pass on the same free memory.
* **Configuration:** `GX_MEDIA_RESERVE_GIB` and `GX_GUARD_RESERVE_GIB` can
  raise the reserve but are refused below 30. The footprints can only be
  raised. A configured but unreadable `/proc/meminfo` refuses work.
* **Never fits:** a job whose growth plus the reserve exceeds what gx10-02
  ever has available (117 GiB) is refused at submit (HTTP 422
  `exceeds_node_reserve`). Today that is only the keyframe video edit
  (107 + 30). See **B-028**.

**Making room without bypassing the router.**
* **Router → idle gx-music.** The router may unload an IDLE gx-music engine
  through the supervisor's own lifecycle path:
  `POST /v1/music/unload {"if_idle": true}`. The supervisor refuses while a
  render runs, jobs are queued or a pin is honoured. The router mounts the
  supervisor key read-only and never touches the engine container.
  * **Allowed only when** unloading would actually make enough room, and never
    with a pin, the Music / Maintenance / Max profile or a policy hold.
  * **Verified:** the engine reports unloaded, the container is gone, the
    ledger no longer lists gx-music, and MemAvailable is re-read. Only then is
    admission recomputed. An unverified unload does not admit.
* **gx-music → ComfyUI.** Unchanged: music still frees idle ComfyUI weights
  only through the router's free path.
* **Control Center.** The creative gate uses the same numbers, subtracts
  pending memory, no longer submits behind a busy router, unloads idle music
  with `if_idle`, and fails a never-fits job immediately.

**Waiting instead of failing.**
* **Router video jobs:** a video that does not fit stays `queued` with
  `phase: "waiting"` and a `waiting` object (code, reason, required /
  available / reserve / pending GiB, blocker, next step, since). This covers
  gateway clients too. It re-checks every 15 s for up to 30 minutes, then
  fails with `insufficient_memory` and the reason. The worker also waits,
  instead of starting, when a gx-max or Maintenance hold appears after
  submit.
* **Synchronous images:** refused with 503 and the same details. The
  Control Center queue turns that into a wait.
* **Music:** waits with a numeric reason naming the media job it waits for.

**Unchanged:** gx-max takeover, its drain of music and media, the
supervisor's gx-max handling, the profiles' preemption rules, pins, and
Maintenance.

**Open WebUI identity (production `open-webui`, chat.legenex.co).**
* **Cause:** the instance had no model entries for the gx aliases, so no
  system prompt reached the model. Asked "what model are you?", the 4B
  fine-tune answered from its training ("official Qwen3.5", in another chat
  "Grok-3 Mini"). Routing was correct.
* **Fix:** `gx_control_ui/owui_identity.py` writes one Open WebUI model entry
  per text alias (id and name = alias) with a short system prompt built
  **only** from `legenex/models/registry.json`:
  * each alias has an `identity` block with facts verified against the
    Hugging Face API and sha256;
  * the block is used only while its repository and revision match the
    alias's current binding. After a Model Manager reassignment, the prompt
    shrinks to the repository, revision and runtime.
* **How it is written:** through Open WebUI's own model layer inside the
  container. Entries are owned by the oldest admin and have no grants, so
  visibility is unchanged. Rows the sync did not create are never
  overwritten (`--adopt` is explicit).
* **Kept in sync by:**
  * Model Manager, after assign and rollback;
  * Setup → Open WebUI → *Sync identity*;
  * `python3 -m gx_control_ui.owui_identity check|apply`;
  * the daily integrity audit, which reports drift.
* **gx-auto** gets a router prompt that names no model.

**The registry is now tracked in Git.** `.gitignore`'s `models/` pattern had
excluded `legenex/models/registry.json`, the file the Control Center, the
tests and the documentation all treat as the source of truth. It is now
re-included; model weights stay ignored.
