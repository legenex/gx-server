# Leader status — gx10-01

Agent role: LEAD. Owns the canonical git repo at
`/home/legenex/Documents/Projects/Server/gx-cluster`, branch `legenex-dual-gx10`.

Last updated: 2026-09-15 00:15 CEST.

---

## Headline (2026-09-15 session)

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

## Next (2026-09-15, current)

1. **Physical:** node 2 needs a power cycle by a human — nothing remote can
   recover it.
2. Run `legenex/scripts/recover-node2.sh` (report-only, then `--apply`) the
   moment it's back, before touching anything else on it.
3. Deploy this session's `legenex/lifecycle/` + `legenex/orchestrator/`
   admission-control layer to node 2 so it gets the same structural
   protection node 1 now has (currently node 1-only).
4. Run `legenex/scripts/gx-reason-diagnose.sh` (B-011) once node 2 is
   confirmed clean — do not skip straight to re-downloading a different
   quant.
5. Run `legenex/tests/gx-max-validate.sh` for the full two-node lifecycle.
6. Decide whether to wire HiDream and a video "hd" tier into
   `legenex/media/workflows/` (gap found tonight, not blocking) before
   calling gx-image/gx-video complete.
7. Reconcile gx-reason's documented 78 GiB memory budget in `node02.yaml`
   against the measured ~95 GiB footprint (D-013) once B-011 is fixed and
   the real footprint is known to be stable.
