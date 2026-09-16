# Current state

**This file must always reflect reality.** If you are a new agent resuming this
work, read this first, then ARCHITECTURE.md (what is locked), then BLOCKERS.md.

## LATEST UPDATE — 2026-09-16 ~09:00-11:00 CEST — read this section first

**Six of the seven public tiers serve real output. The seventh, `gx-max`, does
not fit on this hardware, and that is now measured rather than suspected.**

| Tier | State | Evidence |
|---|---|---|
| `gx-mini` | **SERVING** | real answer through the gateway |
| `gx-fast` | **SERVING** | real answer through the gateway |
| `gx-reason` | **SERVING** | bat-and-ball answered correctly, `reasoning_content` separated |
| `gx-max` | **REFUSED, not serving** | admission guard refuses on measured numbers; see below |
| `gx-auto` | **SERVING** | routes correctly and never escalates to gx-max |
| `gx-image` | **SERVING** | real 1024x1024 PNG, 26.5 s, visually correct |
| `gx-video` | **SERVING** | real 33-frame h264 MP4, Wan 2.2, 48.1 s, visually correct |

### gx-max: the 30 GiB reserve is not achievable, and neither is gx-max

Eight instrumented two-node runs today. Loading one TP=2 rank takes a
121.63 GiB node from ~110 GiB MemAvailable down to **between 437 MiB and 0
MiB — on both nodes** — and ends in a kernel global OOM kill of the SGLang
scheduler. Every tuning lever was tried and **none of them moves that peak**:
`--mem-fraction-static` (measured identical troughs at 0.50 and 0.70),
`--context-length`, `--chunked-prefill-size`, `--cuda-graph-max-bs-decode`,
`--max-running-requests`, the container `--memory` cap (106g down to 28g), and
`--load-format` (`layered` is unsupported for this NVFP4 path;
`runai_streamer` loads but changes nothing).

The reason is arithmetic, not configuration: the checkpoint is 163.48 GiB, so
at the locked `--tp 2` each rank holds ~82 GiB of weights — two thirds of a
node — before any KV cache, and the loader adds ~26 GiB of host-side pinned
memory on top. `--mem-fraction-static` only ever moves the *steady state*.

**What this means in practice:** `gx-max` is exposed by the gateway but the
admission guard refuses every acquisition, before launching anything, because
117 GiB (the measured peak) plus any reserve exceeds a 121.63 GiB node. That
refusal is deliberate and correct. Making gx-max runnable needs a human
decision about a LOCKED constraint — the model, the quantisation, or the node
count. **The numbers and the three options are in `coordination/BLOCKERS.md`
B-022; what was changed and why is in `coordination/DECISIONS.md` D-022.**

Do not "fix" this by lowering `GXMAX_GUARD_RESERVE_GIB`. It was tested: even a
5 GiB reserve does not make gx-max fit, because the peak is the whole node.

### The orphan-rank failure from B-020 is fixed and proven on the real workload

This was the other half of the task and it is done. Three layers now:
`rank1-deadman.sh` (a watchdog resident **on node 2**, armed before rank0
starts, that force-removes rank1 when rank0 disappears — no ssh required),
`gx-max-unwind.sh` (a dedicated failure path with bounded node-2 retries that
*confirms* both ranks are gone and verifies memory, swap, locks, ledgers and
SSH/Tailscale/fabric health), and an EXIT trap in `gx-max-start.sh` that no
failure path can miss.

Proven, not asserted: five synthetic tests pass, and on the real DeepSeek
workload the deadman fired on node 2 at 1 GiB MemAvailable and the node came
straight back to 117 GiB — the exact condition that cost 80 minutes in B-020.
Three unwind runs reported `UNWIND COMPLETE — cluster verified clean`.

One node-2 wedge did still occur (run 5) and took **17 minutes** to clear
itself, against 80 minutes for B-020. During it, node 2 answered ICMP **and
TCP:22 on the ConnectX fabric** while Tailscale was completely dark — so the
fabric is the better liveness probe during a wedge. See B-020 for the
corrected, no-physical-intervention-first recovery procedure.

### Measured today

| Item | Value |
|---|---|
| gx-image | 1,274,968 B PNG, 1024x1024, 26.5 s, 1043 distinct colours sampled |
| gx-video | 100,023 B h264 MP4, 640x640, 33 frames @16 fps, all 33 frames distinct, 48.1 s |
| Media unload | node2 47 → 114 → 117 GiB, swap unchanged |
| gx-image / gx-video min MemAvailable | 59.7 GiB / 42.8 GiB — both well above the 30 GiB floor |
| gx-max per-rank load peak | ~117 GiB of a 121.63 GiB node |
| gx-max steady state (if it could load) | ~26 GiB free at `--mem-fraction-static 0.70` |
| Checkpoint integrity | node1 and node2 shard manifests byte-identical |

Full numbers: `TEST_RESULTS.md` §15.

---

## EARLIER UPDATE — 2026-09-16 ~07:30-08:15 CEST

**Both nodes are healthy and gx-reason works for the first time.** The two
things that were blocking this project are both closed:

**1. Node 2 is back — and it recovered ITSELF. No power cycle happened.**
The previous update said node 2 was physically wedged and needed a human at
the machine (B-020, B-016). That turned out to be wrong, and the evidence is
unambiguous: node 2's `uptime` shows a continuous 22 h since a boot at
2026-09-15 09:33 — hours *before* the incident. What actually ended it is
visible in `docker inspect gx-max-rank1`: `OOMKilled=true`,
`FinishedAt=2026-09-15T22:44:48Z`. The orphaned rank1 held the node for 80
minutes, the kernel's OOM killer eventually reclaimed it against its own
`--memory 106g` cap, and userspace un-starved on its own. The host-resilience
design from B-012 worked; it was just slow. `recover-node2.sh` now passes
every substantive check. **Operational consequence: for this failure shape,
wait ~80 minutes and re-probe before dispatching a human.** See B-020 for the
corrected record.

**2. B-011 is RESOLVED — gx-reason serves correct output through the
gateway.** The Qwen3.5-122B-A10B GGUF/llama.cpp tier that produced
`////////////////////` for every prompt has been replaced, per D-021, with
`nvidia/Qwen3.6-27B-NVFP4` on vLLM (the same image already proven for
gx-fast). Downloaded, deployed and live-tested end to end today. The clinching
result is the original repro prompt, run identically against the new engine:

```
"The capital of France is"  ->  " Paris."      (was "////////////////////")
```

and through the real LiteLLM gateway, a multi-step reasoning question
answered correctly (bat-and-ball: `$0.05`, not the `$0.10` trap) with
`reasoning_content` properly separated. vLLM drives the *same* GDN
linear-attention kernels llama.cpp got wrong — confirming D-021's diagnosis
that the architecture was never the problem, that llama.cpp build's CUDA
implementation of it was.

**All four text tiers are now working: gx-mini, gx-fast, gx-reason, and
gx-max remains the one unproven tier** (its last acquisition OOM-killed
rank0 mid-load, which is what caused incident 2 above; the 90->95 GiB
estimate change from D-020 has not been re-tested yet).

**One new finding, recorded as B-021:** `--memory` cgroup caps do NOT bound a
model's real footprint on this hardware. Measured with gx-reason loaded: the
node lost 44 GiB of MemAvailable while the container's own `memory.current`
read 10.92 GiB — the CUDA pool is not charged to the container cgroup on
DGX Spark unified memory. The admission guard is unaffected (it reads the
node's real `/proc/meminfo`), and `--gpu-memory-utilization` is what actually
bounds the pool, but several memory-budget comments in this repo overstated
what the cgroup cap enforces.

**Measured, live, today (full numbers in `TEST_RESULTS.md`):**

| Item | Value |
|---|---|
| gx-reason checkpoint on disk (node 2) | 21,921,697,184 B = 20.42 GiB, 3 shards, matches HF exactly |
| gx-reason cold start | 401 s to first token (~225 s weights, 177 s engine init) |
| gx-reason generation | 12.4 tok/s (dense 27B, unified memory) |
| gx-reason real node footprint | ~44 GiB (MemAvailable 114 -> 70 GiB) |
| gx-reason unload | 70 -> 116 GiB MemAvailable in ~5 s |
| Reasoning-token cost of one simple question | 1468 of 1690 completion tokens |

The last figure is why the gateway's `gx-reason` output budget was raised
from 8192 to 16384 tokens (and input lowered to 49152 to match the 65536
context): on a reasoning tier, `<think>` content spends the output budget.

**What remains, in priority order:** re-run `gx-max-validate.sh` to test the
D-020 fix; real end-to-end `gx-image`/`gx-video` generations; then the
root-requiring items (kernel `apt-mark hold`, watchdog) that still need a
human with sudo. See `TASKS.md`.

---

Last updated: 2026-09-15 12:35 CEST, by the lead agent on gx10-01, after a
full autonomous two-node completion pass (Phases 1-6 of the recovery/
validation task: node 2 recovery, resource-ownership deployment to node 2,
gx-reason diagnosis, real gx-image/gx-video E2E validation, a first-ever
gx-max acquisition attempt through the real orchestrator, and a full
acceptance-suite run). This update is based on **live checks and real
inference/generation against both nodes**, not a re-read of prior notes.
See `CHANGELOG.md`'s `[Unreleased]` section for the full list of fixes and
findings; the highlights: two real production bugs found and fixed
(gx-orchestrator boot-race, D-019; gx-max-start.sh's dead conflict-drain
list), one real, unresolved architecture decision surfaced and documented
rather than worked around (gx-max vs the 30 GiB reserve floor, B-017), and
gx-image/gx-video validated end-to-end for the first time (real generations,
visually inspected).

---

## One-paragraph summary — HISTORICAL, as of 2026-09-15 12:35

> Everything from here down is the 2026-09-15 snapshot, kept for the record.
> The power cycle it mentions is the *earlier* B-012 recovery, not the B-020
> incident — B-020 needed no power cycle at all (see the top of this file).
> For current state, read the LATEST UPDATE section above.

**Both nodes were healthy at the time of this 2026-09-15 update.** Node 2 — reported in the previous update
as physically wedged, needing a power cycle — has since been power-cycled by
the human operator and is **confirmed clean**: `recover-node2.sh` passed all
16 checks (kernel, driver, Docker, both ConnectX rails, `/swapfile-sglang`,
disk, no stale containers, no accidental large workload, llama-swap reachable
both locally and over the fabric). On node 1, the gateway container
(`gx-litellm`) had exited cleanly about an hour before this session started
(its Postgres connection was administratively terminated — benign, not a
crash loop) and has been restarted and re-verified healthy. The orchestrator
correctly reports all four tiers as `stopped`/`unloaded` (normal, on-demand)
and `gx-max` as `down`. Separately, in the session immediately before this
one, `BLOCKERS.md` B-011 (gx-reason produces garbage output) was narrowed
from "unknown cause" to "isolated to the CUDA/GDN kernel execution path of
this llama.cpp build for the `qwen3_5_moe` hybrid architecture" — **still
OPEN**, not fixed, ruled out as a checkpoint/quant or stale-build problem.

## Verified live this session (2026-09-15, ~10:30–10:40 CEST)

| Check | Result |
|---|---|
| Node 1 kernel | `6.17.0-1032-nvidia` (confirmed via `uname -r`) |
| Node 1 memory | 112 GiB free / 114 GiB available of 121 GiB, swap 0/63 GiB used |
| Node 2 SSH (`ssh legenex-02@gx10-02`) | **reachable**, banner completes normally |
| Node 2 kernel | `6.17.0-1032-nvidia` (matches node 1) |
| Node 2 memory | 116 GiB available of 121 GiB, swap 2.8/63 GiB used |
| Node 2 `nvidia-smi` | OK — GB10, driver `580.173.02`, 47°C |
| Both ConnectX/RoCE rails (`192.168.100.x`, `192.168.101.x`) | ACTIVE / LINK_UP, 0% loss, sub-ms RTT, both directions |
| `/swapfile-sglang` (48 G) | present on **both** nodes |
| `recover-node2.sh` (report-only) | **16 PASS / 0 FAIL / 0 WARN / 1 SKIP** (nothing to clear) |
| Node 1 `gx-litellm` (gateway) | was `Exited (128)`; restarted via documented `docker compose up -d`; now `Up`, `health: healthy`, `HTTP 200` on `/health/liveliness` |
| Node 1 `gx-llama-swap-node01`, `gx-litellm-db` | healthy throughout, unaffected |
| Node 1 `gx-orchestrator.service` (systemd --user) | `active running`, `/health/detailed` returns `status: ok`, all four tiers `stopped`/`usable: true`, `gx-max: down` |
| Node 2 `gx-llama-swap-node02` | healthy, reachable on loopback **and** from node 1 over the fabric (`192.168.100.11:28080`) |
| Node 2 ComfyUI / gx-reason / gx-max | all correctly report `STOPPED` / `NOT_PROVISIONED` (on-demand; nothing auto-started) |

**Why the gateway had exited:** `docker logs gx-litellm` showed a clean
graceful shutdown triggered by `"terminating connection due to administrator
command"` from Postgres — i.e. something explicitly closed the DB connection
(most likely a `docker restart`/compose action during the prior session,
consistent with all node-1 containers showing "Up about an hour" at session
start). Not a crash, not a bug. Restarted per `RECOVERY.md` §1 and confirmed
healthy.

## Node 2: RESOLVED — was physically wedged, now recovered

Previously (2026-09-14 session): node 2's kernel stayed alive (ICMP replied
0% loss) but userspace was starved — SSH could not complete a banner
exchange, llama-swap did not answer. Root cause: two ~77 GB mmap'd models
resident on a 121 GiB node at once (see `coordination/BLOCKERS.md` B-012 for
the full incident and the admission-control layer built in response).

**Since then:** the human operator physically power-cycled node 2. This
session ran the documented, report-only `legenex/scripts/recover-node2.sh`
and it passed every check (see table above) — node 2 is not just "pinging
again", it is verified clean: correct kernel, both fabric rails up, the
required swapfile present, no stale containers, no accidental resident
workload, and its own llama-swap answering both locally and across the
fabric.

**Update, later the same session:** the resource-guard ledger/lock module
(`legenex/lifecycle/` + `legenex/orchestrator/`) has since been deployed to
node 2 and independently verified there too — a normal launch is admitted
correctly against node 2's own real `/proc/meminfo` and lock file, and a
deliberately oversized synthetic launch is correctly refused. `gx-hostwatch.sh`
is also now running as a `systemd --user` timer on node 2. See
`coordination/BLOCKERS.md` B-012 for the exact evidence. **Node 2 now has the
same structural protection node 1 has**, with one piece still separate:
`gx-max-start.sh`'s rank1 launch still uses its original real remote `flock`
convention rather than having been rewritten to call through the newly
deployed module — real, not-yet-done work, distinct from "the module isn't
there."

**Still true / not yet done:**
- No BMC/IPMI/Redfish/MCTP remote power path exists on either node — a
  future B-012-style wedge still requires a human physically present (see
  `coordination/BLOCKERS.md` B-016).
- `legenex/scripts/gx-reason-diagnose.sh` and `legenex/tests/gx-max-validate.sh`
  have **not** been re-run against the freshly-recovered node 2 by this
  session (this integration deliberately did not start any large model — see
  below); the gx-reason diagnostic *was* already re-run in the session
  immediately prior (see next section).

**Note on process:** this file, and specifically the "no ledger deployed"
claim above, was edited concurrently by a second agent session working the
same repo at the same time, without any coordination protocol between the
two. Both sessions' edits landed in git history (see `coordination/
DECISIONS.md` D-018 and `coordination/BLOCKERS.md`'s own note on this). Not
adversarial — the concurrent session's claims were independently verifiable
and checked out — but a real operational gap worth a human's attention if
multiple agents are going to work this repo at once going forward.

## B-011 — gx-reason garbage output: narrowed, still OPEN

Carried over from the prior session, immediately before this one (see
`CHANGELOG.md` "Unreleased" and `coordination/BLOCKERS.md` B-011 for full
detail — summarized here because it materially changes what "gx-reason
status" means):

- Fixed two script bugs that were silently invalidating diagnostics:
  `recover-node2.sh`'s memory check (an SSH quoting bug made it WARN instead
  of actually checking), and `gx-reason-diagnose.sh`'s CPU-only comparison
  (it omitted the CDI GPU device entirely, so the "CPU-only" container failed
  to start at all — `llama-server` is dynamically linked against
  `libcuda.so.1`, which requires the device to be mounted even to run
  `--n-gpu-layers 0`).
- With both fixed, re-ran the real GPU-vs-CPU comparison against the
  recovered node 2: **GPU path reproduces the exact original garbage
  (`////////////////////`); CPU-only path with identical weights and
  sampling is coherent** (`"The capital of France is Paris."`). This rules
  out the checkpoint/quant.
- Rebuilt `legenex/llama-cpp-spark` from current upstream `llama.cpp` master
  to rule out a stale build — build succeeded, **identical GARBAGE/SANE
  split reproduced byte-for-byte** on the new binary. Rules out a stale
  build too.

**Conclusion: the bug is real and isolated to the CUDA/GDN kernel execution
path for the `qwen3_5_moe` hybrid architecture on this hardware** — either an
upstream `llama.cpp` correctness bug for `sm_121`/GB10, or something specific
to this driver/CUDA combination. **gx-reason cannot be served correctly on
GPU today.** Running it CPU-only is not a real fix (a 122B-class MoE model on
CPU is far too slow to be a usable tier) and has not been deployed. See
`coordination/BLOCKERS.md` B-011 for the three concrete next steps (upstream
issue research, an alternate quant, or a bisect) — none attempted yet, each
is real, separate work requiring a human decision before starting (a new
quant means a new multi-GB download).

## Second, unrelated node-1 incident from the prior session (found and fixed then)

1. **`gx-litellm` had lost its Docker network attachment entirely** and was
   crash-looping against an unreachable `litellm-db:5432`. Fixed by
   recreating it via `docker compose up -d litellm`.
2. **The orchestrator was not running at all** — no process, no systemd unit
   had ever existed for it. Fixed: started it and added
   `~/.config/systemd/user/gx-orchestrator.service` (enabled, hardened,
   binds only `127.0.0.1,172.17.0.1:18900`). Confirmed still `active running`
   this session.

Separately, **`vllm-qwen38-uncensored`** (~80 GiB resident, unrelated to the
seven-alias tier set) was identified as a major memory-safety risk and has
been **permanently retired** — container and checkpoint deleted, all active
runtime/download/routing/lifecycle references removed from the repo
(`c0076f8`, see `coordination/DECISIONS.md` D-015). Confirmed this session:
`/opt/models/` on node 1 no longer contains a Qwen3.8 directory.

## Resource ownership (built in response to B-012, unchanged this session)

Direct response to the B-012 root cause. Full detail in `ARCHITECTURE.md`
§9 and `coordination/DECISIONS.md`; summary here:

- `legenex/orchestrator/gx_orchestrator/resource_guard.py` +
  `legenex/lifecycle/resource-guard.sh` — one shared arithmetic module
  (bash shells out to the same Python module): workload classes (small/
  medium/large/exclusive), a 30 GiB minimum-reserve floor checked against
  both a residency ledger AND live `/proc/meminfo`, a flock-backed
  cross-process `NodeLock`.
- `legenex/lifecycle/gx-safe-run.sh` — the sanctioned replacement for a bare
  `docker run` on any medium/large/exclusive container.
- `gx-max-start.sh`/`gx-max-stop.sh` route both rank launches through the
  hard, non-bypassable admission guard; `_do_acquire()` unwinds any
  partially-started rank on a failed acquire instead of leaking it.
- Docker `--memory`/`--memory-swap` caps and `--oom-score-adj` biasing on
  every model container.
- `legenex/host/gx-hostwatch.sh` — dependency-free watchdog (systemd
  `--user` timer), logs and alerts only.

**Now deployed and verified on both nodes** — see "Node 2" above for the
2026-09-15 deployment evidence. `gx-max-start.sh`'s rank1 launch itself still
uses its original real remote `flock` convention rather than calling through
the module directly; that rewrite is separate, not-yet-done work.

## Hardware

| | gx10-01 (node 1, control) | gx10-02 (node 2, compute) |
|---|---|---|
| DGX Spark version | 7.5.0 | 7.5.0 |
| Kernel | `6.17.0-1032-nvidia` — **confirmed live 2026-09-15** | `6.17.0-1032-nvidia` — **confirmed live 2026-09-15** |
| Arch / Python / Docker | aarch64 / 3.12.3 / 29.2.1 | aarch64 / 3.12.3 / 29.2.1 (confirmed) |
| GPU / driver / CUDA | GB10, 580.173.02, CUDA 13.0 | GB10, 580.173.02 — confirmed live |
| RAM | 121 GiB | 121 GiB |
| Swap | 63 GiB (`/swap.img` + `/swapfile-sglang` 48 G) | 63 GiB (same two files) — confirmed live |
| sudo | **password required** | **password required** |
| User lingering | enabled | disabled (T-1 in `coordination/WORKER_TASKS.md`, blocked on the human) |
| Linux user | `legenex` | `legenex-02` |
| LAN | `10.60.21.37` | `10.60.21.41` |
| Tailscale (management only) | `100.105.214.61` | `100.73.238.4` |
| ConnectX rail A | `192.168.100.10` | `192.168.100.11` |
| ConnectX rail B | `192.168.101.10` | `192.168.101.11` |

Mac management device Tailscale IP: `100.104.35.71`.

GPU passthrough is **CDI** (`--device nvidia.com/gpu=all`) on both nodes. There
is no `nvidia` docker runtime and no `/etc/docker/daemon.json`.

## Fabric

| Rail | node 1 | node 2 | state |
|---|---|---|---|
| A `enp1s0f0np0` / `rocep1s0f0` | 192.168.100.10 | 192.168.100.11 | **ACTIVE, confirmed live 2026-09-15** (both directions) |
| B `enP2p1s0f0np0` / `roceP2p1s0f0` | 192.168.101.10 | 192.168.101.11 | **ACTIVE, confirmed live 2026-09-15** (both directions) |

Custom NCCL 2.30.7 built for SM121; a two-node `all_gather_perf` (16 GiB)
passed with zero errors, ~21.3 GB/s average bus bandwidth — see
`TEST_RESULTS.md`. A single 400-token gx-max generation independently moved
772 MB of RDMA traffic split near-evenly across both rails.

Tailscale is management/SSH only — confirmed by measurement, not assumption.
`ssh legenex-02@gx10-02` (Tailscale) is the correct SSH endpoint; SSH directly
to `192.168.100.11` is refused — the fabric addresses are not SSH endpoints.

## What is running right now

**Node 1** (verified via `docker ps`, `systemctl --user`, direct `curl`):

| Service | Port | State |
|---|---|---|
| LiteLLM gateway (`gx-litellm`) | 4000 (loopback) | **healthy** (restarted this session, see above) |
| Postgres (`gx-litellm-db`) | 15432 (loopback) | **healthy** |
| llama-swap node 1 (`gx-llama-swap-node01`) | 28080 / 19001 (loopback) | **healthy** |
| gx-orchestrator (systemd `--user`) | 18900 (loopback + docker bridge) | **active running**, `/health/detailed` → `ok` |
| open-webui | — | **healthy** (not part of the gx tier set) |
| gx-mini / gx-fast / gx-reason (llama-swap-managed) | via llama-swap | **stopped** — on-demand, correct |
| gx-max rank 0/1 (SGLang) | 30000 | **stopped** — `down`, correct |

**Node 2** (verified live at end of this session — `free -h`, `docker ps`):

| Service | State |
|---|---|
| llama-swap node 2 (`gx-llama-swap-node02`) | **healthy**, both loopback and fabric-reachable |
| gx-comfyui + gx-media-router | **healthy, running idle** (built and started this session — first time ever). Low footprint at idle (~2-3 GiB); ComfyUI's per-generation model cache is explicitly freed after each test (see B-018 for the one gap this doesn't close: a generation run by hand, outside the test suite, still leaves ~70 GiB cached until `/free` is called or `docker compose down`) |
| gx-reason | `STOPPED` — unloaded after testing, correct |
| gx-max rank 1 | `STOPPED` — never successfully started this session, see B-017 |
| GPU owner | `free` |
| MemAvailable | **113 GiB** |

No large model is resident on either node right now. Real inference/generation
WAS run against gx-mini, gx-fast, gx-reason (confirmed broken), gx-image, and
gx-video this session — see the Tier status table below for results.

## Tier status

| Alias | Model | Engine | Node | State right now |
|---|---|---|---|---|
| gx-mini | Qwen3.5-4B Q4_K_M + BF16 mmproj | llama.cpp | 1 | **WORKING.** Real text inference verified live this session through the gateway; stopped/on-demand now |
| gx-fast | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM | 1 | **WORKING.** Real text inference + tool-calling (`get_weather`) verified live this session; stopped/on-demand now |
| gx-reason | ~~`unsloth/Qwen3.5-122B-A10B-GGUF`~~ **REPLACED 2026-09-15** → `nvidia/Qwen3.6-27B-NVFP4` | ~~llama.cpp~~ → vLLM | 2 | **Old combination confirmed broken and rejected (B-011). Replacement fully configured (D-021), NOT yet live-tested** — node2 went down (B-020) before the new checkpoint could be downloaded. Highest-priority next step once node2 is back |
| gx-max | `nvidia/DeepSeek-V4-Flash-0731-NVFP4` | SGLang TP=2 | 1+2 | **DOWN. B-017's admission refusal is RESOLVED (D-020) — both ranks passed admission and rank1/rank0 both started for the first time ever this session.** Then rank0 was OOM-killed during weight loading, and the orphaned rank1 left node2 wedged — **node2 is currently down, see B-020, needs a physical power cycle.** Not yet fully validated end-to-end |
| gx-auto | — | orchestrator | 1 | **WORKING — a real, live-production bug was found and fixed this session (D-019).** The orchestrator's `172.17.0.1` bind failed at this morning's boot (race with `docker0` getting its address) and nobody noticed for 2.5+ hours: every gx-auto request from the LiteLLM container was silently unable to reach the classifier. Fixed (ExecStartPre wait-for-docker0) and reverified: all 3 routing test cases pass, including correct escalation to gx-reason for a hard-reasoning prompt |
| gx-image | Qwen-Image 2512 (+Lightning LoRA) / HiDream I1 (not wired) | ComfyUI | 2 | **WORKING — real E2E generation verified for the first time this session.** Built+started the media stack (previously never deployed), real 1024x1024 image via the gateway in 28s, visually inspected (a genuine hummingbird/sailboat, not noise). Stopped again after testing (on-demand is the intent, though nothing currently auto-restarts it — see B-018) |
| gx-video | Wan 2.2 A14B (LTX 2.3 not used — licence) | ComfyUI | 2 | **WORKING — real E2E generation verified for the first time this session.** Real playable MP4 via the router's async contract in 58s. No "hd"/no-LoRA tier wired yet (unchanged gap) |

**Known media gap, unchanged:** HiDream-I1-Full and a no-LoRA "quality" Wan
variant are documented in `MODELS.md` as available checkpoints but have no
`_gx`-enabled template in `legenex/media/workflows/` yet.

## Automated test count (unchanged this session — no code touched)

168 tests passing across three independent suites, all runnable without a
live cluster:

```
legenex/orchestrator:        116 tests   (python3 -m unittest discover -s . -p 'test_*.py')
legenex/lifecycle/tests:       9 tests   (python3 -m unittest discover -s tests -p 'test_*.py')
legenex/media/router:         43 tests   (./qa.sh)
```

## Repository layout (unchanged this session)

```
legenex/orchestrator/gx_orchestrator/
  resource_guard.py      workload sizing, admission math, NodeLock, ResidencyLedger
  health.py               per-tier real-upstream health probing
  status_cli.py           `gx status` implementation
legenex/lifecycle/
  resource-guard.sh       bash-side admission-control library
  gx-safe-run.sh          sanctioned replacement for a bare `docker run`
  tests/                  bash-side resource-guard tests
legenex/host/
  gx-hostwatch.sh         dependency-free host resilience watchdog
  systemd/                its service+timer templates
legenex/scripts/
  gx-status.sh            `gx status` entry point
  recover-node2.sh        node-2 recovery checklist (report-only until --apply)
  gx-reason-diagnose.sh   B-011 GPU-vs-CPU diagnostic, unload-gated
legenex/tests/
  gx-max-validate.sh      full acquire->serve->release->restore validation
```

## Known gaps

See `coordination/BLOCKERS.md` for the full list with severities. The ones
that matter most right now:

* **B-017 (S1, new this session)** gx-max cannot acquire through the real
  orchestrator: its locked ~90 GiB/rank footprint doesn't leave the
  admission guard's 30 GiB reserve floor. Needs a human decision between
  three documented options — not a bug, a genuine unresolved design
  collision. See ARCHITECTURE.md §5 and BLOCKERS.md B-017.
* **B-011** gx-reason is functionally broken on GPU (garbage output),
  isolated to a CUDA/GDN kernel bug — confirmed this session it is NOT a
  stale-build problem (rebuilt from current llama.cpp master, identical
  result). Still OPEN.
* **B-018 (S2, new this session)** ComfyUI's `docker compose up` start does
  not go through the resource-ownership admission guard at all — a real
  near-miss (node 2 hit ~10 GiB available mid-testing) was caught live and
  the test suite hardened, but the underlying gap in the launch path itself
  is not fixed.
* **B-001** the kernel pin has no `apt-mark hold`; kernel 7.0 is still
  installed on both nodes. Needs root.
* **B-003** SGLang `:30000` is bound `0.0.0.0` with no auth.
* **B-013** no writable git remote is configured — commits on
  `legenex-dual-gx10` not yet pushed anywhere.
* **B-016** no BMC/IPMI/Redfish path on either node — a future wedge needs a
  human physically present.
* `gx-max-start.sh`'s rank1 launch still doesn't call through the (now
  node-2-deployed) resource-guard module directly — it uses its original
  real remote `flock` convention. Cosmetic/consistency gap, not a safety one.
* ~~gx-image/gx-video real end-to-end generation has never been run through
  the gateway~~ — **done this session**, both confirmed working with real,
  visually-inspected output.

## How to resume

```bash
cd /home/legenex/Documents/Projects/Server/gx-cluster
legenex/scripts/gx-status.sh                   # one-shot cluster status, human + --json
curl -s localhost:18900/health/detailed        # orchestrator + tier view
cd legenex/orchestrator && python3 -m unittest discover -s . -p 'test_*.py'
```

Node 2 is already recovered — do not re-run `recover-node2.sh --apply`
speculatively; the report-only form is safe to re-run any time to re-confirm.

Suggested next real work (see `TASKS.md` for the full prioritized list):

```bash
legenex/tests/gx-max-validate.sh               # full two-node lifecycle, unverified since node2's recovery
# gx-reason: do NOT re-attempt the same diagnostic — B-011 is already isolated.
# Next step there is a human decision (upstream issue, new quant, or a bisect).
```
