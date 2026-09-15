# Current state

**This file must always reflect reality.** If you are a new agent resuming this
work, read this first, then ARCHITECTURE.md (what is locked), then BLOCKERS.md.

Last updated: 2026-09-15 10:40 CEST, by the lead agent on gx10-01, during
integration of the ChatGPT-project seed files into the canonical repo. This
update is based on **live checks run against both nodes during this session**
(SSH, `docker ps`, `free -h`, `curl` health endpoints, `recover-node2.sh`), not
just a re-read of prior notes — see "Verified live this session" below.

---

## One-paragraph summary

**Both nodes are healthy right now.** Node 2 — reported in the previous update
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

**Node 2** (verified via SSH + `gx-node2ctl status` inside `recover-node2.sh`):

| Service | State |
|---|---|
| llama-swap node 2 (`gx-llama-swap-node02`) | **healthy**, both loopback and fabric-reachable |
| gx-reason | `NOT_PROVISIONED` in the node2ctl summary (placeholder line — do not read as a real status; the authoritative source is node 1's orchestrator, which reports `gx-reason: stopped/usable`) |
| ComfyUI | `STOPPED` — on-demand, correct; present/provisioned (not "not installed") |
| gx-max rank 1 | `STOPPED` — correct |
| GPU owner | `free` |

No large model is resident on either node. No large model was started during
this session's integration work (deliberately — this was a documentation
integration pass, not a validation run).

## Tier status

| Alias | Model | Engine | Node | State right now |
|---|---|---|---|---|
| gx-mini | Qwen3.5-4B Q4_K_M + BF16 mmproj | llama.cpp | 1 | **stopped, on-demand.** Previously verified working end-to-end (text + vision) through the gateway multiple times; infra healthy |
| gx-fast | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM | 1 | **stopped, not cold-started this session either.** Verified statically present on disk (22G, 3 shards) and correctly wired; no memory-safety reason to hold it back right now (node 1 has 114 GiB available), just not exercised in a docs-integration pass |
| gx-reason | `unsloth/Qwen3.5-122B-A10B-GGUF` UD-Q4_K_XL | llama.cpp | 2 | **infra reachable (node 2 recovered), but functionally BROKEN — B-011 OPEN.** GPU path produces garbage output; isolated to a CUDA/GDN kernel bug, not the checkpoint. Do not route real traffic here until fixed |
| gx-max | `nvidia/DeepSeek-V4-Flash-0731-NVFP4` | SGLang TP=2 | 1+2 | **stopped, down.** Both nodes are now reachable so an acquire should be *possible*, but `gx-max-validate.sh` has not been re-run since node 2's recovery — treat as unverified until it is |
| gx-auto | — | orchestrator | 1 | **working** — routing logic unchanged this session |
| gx-image | Qwen-Image 2512 (+Lightning LoRA) / HiDream I1 (not wired) | ComfyUI | 2 | **infra reachable; real E2E generation still NOT RUN** through the gateway (see `TEST_PLAN.md`) |
| gx-video | Wan 2.2 A14B (LTX 2.3 not used — licence) | ComfyUI | 2 | **infra reachable; real E2E generation still NOT RUN.** No "hd"/no-LoRA tier wired yet |

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

* **B-011** gx-reason is functionally broken on GPU (garbage output),
  isolated to a CUDA/GDN kernel bug, still OPEN — see above.
* **B-001** the kernel pin has no `apt-mark hold`; kernel 7.0 is still
  installed on both nodes. Needs root.
* **B-003** SGLang `:30000` is bound `0.0.0.0` with no auth.
* **B-013** no writable git remote is configured — 9+ local commits on
  `legenex-dual-gx10` not yet pushed anywhere.
* **B-016** no BMC/IPMI/Redfish path on either node — a future wedge needs a
  human physically present.
* `gx-max-start.sh`'s rank1 launch still doesn't call through the (now
  node-2-deployed) resource-guard module directly — it uses its original
  real remote `flock` convention. Cosmetic/consistency gap, not a safety one.
* gx-image/gx-video real end-to-end generation has never been run through
  the gateway.

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
