# Mission Handoff — GX10 DeepSeek V4.1 Flash Clean Rebuild

If context is compacted or a new agent takes over:

1. Read this file, then state/PROGRESS.md, state/BACKLOG.md, state/DECISIONS.md,
   state/EVIDENCE.md, state/BLOCKERS.md.
2. The mission brief lives in the conversation that started 2026-09-27
   ("GX DUAL GX10 DEEPSEEK V4.1 FLASH CLEAN REBUILD"); key points mirrored in
   DECISIONS.md D-M1..M4.
3. Canonical repo: /home/legenex/Documents/Projects/Server/gx-cluster (this
   state/ dir). gx10-01 is the only Git writer (autosync → PUBLIC repo — never
   put secrets in tracked files). gx10-02 is pull-only.
4. Environment: no sudo; Docker + systemctl --user; CDI GPUs; SSH node2 =
   ssh legenex-02@gx10-02; ConnectX rails 192.168.100.10/11 + 192.168.101.10/11.
5. Protected: /home/legenex/Documents/Projects/gx-backup, Documents/Backups/GX/
   ZIPs, git tag pre-deepseek-v41-rebuild-20260927 on both remotes.
6. Do not raw-`docker stop` model containers — use the resource-guard /
   orchestrator path while old stack still runs (D-036 discipline applies
   until the old stack is decommissioned in PHASE 7-8).

## Phase 15 headless transition — what is happening (2026-09-27 16:5x)
The head node cannot boot the model with the desktop running (needs 111.5 GiB
MemAvailable; 106 with desktop, ~112+ without). Per mission Phase 12, the detached
runner ~/bin/dsv41-baseline-launch.sh:
1. terminates ONLY the graphical wayland session (VS Code desktop, Chrome, gnome die —
   the DROID mission process dies with it, by design and expected),
2. waits for memory, runs mia-dsv41/start.sh (stock baseline; rsync to worker ~387G,
   image ship, two-rank boot ~25 min),
3. verifies health + a 17*19=323 completion probe,
4. writes /srv/logs/dsv41-baseline-READY or -FAILED; full log /srv/logs/dsv41-first-launch.log.
Everything else survives: SSH (session 3), Tailscale, systemd user services
(orchestrator :18900, ts-proxy :4000), LiteLLM containers, all user app containers
(temporarily stopped: gigpilot, financialos, pageflo, open-webui, gx-computer — docker start them after).
GUI restoration: console login at gdm (or reboot; model must be stopped first).
RESUME THE MISSION once the marker appears (or after ~40 min regardless — check the
log tail). Next steps on resume: Phase 16-17 tests + benchmark, then Phase 18-20.


## 2026-09-27 17:55 — Gate A in flight (fast-track mode)
- Headless transition DONE (wayland session 4 terminated; MemAvailable 116.1G passed
  the 111.5G preflight). Headless is now the standing state; GUI restore = console
  login at gdm (or reboot after stopping the model).
- Baseline launch RUNNING detached: log /srv/logs/dsv41-first-launch.log; markers
  /srv/logs/dsv41-baseline-READY or -FAILED. Timeline: image built+shipped OK (SM121
  ext compile), EXL3 pack rsync to worker ~113MB/s (ETA ~18 min from 17:55), then
  engram 190G sync (~28 min), then two-rank boot (~25 min). READY expected ~19:05.
- ON READY: Gate A verification = ops/bench/run_bench.py --suite quick (TTFT +
  decode tok/s, direct :8888), then stop via mia-dsv41/stop.sh (verify memory return
  both nodes), then restart via systemctl --user start gx-dsv41-bootstrap.service
  (or ./start.sh) to prove clean restart. Record evidence in EVIDENCE.md.
- ON FAILED: diagnose from the log tail (rsync/rank boot/OOM), fix, re-dispatch via
  systemctl --user start gx-dsv41-bootstrap.service (unit wraps the same script,
  NOT enabled at boot).
- Then P1: stock uncensor battery (ops/uncensor-verify/verify_uncensored.py) →
  delete stock pack (head disk 94%) → download dealignai CRACK 210G to
  /srv/models/dsv41/uncensored → update registry path/env → load → battery compare →
  production select (see DECISIONS.md 2026-09-27 entries).
- B-M1 (drowzeys overlay gate): still open, needs user click; dealignai is the path.
- Stopped user-app containers (restore after model work settles): gigpilot x5,
  financialos x3, pageflo x2, open-webui, gx-computer.
- All 6 workers green; e2e legacy retired; restore-normal.sh V4.1 (38 tests OK);
  dashboard backend live on :8088; orchestrator (new code) live on :18900 with
  scheduler; LiteLLM still running OLD config (recreate with new compose at P3).
