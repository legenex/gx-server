# HANDOFF

**Current state:** Dual GX10 text/coding/agent cluster is usable. OpenWebUI four-mode chats, Connections, AgentOS, gateway, and backup drill all pass on the current boot.

**Last verified action:** Live check 2026-09-24 21:55 SAST. Last recorded boots remain 2026-09-23 23:56/23:57. Reboot after closeout was not observed.

**Human-only:**

1. Store `restic-password` (and `age.key`) somewhere that is not only these two machines — `docs/RECOVERY-SECRET.md`
2. Reboot gx10-02, then gx10-01, one at a time (sudo). After each node returns: `who -b` should show a new timestamp, then `gx doctor` and one chat per mode.
3. Optional later: true offsite restic, Hermes VPS reconcile

**Do not:** delete UNKNOWN files, commit secrets, reboot both nodes at once, restore GX-Playground / media compose unless you intend to bring media back.

Open **Connections** in GX Cluster for Kilo Code / OpenWebUI / curl settings. Reveal the gateway key there; do not put it in chat or git.
