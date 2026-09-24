# HANDOFF

**Current state:** Dual GX10 text/coding/agent cluster is ready for daily use. Post-reboot persistence is proven (`who -b` 2026-09-24 22:36 / 22:31 SAST).

**Last verified action:** Post-reboot acceptance 2026-09-24 23:25 SAST. Four OpenWebUI chats, Connections, AgentOS, gateway, worker pool, gx-max dual-worker, restic check, `gx doctor` 13/13.

**Human-only:**

1. Store `restic-password` (and `age.key`) somewhere that is not only these two machines — `docs/RECOVERY-SECRET.md`
2. Optional later: true offsite restic, Hermes VPS reconcile

**Do not:** delete UNKNOWN files, commit secrets, reboot both nodes at once, restore GX-Playground / media compose unless you intend to bring media back.

Open **Connections** in GX Cluster for Kilo Code / OpenWebUI / curl settings. Reveal the gateway key there; do not put it in chat or git.
