# Tasks

The prioritized build queue. This file reflects **what remains**, not what has
already been done — completed work is described in `CURRENT_STATE.md`,
`TEST_RESULTS.md` and `CHANGELOG.md`, not repeated here. Checked items are
kept only long enough to show recent motion before being removed.

Last reviewed: 2026-09-16 (second session), after eight instrumented gx-max
runs, the gx-max failure-unwind rebuild, and real gx-image/gx-video
generations. Cross-checked against live system state (see `CURRENT_STATE.md`).

---

## P0 — safety and stability

- [x] ~~**Physically power-cycle gx10-02**~~ — **not needed; node 2
  recovered itself 2026-09-16.** No power cycle ever happened (uptime shows
  22 h of continuous runtime, straight through the incident). The kernel
  OOM-killed the orphaned `gx-max-rank1` after 80 minutes
  (`OOMKilled=true`) and userspace un-starved on its own.
  `recover-node2.sh` then passed every substantive check. B-020 has been
  corrected accordingly — for this failure shape, wait ~80 min and re-probe
  before dispatching a human.
- [x] ~~Download `nvidia/Qwen3.6-27B-NVFP4` and run gx-reason tests A-E~~ —
  **done and verified live 2026-09-16; B-011 is CLOSED.** 20.42 GiB
  downloaded to `/srv/models/vllm/Qwen3.6-27B-NVFP4` (sizes match the HF API
  exactly), `node02.yaml` deployed and `-validate`-checked, all of A-E
  passed: the original B-011 repro prompt now answers " Paris.", and a
  multi-step reasoning question is answered correctly through the real
  gateway. Numbers in `TEST_RESULTS.md`.
- [x] ~~Re-run gx-max and find out whether tuning is enough~~ — **done,
  2026-09-16, eight instrumented two-node runs. The answer is no.** Loading
  one TP=2 rank takes a 121.63 GiB node to between 437 MiB and 0 MiB
  MemAvailable on BOTH nodes. `--mem-fraction-static` was measured at 0.50
  and 0.70 with an *identical* trough, so it does not touch the peak at all;
  `--context-length`, `--chunked-prefill-size`, `--cuda-graph-max-bs-decode`,
  `--max-running-requests`, the container `--memory` cap (106g→28g) and
  `--load-format` (layered / runai_streamer) were all tested too. Numbers in
  `TEST_RESULTS.md` §15.1.
- [ ] **HUMAN DECISION REQUIRED — B-022: what reserve, or what locked change?**
  This is the single thing standing between the cluster and a complete
  seven-tier platform. gx-max's measured per-rank peak is ~117 GiB of a
  121.63 GiB node, so the admission guard refuses it whatever the reserve is
  set to. The three options (accept a ~20 GiB steady-state reserve with a
  documented load excursion; keep 30 GiB and retire gx-max; or reopen L-6 for
  a smaller model / heavier quantisation / more nodes) are written up with
  the supporting measurements in `coordination/BLOCKERS.md` B-022. Do not
  resolve it by editing `GXMAX_GUARD_RESERVE_GIB` — that was tested and does
  not make gx-max fit.
- [x] ~~Deploy the resource-ownership/admission-control layer to node 2~~ —
  **done and verified 2026-09-15.** `legenex/lifecycle/` +
  `legenex/orchestrator/` are now on node 2 too; a normal launch is admitted
  and an oversized one is correctly refused, against node 2's own real
  `/proc/meminfo` and lock file. `gx-hostwatch.sh` is running there as a
  systemd `--user` timer.
- [ ] Rewrite `gx-max-start.sh`'s rank1 launch to call through the
  now-deployed node-2 resource-guard module directly, instead of its
  original real remote `flock` convention. Small consistency cleanup, not a
  safety gap (the `flock` convention is real and correct). Lower priority
  than it was: while B-022 is open, that launch path never executes.
- [x] ~~Make an orphaned rank impossible after a failed gx-max launch~~ —
  **done and proven on the real workload 2026-09-16.** `rank1-deadman.sh`
  (resident on node 2, needs no ssh), `gx-max-unwind.sh` (bounded retries,
  confirms both ranks gone, verifies memory/swap/locks/ledgers/SSH/fabric)
  and an EXIT trap in `gx-max-start.sh` that no failure path can miss.
  Regression suite: `legenex/tests/unwind-tests.sh`.
- [x] ~~Real gx-image and gx-video generations~~ — **done 2026-09-16.**
  1024x1024 PNG in 26.5 s and a 33-frame 640x640 h264 MP4 (Wan 2.2) in
  48.1 s, both visually verified, both well above the 30 GiB memory floor,
  clean unload afterwards. `TEST_RESULTS.md` §15.3-15.5.
- [ ] Arm the hardware watchdog on both nodes (`coordination/BLOCKERS.md`
  B-014) — needs a human with sudo, and a conscious decision on the timeout
  given gx-max's long cold start. Not done unilaterally.
- [ ] `apt-mark hold` the pinned kernel packages on both nodes (B-001) —
  needs root. Kernel 7.0 is still installed on both nodes as of the last
  check.
- [ ] Decide + apply a fix for SGLang `:30000` binding `0.0.0.0` with no auth
  (B-003) — either `--host 127.0.0.1` (needs testing that rank1 can still
  reach rank0's bootstrap on port 5000) or a firewall rule.

## P1 — finish the core text-model platform

- [x] ~~Decide gx-reason's replacement engine/model~~ — done (D-021):
  `nvidia/Qwen3.6-27B-NVFP4` on vLLM. What remains is deployment/live-test,
  moved to P0 above (blocked on node 2 being physically recovered, B-020).
- [x] Cold-start and live-verify `gx-fast` again — **done this session**:
  real completion through the gateway, correct answer to a multi-step
  logic question ("9"), ~2m7s cold start. See `TEST_RESULTS.md`.
- [ ] Re-run `legenex/tests/gx-max-validate.sh` (full acquire → serve →
  release → restore) — **attempted this session, see B-020**: got further
  than ever before (both ranks passed admission, rank1 started, rank0
  started) but rank0 was OOM-killed during weight loading, which then left
  node2 wedged. Needs a re-run once node2 is physically recovered, with the
  90->95 GiB estimate change (D-020) in place.
- [ ] Reconcile gx-reason's admission-guard sizing once the new
  vLLM/Qwen3.6-27B-NVFP4 checkpoint has actually been exercised live
  (currently 45 GiB, a documented ceiling-above-expected estimate, not yet
  a real measurement — see D-021).
- [ ] Re-measure gx-reason's admission estimate now that a real figure
  exists — **the 45 GiB ceiling in `resource_guard.py` measured true**
  (~44 GiB real node-level footprint), so this is now a confirmation to
  record rather than an open question; the remaining work is deciding
  whether to tighten 45 -> ~46-48 GiB with a small explicit margin, or leave
  the round number.
- [ ] Decide what to do about B-021 (`--memory` cgroup caps do not bound the
  CUDA pool on this hardware). No action is required for safety — the
  admission guard reads real `/proc/meminfo` — but `node01.yaml` still
  carries the original overstated "enforced backstop" wording that
  `node02.yaml` has had corrected.
- [ ] Give `lib.sh`'s `n2()` SSH helper a hard `timeout`, not just
  `ConnectTimeout` — the newer recovery/validation scripts already wrap
  their own SSH calls this way; `n2()` itself was deliberately left alone
  pending a dedicated pass (see `coordination/BLOCKERS.md` B-012).

## P2 — media

- [ ] Run a real `gx-image` generation through the gateway end-to-end and
  verify the output file — ComfyUI is provisioned on node 2 (confirmed
  reachable, `STOPPED`/on-demand as of 2026-09-15) and the router has 43
  passing unit/protocol tests, but no real E2E generation has been recorded
  yet.
- [ ] Same for `gx-video`.
- [ ] Wire HiDream I1 Full and a no-LoRA "quality" Wan 2.2 variant into
  `legenex/media/workflows/` with a `_gx`-enabled template — both checkpoints
  are verified/documented in `MODELS.md` but neither is servable today.
- [ ] Decide on LTX 2.3 (licence review, B-008) if video is ever used for
  commercial/client work.

## P3 — productionization

- [ ] Add a writable git remote (fork or the gitea backup) — nothing can be
  pushed right now (`coordination/BLOCKERS.md` B-013). Needs a human to
  create/authorize it; do not put a token in a remote URL.
- [ ] Basic observability: node_exporter / DCGM exporter / log shipping
  (B-006). `--enable-metrics` on gx-max is the only piece done so far.
- [ ] A documented, tested boot-ordering story beyond "large models never
  auto-start" — management services, gateway, orchestrator, on-demand
  tiers, in that order, verified across an actual reboot.
- [ ] A real reboot/recovery acceptance test (see `TEST_PLAN.md` §10) — not
  yet run on either node.
- [ ] A remote-access-over-Tailscale acceptance test (`TEST_PLAN.md`, item
  not yet run).
- [ ] A consolidated final benchmark suite — individual tier numbers exist
  in `TEST_RESULTS.md`, but nothing runs them all as one report.
- [ ] Checksum manifest for the duplicated 164 GB gx-max checkpoint across
  both nodes (B-005) — currently copied by hand with no verification that
  the two copies stay identical.

## Explicitly out of scope for now

- Hermes/Buzz integration — mentioned in the original project brief, not
  referenced anywhere in the current implementation; needs a scoping
  conversation with the human before it becomes a task.
