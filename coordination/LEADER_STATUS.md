# Leader status — gx10-01

Agent role: LEAD. Owns the canonical git repo at
`/home/legenex/Documents/Projects/Server/gx-cluster`, branch `legenex-dual-gx10`.

Last updated: 2026-09-16 ~11:30 CEST.

---

## Headline (2026-09-16, gx-max memory investigation + media completion)

Six of the seven public tiers serve real, verified output. The seventh —
`gx-max` — does not fit on this hardware, and that is now measured across
**eight instrumented two-node runs** rather than suspected. Loading one TP=2
rank takes a 121.63 GiB node from ~110 GiB MemAvailable to between 437 MiB and
0 MiB on *both* nodes and ends in a kernel global OOM kill. Every tuning lever
was tested; `--mem-fraction-static` was measured at 0.50 and 0.70 with an
identical trough, because the trough is the model weights (a 163.48 GiB
checkpoint puts ~82 GiB on each rank) and not the KV/static pool.

`coordination/BLOCKERS.md` **B-022** carries the numbers and the three options.
It is the one thing that needs a human, alongside the unrelated kernel
`apt-mark hold` (one sudo command per node).

Also done this session: the B-020 orphan-rank failure mode is fixed and proven
on the real workload (a node-2-resident deadman, a dedicated unwind with
verification, and an EXIT trap no failure path can miss); real `gx-image` and
`gx-video` generations, inspected as files; `gx-auto` no longer acquires
gx-max; and gx-max's health no longer reports a fake `usable: true`.

---

## Headline (2026-09-15, ChatGPT project-seed integration session)

Integrated the ChatGPT-Project bundle (`_project_seed/`) into the canonical
repo: merged genuinely new facts (node LAN/Tailscale IPs, an NCCL
`all_gather_perf` benchmark, the BMC/IPMI/Redfish absence finding, the
GDM/RDP stale-session procedure) into `OPERATIONS.md`, `RECOVERY.md`,
`TEST_RESULTS.md` and `coordination/BLOCKERS.md`; added the root-level
`DECISIONS.md`/`TASKS.md`/`TEST_PLAN.md`/`HANDOFF.md` the target layout
requires (as pointers to the existing canonical logs, not competing ones —
see D-018); added `docs/chatgpt/` with reference copies for re-upload to the
ChatGPT Project; removed `_project_seed/`.

While validating the seed's `CURRENT_STATE.md` against this repo's own
(which the seed could not have known about — it predates the prior session's
detailed work), found real drift against the **live machines**, not just
between documents: node 2 — last documented as physically wedged, needing a
power cycle — had already been power-cycled by the human operator and was
fully recovered; ran the report-only `recover-node2.sh` fresh and got 16
PASS / 0 FAIL. Separately, node 1's gateway container was found `Exited
(128)` (a benign restart artifact — its DB connection had been
administratively terminated, not a crash) and was brought back up and
re-verified healthy. `CURRENT_STATE.md` has been substantially rewritten to
match. No application code changed, no large model started.

## Headline (2026-09-15, B-011 diagnosis session, immediately prior)

Fixed two script bugs (`recover-node2.sh`'s memory check, and
`gx-reason-diagnose.sh`'s CPU-only comparison) and re-ran the B-011
GPU-vs-CPU comparison against the newly-recovered node 2: isolated the
garbage-output bug to the CUDA/GDN kernel execution path for the
`qwen3_5_moe` hybrid architecture (ruled out the checkpoint/quant and a
stale build). B-011 remains OPEN; see `coordination/BLOCKERS.md`.

## Headline (2026-09-15, resource-ownership session)

Node 2 was found physically wedged at session start (B-012: two large
mmap'd models resident at once, bypassing llama-swap's exclusivity via a
bare `docker run`) and remains so — a physical power cycle is required, and
nothing in this session touched it beyond confirming it's still down.
Everything below happened on node 1 only.

Built a resource-ownership/admission-control layer (`ARCHITECTURE.md` §9)
that makes B-012's exact failure mode structurally refused, not just
documented — proven by concurrency tests (5 real racing OS processes,
SIGKILL-recovery, cross-language bash/Python flock contention), plus
Docker memory caps/oom-score-adj hardening and a dependency-free host
watchdog. Found and fixed, independently, live tonight: a crash-looping
gateway (lost its Docker network attachment), an orchestrator that had
never been given a systemd unit and wasn't running at all despite prior
docs claiming otherwise, a tier-health-probe bug that reported the dead
node 2's gx-reason as healthy, two real gx-auto routing bugs, three real
media-router security gaps (auth-bypass magic strings, cross-kind workflow
mixing, a header-injection defense-in-depth gap), and a gx-max
failed-acquire path that could leak a partially-started ~80-90 GiB rank.
Retired Qwen3.8 (`vllm-qwen38-uncensored`, ~80 GiB unmanaged, unrelated to
the seven-alias tier set) per the human operator's direct action. 168
automated tests passing across three independent suites (orchestrator,
lifecycle, media router), all runnable without node 2. 8 local commits this
session on `legenex-dual-gx10`; upstream remains non-writable (B-013,
unchanged) — not pushed.

See `CURRENT_STATE.md` for the full current-state snapshot and
`coordination/BLOCKERS.md` for the exact remaining human-action list.

## Headline (prior session, 2026-09-14 18:15 CEST)

**gx-max works.** The two-node SGLang DeepSeek V4 Flash engine came up cleanly on
the pinned 6.17.0-1032 kernel, passed the FlashInfer autotune stage that kernel
7.0 had broken, and is serving real inference at ~65 tok/s with 772 MB/generation
of RDMA traffic across both ConnectX rails. Full evidence in TEST_RESULTS.md.

## Done

* gx-max validated end-to-end (health, both ranks, fabric, throughput, samples).
* Exact working launch configuration captured and turned into reproducible
  scripts: `legenex/lifecycle/{gx-max.conf,lib.sh,gx-max-start.sh,gx-max-stop.sh,gx-max-status.sh}`.
* Orchestrator built (`legenex/orchestrator/`): deterministic gx-auto router +
  serialised gx-max acquisition, stdlib-only, 29 unit tests passing, verified
  live against the running engine.
* Gateway configs authored (`legenex/gateway/`): LiteLLM + llama-swap node1/node2
  + compose + env sample, all YAML-validated, secret-scanned.
* Verified real checkpoints for gx-fast and gx-reason (MODELS.md).
* Verified all four media model IDs, all ungated (MODELS.md).
* Docs: ARCHITECTURE.md, MODELS.md, TEST_RESULTS.md, coordination/.

## In flight

* gx-fast weights downloading to node 1 (`nvidia/Qwen3.6-35B-A3B-NVFP4`, 23.5 GB).
* gx-reason weights downloading to node 2
  (`et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10`, 78.8 GB, tmux session `gxdl`).

## Next

1. Finish downloads.
2. Release gx-max (it currently holds both nodes) → bring up gx-mini + gateway.
3. Wire and test gx-fast, then gx-reason.
4. Build ComfyUI for aarch64/sm_121, stage media models, build the media router.
5. Run the full acceptance suite including a real drain → acquire → release cycle.

## What I own vs the worker

I own: the git repo, all canonical config, the orchestrator, lifecycle scripts,
documentation, and all git operations.

The gx10-02 worker owns: node-2 runtime prep, node-2 validation, node-2 local
services, media prep, and node-2 benchmarks. It reports results; I integrate.

**The worker must not write into the repo.** It had been writing to
`coordination/node2/` — that path is now gitignored and the worker has been asked
to use `/home/legenex-02/gx-worker/WORKER_RESULTS.md` instead.

## Next (2026-09-15, current — see `TASKS.md` for the full list)

1. ~~Physical: node 2 needs a power cycle~~ — **done**, verified clean
   (16/16 `recover-node2.sh` checks pass).
2. ~~Run `gx-reason-diagnose.sh` (B-011) once node 2 is confirmed clean~~ —
   **done, in the session before this one.** Isolated to a CUDA/GDN kernel
   bug; still OPEN, needs a human decision on the next step (see
   `coordination/BLOCKERS.md` B-011).
3. ~~Deploy `legenex/lifecycle/` + `legenex/orchestrator/` to node 2~~ —
   **done, by a second concurrent session, verified 2026-09-15** (see
   `coordination/BLOCKERS.md` B-012). Remaining: rewrite `gx-max-start.sh`'s
   rank1 launch to call through it directly instead of its original real
   remote `flock` convention (small consistency cleanup, not a safety gap).
4. Run `legenex/tests/gx-max-validate.sh` for the full two-node lifecycle —
   the last full pass predates the B-012 wedge and node 2's recovery.
5. Decide whether to wire HiDream and a video "hd" tier into
   `legenex/media/workflows/` before calling gx-image/gx-video complete.
6. Reconcile gx-reason's documented 78 GiB memory budget in `node02.yaml`
   against the measured ~95 GiB footprint (D-013) once B-011 is fixed and
   the real footprint is known to be stable.
