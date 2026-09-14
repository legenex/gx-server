# Leader status — gx10-01

Agent role: LEAD. Owns the canonical git repo at
`/home/legenex/Documents/Projects/Server/gx-cluster`, branch `legenex-dual-gx10`.

Last updated: 2026-09-14 18:15 CEST.

---

## Headline

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
