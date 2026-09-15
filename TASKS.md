# Tasks

The prioritized build queue. This file reflects **what remains**, not what has
already been done — completed work is described in `CURRENT_STATE.md`,
`TEST_RESULTS.md` and `CHANGELOG.md`, not repeated here. Checked items are
kept only long enough to show recent motion before being removed.

Last reviewed: 2026-09-15, during integration of the ChatGPT project-seed
files, cross-checked against live system state (see `CURRENT_STATE.md`).

---

## P0 — safety and stability

- [x] ~~Deploy the resource-ownership/admission-control layer to node 2~~ —
  **done and verified 2026-09-15.** `legenex/lifecycle/` +
  `legenex/orchestrator/` are now on node 2 too; a normal launch is admitted
  and an oversized one is correctly refused, against node 2's own real
  `/proc/meminfo` and lock file. `gx-hostwatch.sh` is running there as a
  systemd `--user` timer.
- [ ] Rewrite `gx-max-start.sh`'s rank1 launch to call through the
  now-deployed node-2 resource-guard module directly, instead of its
  original real remote `flock` convention. Small consistency cleanup, not a
  safety gap (the `flock` convention is real and correct).
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

- [ ] **Fix or route around B-011** (gx-reason produces garbage output on
  GPU; isolated to a CUDA/GDN kernel bug in this llama.cpp build, not the
  checkpoint). Needs a human decision on which of the three documented next
  steps to fund: file/research an upstream `llama.cpp` issue, download a
  different quant to test, or bisect llama.cpp history. See
  `coordination/BLOCKERS.md` B-011.
- [ ] Cold-start and live-verify `gx-fast` again — it is wired and the
  checkpoint is on disk, but has not been exercised end-to-end since before
  the node-1 gateway/orchestrator incident.
- [ ] Re-run `legenex/tests/gx-max-validate.sh` (full acquire → serve →
  release → restore) now that node 2 has been power-cycled and re-verified —
  the last full pass predates the wedge.
- [ ] Reconcile gx-reason's admission-guard sizing (95 GiB, from the measured
  B-011 footprint) against `node02.yaml`'s stale 78 GB comment once B-011 is
  actually fixed and the real working-set is known to be stable
  (`coordination/DECISIONS.md` D-013).
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
