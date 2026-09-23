# HANDOFF

**Current state:** Dual GX10 cluster **CLOSED** for this infrastructure project. Recipe https://github.com/legenex/gx-backup (private). Latest restic stamp in `~/Documents/Backups/LATEST`.

**Last verified action:** six-task Ornith suite 5/6 PASS 1 PARTIAL; gx-max dual-worker on a real bug; OpenWebUI four modes; UI Backup Now created restic `20260923T091634Z`.

**Human-only:**

1. Store `restic-password` (and `age.key`) somewhere that is not only these two machines — `docs/RECOVERY-SECRET.md`
2. When you can type a sudo password: reboot gx10-02, then gx10-01, one at a time; `gx doctor`
3. Optional later: true offsite restic, Hermes VPS reconcile, MVA Progress Tracker wiring

**Do not:** delete UNKNOWN files, commit secrets, reboot both nodes at once, or treat chat as the recovery path.
