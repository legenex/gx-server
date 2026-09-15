# Tasks for the gx10-02 worker

Written by the LEAD agent on gx10-01. Copy of record lives in the repo; a copy is
pushed to `/home/legenex-02/gx-worker/WORKER_TASKS.md`.

Report results in `/home/legenex-02/gx-worker/WORKER_RESULTS.md`.
**Do not write into the git repo on gx10-01.**

---

## Standing rules

1. **Never** stop, kill or restart `gx-max-rank1` unless the lead asks.
2. **Never** upgrade the kernel. It is pinned at `6.17.0-1032-nvidia` because
   kernel 7.0 broke RDMA memory registration. Do not run `apt upgrade`,
   `apt autoremove`, or any firmware update.
3. **Never** change MTU, Netplan, RDMA config, ConnectX firmware, or routing.
4. GPU passthrough on this node is **CDI**: `--device nvidia.com/gpu=all`.
   There is no `nvidia` docker runtime. `--gpus all` will not work.
5. No sudo is available. Use Docker and `systemctl --user`.
6. Do not start large models without checking free memory first; gx-max takes
   the whole node when it is up.

## T-1 — Enable user lingering (blocked, needs the human)
`loginctl show-user legenex-02` reports `Linger=no`, so user services will not
survive logout or reboot on this node. Try `loginctl enable-linger legenex-02`;
if polkit refuses without a password, record it in WORKER_RESULTS.md and stop.
**Still open as of 2026-09-15.**

## Superseded / done — kept for history, no action needed

- ~~T-2 (watch the gx-reason download)~~ — done; superseded anyway, since
  gx-reason moved from vLLM to llama.cpp/GGUF (D-009). The vLLM checkpoint
  this task downloaded is no longer what's served.
- ~~T-3 (find a vLLM image with real sm_121a NVFP4 kernels)~~ — moot for
  gx-reason (moved to llama.cpp, D-009). Still relevant background for
  gx-fast's B-010 (weight-only FP4 via Marlin, not native) if anyone
  revisits that, but not an active task.
- ~~T-4 (ComfyUI feasibility research + Dockerfile)~~ — done; ComfyUI is
  provisioned on node 2 (confirmed reachable, `STOPPED`/on-demand as of
  2026-09-15) with real workflow templates in `legenex/media/workflows/`
  and 43 passing router tests.
- ~~T-5 (report node-2 memory when gx-max releases)~~ — the memory model is
  well-established now (`ARCHITECTURE.md` §9, `resource_guard.py`); no
  longer a standalone task.

## T-6 — done: resource-guard ledger deployed to node 2

~~Node 2 has no resource-guard ledger deployed yet~~ — **done and verified
2026-09-15** by the lead: `legenex/lifecycle/` + `legenex/orchestrator/`
are on node 2, a normal launch is admitted and an oversized synthetic one
is correctly refused against node 2's own real `/proc/meminfo` and lock
file, and `gx-hostwatch.sh` is running there as a `systemd --user` timer.
See `coordination/BLOCKERS.md` B-012. Remaining, separate piece: rewrite
`gx-max-start.sh`'s rank1 launch to call through the module directly
instead of its original real remote `flock` convention — not urgent, that
convention is correct, just not the same code path as node 1.

## T-7 — Real end-to-end gx-image / gx-video generation

`TEST_PLAN.md` §8-9 are unchecked: no actual image or video has been
generated through the gateway yet, only unit/protocol tests against the
router. Once the lead confirms it's safe to load a media model (node 2 has
~116 GiB available as of 2026-09-15), generate one real image and one real
video through `gx-image`/`gx-video` and report the output file paths and
timings in `WORKER_RESULTS.md`.
