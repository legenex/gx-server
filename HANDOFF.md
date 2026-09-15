# Handoff

Filled-in handoff for whoever (human or agent) picks this up next. The blank
format for future recurring updates lives in
`docs/chatgpt/STATUS_UPDATE_TEMPLATE.md` — use that one going forward; this
file always holds the *latest* handoff, not a history (see `CHANGELOG.md`,
`coordination/LEADER_STATUS.md` and `TEST_RESULTS.md` for history).

Last updated: 2026-09-15 ~10:45 CEST.

## Current objective

Just completed: integrating the ChatGPT-project seed bundle
(`_project_seed/`) into this canonical repo — merging genuinely new facts
(node LAN/Tailscale IPs, NCCL benchmark numbers, the BMC-absence and
RDP/GDM findings) while discarding anything the repo's own, more recent and
more detailed docs had already superseded, and correcting doc drift found
along the way (node 2 had actually already been recovered from its B-012
wedge, and the gateway needed a restart — see `CURRENT_STATE.md`).

## Current node ownership

- gx10-01 GPU owner: none (idle; gx-mini/gx-fast on demand only)
- gx10-02 GPU owner: none (idle; gx-reason/ComfyUI/gx-max-rank1 on demand only)
- Active model runtimes: none on either node
- Active media runtimes: none

## Last verified good state (this session, live-checked)

- Kernel: `6.17.0-1032-nvidia` on both nodes, confirmed via `uname -r`.
- Memory: node 1 ~114 GiB available, node 2 ~116 GiB available, of 121 GiB
  each.
- Connectivity: SSH both directions works; both ConnectX/RoCE rails ACTIVE
  both directions.
- Health endpoints: gateway `/health/liveliness` → 200; orchestrator
  `/health/detailed` → `status: ok`, all four tiers `stopped`/`usable: true`,
  `gx-max: down`.
- `legenex/scripts/recover-node2.sh` (report-only): 16 PASS / 0 FAIL / 0 WARN.

Full detail: `CURRENT_STATE.md`.

## Files changed this session

Documentation and coordination files only — no application code touched, no
containers left in a different state than found except the gateway restart
(see below). See `git diff --stat` / `git log -1` on `legenex-dual-gx10` for
the exact list; summary:

- `CURRENT_STATE.md` — substantially rewritten to match live-verified state.
- `coordination/BLOCKERS.md` — B-012 marked resolved (node 2 recovered),
  B-016 added (no BMC/remote-power path).
- `TEST_RESULTS.md`, `RECOVERY.md`, `OPERATIONS.md`, `CLAUDE.md` — additive
  updates (see `CHANGELOG.md`).
- New at root: `DECISIONS.md`, `TASKS.md`, `TEST_PLAN.md`, this file.
- New: `docs/chatgpt/` with reference copies of the ChatGPT-project files.
- `_project_seed/` removed after integration.

One live operational action was taken, not just documentation: the node-1
gateway container (`gx-litellm`) was found `Exited (128)` (benign — its
Postgres connection had been administratively terminated, not a crash) and
was restarted via the documented `docker compose up -d` procedure, then
re-verified healthy.

## Tests completed

No application test suites were re-run this session (no code changed). The
168-test count from the prior session (`legenex/orchestrator` 116,
`legenex/lifecycle/tests` 9, `legenex/media/router` 43) is unaffected. Live
verification performed instead: gateway health, orchestrator health,
`recover-node2.sh`, fabric ping both directions, SSH both directions — all
recorded in `TEST_RESULTS.md` §12.

## Current blocker

None blocking this session's own work. The cluster's real open blockers are
unchanged in kind, just corrected in status — see `coordination/BLOCKERS.md`:
B-011 (gx-reason garbage output, isolated to a CUDA kernel bug, needs a human
decision on next step), B-013 (no writable git remote), B-001/B-003/B-014
(all need root). None are new to this session.

## Next action

See `TASKS.md` for the full prioritized list. Smallest, most valuable next
steps (updated after a concurrent second agent session deployed the
resource-guard ledger to node 2 during this same window — see the note
below):
1. Re-run `legenex/tests/gx-max-validate.sh` — the last full pass predates
   the B-012 wedge and node 2's recovery.
2. Get a human decision on which B-011 next step to fund.
3. Rewrite `gx-max-start.sh`'s rank1 launch to call through the now-deployed
   node-2 resource-guard module directly (small consistency cleanup).

**Note on concurrent work:** while this session was writing documentation,
a second Claude Code session was independently working the same repo and
deployed the resource-guard admission-control layer to node 2 (previously
this session's own top next-action item). No coordination protocol exists
between concurrent sessions on this repo today — see `coordination/
DECISIONS.md` D-018 and `coordination/BLOCKERS.md` B-012's note on this. If
multiple agents are going to work this repo concurrently going forward, the
human operator should decide whether that needs a lock/lease mechanism.

## Locked decisions reminder

- Kernel 6.17 pin, never 7.0.
- ConnectX/RoCE for model traffic, Tailscale management-only.
- `gx-max` = SGLang TP=2, `nvidia/DeepSeek-V4-Flash-0731-NVFP4`, never vLLM.
- LiteLLM gateway, llama-swap lifecycle.
- Qwen3.8 retired permanently, do not resurrect.
- 30 GiB `MemAvailable` reserve floor.
- No large model auto-starts at boot.

Full list with rationale: `ARCHITECTURE.md` §1, `CLAUDE.md`.
