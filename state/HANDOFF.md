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
