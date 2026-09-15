# Handoff

Filled-in handoff for whoever (human or agent) picks this up next. The blank
format for future recurring updates lives in
`docs/chatgpt/STATUS_UPDATE_TEMPLATE.md` — use that one going forward; this
file always holds the *latest* handoff, not a history (see `CHANGELOG.md`,
`coordination/LEADER_STATUS.md` and `TEST_RESULTS.md` for history).

Last updated: 2026-09-15 ~12:55 CEST, by the lead agent on gx10-01, at the
end of a full two-node completion pass following node 2's recovery.

## Current objective

Just completed: node 2 resource-ownership deployment, gx-reason diagnosis
(rebuild attempt), real gx-image/gx-video E2E validation (first time ever
run), a first real gx-max acquisition attempt through the orchestrator, a
full acceptance-suite run, and an independent multi-agent review pass with
findings fed back and fixed. See `CHANGELOG.md`'s `[Unreleased]` section for
the complete, detailed list — this file is the short version.

## Current node ownership

- gx10-01 GPU owner: none (idle; gx-mini/gx-fast on demand only)
- gx10-02 GPU owner: none (idle; gx-reason on demand; gx-comfyui +
  gx-media-router running but idle — built and started this session for
  the first time, low footprint at idle, on-demand generation only)
- Active model runtimes: none on either node
- Active media runtimes: gx-comfyui + gx-media-router (idle, healthy)

## Last verified good state (this session, live-checked, real inference)

- Kernel: `6.17.0-1032-nvidia` on both nodes.
- Memory: node 1 ~108 GiB available, node 2 ~113 GiB available, of 121 GiB
  each — both nodes confirmed clean at end of session.
- gx-mini, gx-fast: real text inference verified, both PASS.
- gx-reason: real inference confirmed still BROKEN (B-011, garbage GPU
  output) — rebuilding from current llama.cpp master did NOT fix it.
- gx-auto: real routing verified for all 3 test cases, including correct
  escalation to gx-reason — this required first finding and fixing a real
  production bug (D-019, orchestrator boot-race left it unreachable from
  the gateway container for 2.5+ hours).
- gx-image, gx-video: real generation verified for the first time ever —
  genuine 1024x1024 image (28s) and playable MP4 (58s), both visually
  inspected. Ingress security boundary (ComfyUI unreachable from node 1)
  now has a real regression test, not just a manual check.
- gx-max: first real acquisition attempt through the orchestrator.
  Correctly, safely refused by the admission guard (B-017) — not a memory
  incident, a genuine unresolved collision between two locked designs that
  needs a human decision. Cleanup verified correct on both nodes.
- `legenex/tests/acceptance.sh` (non-slow suite): 12 PASS, 1 FAIL (gx-reason,
  expected), 1 SKIP (no vision fixture).
- Independent multi-agent review (4 reviewers: memory/lifecycle,
  networking/gx-max, routing/media security, recovery/docs) found several
  real issues, all fixed except one newly-documented gap — see
  `coordination/BLOCKERS.md` B-019 (node2 admission-check TOCTOU race).

Full detail: `CURRENT_STATE.md`.

## Files changed this session

Application code AND documentation both changed — see
`git log --oneline 3920192..HEAD` on `legenex-dual-gx10` for the exact
commit list. Highlights:

- `legenex/lifecycle/gx-max-start.sh`, `legenex/tests/gx-max-validate.sh` —
  fixed a dead container-name list in the conflict-drain step, and replaced
  a `ping`-based fabric check (fails under the orchestrator's
  `NoNewPrivileges=true` hardening) with a capability-free TCP probe.
- `legenex/orchestrator/systemd/` — new: the orchestrator's systemd unit
  checked into git for the first time, plus a boot-race fix
  (`wait-for-docker0.sh`).
- `legenex/tests/acceptance.sh` — `t_media` no longer skips (real E2E
  tests), memory-interlock hardening added to `t_reason`/`t_media`, plus a
  bug in that same cleanup code found and fixed live.
- `legenex/scripts/recover-node2.sh`, `legenex/scripts/gx-reason-diagnose.sh`
  — real bugs fixed (SSH argument quoting; a missing-library failure
  misdiagnosed as a CPU-vs-GPU result).
- `coordination/BLOCKERS.md` — B-017 (gx-max vs admission guard, needs a
  human decision), B-018 (ComfyUI's compose start bypasses the admission
  guard), B-019 (node2 admission-check race, found by independent review)
  added; B-011 updated (rebuild attempted, ruled out, still open).
- `coordination/DECISIONS.md` — D-019 (orchestrator boot-race) added.
- `CURRENT_STATE.md`, `ARCHITECTURE.md` — brought current.

## Tests completed

`legenex/orchestrator` 116, `legenex/lifecycle/tests` 9, `legenex/media/router`
43 — all still passing (no regressions from this session's code changes).
Plus the live acceptance suite above, plus real inference/generation against
every tier that can currently serve one.

## Current blocker

**B-017 (S1) needs a human decision, not further automated attempts.**
gx-max cannot acquire through the real orchestrator: its locked ~90 GiB/rank
footprint doesn't leave the admission guard's 30 GiB reserve floor. Three
options are documented in `coordination/BLOCKERS.md` B-017 — none applied,
on purpose, since either direction (loosen the guard, or leave gx-max
permanently unable to acquire through production) is a real architecture
call that shouldn't be made unilaterally.

Other open items, unchanged in kind: B-011 (gx-reason, needs a human decision
on which next step to fund), B-013 (no writable git remote), B-001/B-003
/B-014 (all need root).

## Next action

1. Get a human decision on B-017 (gx-max admission floor) and B-011
   (gx-reason next step) — these are the two things blocking full
   production readiness that this session could not resolve itself.
2. B-019 (S2, found by independent review): node2's admission check in
   `gx-max-start.sh` is not atomic with the actual rank1 launch — a real
   TOCTOU window, distinct from B-018. Fix suggested in the blocker entry.
3. B-018: wire ComfyUI's compose-based start through the resource-ownership
   admission guard properly (currently mitigated in the test suite only,
   not fixed at the source).
4. `gx-max-start.sh`'s rank1 launch still doesn't call through the
   node-2-deployed resource-guard module directly (uses a real remote
   `flock` convention instead) — small consistency cleanup, not urgent.

## Locked decisions reminder

- Kernel 6.17 pin, never 7.0.
- ConnectX/RoCE for model traffic, Tailscale management-only.
- `gx-max` = SGLang TP=2, `nvidia/DeepSeek-V4-Flash-0731-NVFP4`, never vLLM.
- LiteLLM gateway, llama-swap lifecycle.
- Qwen3.8 retired permanently, do not resurrect.
- 30 GiB `MemAvailable` reserve floor (see B-017 for the one place this
  collides with another locked decision, unresolved).
- No large model auto-starts at boot.

Full list with rationale: `ARCHITECTURE.md` §1, `CLAUDE.md`.
