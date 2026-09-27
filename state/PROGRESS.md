# Mission Progress — GX10 DeepSeek V4.1 Flash Clean Rebuild

Mission start: 2026-09-27. Supersedes prior per-feature progress tracking for
the rebuild. This file is the canonical progress record for the V4.1 mission.

## Completed

- [x] PHASE 1-2: Ground truth established. Both repos intact (gx-cluster →
  github.com/legenex/gx-server; gx-backup → github.com/legenex/gx-backup).
  Kernel 6.17.0-1032-nvidia + driver 580.173.02 confirmed on gx10-01;
  gx10-02 reachable via SSH (Tailscale), same disk layout. No sudo anywhere
  (Docker + `systemctl --user` only). GPU via CDI.
- [x] PHASE 3: gx-backup verified: clean Git tree (3 auto-modified package
  manifests committed), remote OK, install/restore scripts, restic-based
  recipe, docs + recovery PDF present.
- [x] PHASE 4: Manual archives located, checksummed, copied to
  `/home/legenex/Documents/Backups/GX/`, checksums re-verified:
  - `gx-backup.zip` — 23,196,389 B — sha256 `8a79456abde16a81182e8dd715217bbe90698199760252b681675b8be4757221` (original: Documents/Projects/gx-backup.zip, mtime 2026-09-27 14:36)
  - `Server Backup.zip` (full gx-cluster incl. .git) — 150,658,586 B — sha256 `cf75c6607148f2f1dd5018dead19352b88a4a05128d72efd9d6383e56f30330c` (original: Documents/Projects/Server Backup.zip, mtime 2026-09-27 14:30)
- [x] PHASE 5: Pre-V4.1 recovery points created and PUSHED:
  - gx-cluster tag `pre-deepseek-v41-rebuild-20260927` → github.com/legenex/gx-server
  - gx-backup tag `pre-deepseek-v41-rebuild-20260927` → github.com/legenex/gx-backup
- Disk ground truth: gx10-01 697G used / 173G free (/srv/models 268G);
  gx10-02 499G used / 371G free (/srv/models 241G, /srv/ai-stack 6.8G).

## In progress

- [ ] PHASE 6: Full inventory (filesystem, services, models, Docker, storage)
      on both nodes — parallel read-only agents dispatched.

## Next

- PHASE 7-8: Stop + delete obsolete AI stack (after inventory evidence).
- PHASE 9-10: Filesystem normalisation + storage verification.
