# HANDOFF

**Current state:** Dual GX10 **text/coding/agent** cluster. Recipe https://github.com/legenex/gx-backup (private). UI source https://github.com/legenex/gx-server (autosync). Latest restic stamp in `~/Documents/Backups/LATEST`.

**Last verified action:** GX-Playground and media stack backed up (restic `24ba5393`) then removed from active operation. Four modes still pass live pings. GX-Cluster overall Healthy. gx doctor 13/13.

**Human-only:**

1. Store `restic-password` (and `age.key`) somewhere that is not only these two machines — `docs/RECOVERY-SECRET.md`
2. When you can type a sudo password: reboot gx10-02, then gx10-01, one at a time; `gx doctor`. Confirm 8090 stays down and media units stay masked.
3. Optional later: true offsite restic, Hermes VPS reconcile, restore AgentOS live tree (`/home/legenex/agentos` is missing; checkout is `Documents/Projects/AgentOS`)

**Do not:** delete UNKNOWN files, commit secrets, reboot both nodes at once, treat chat as the recovery path, or re-enable GX-Playground / media compose unless you intend to bring media back.

**Revive media later:** unmask units from git (`legenex/*/systemd`), `docker compose -f legenex/media/docker-compose.media.yml up` on gx10-02, restore weights via original download scripts. Library is still at `/srv/projects/gx-cluster/media` and in restic.
