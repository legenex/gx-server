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
