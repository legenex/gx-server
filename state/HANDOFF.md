# HANDOFF

**Current state:** Dual GX10 text/coding/agent cluster is ready for daily use except a full reboot proof (needs sudo).

**Last verified action:** OpenWebUI chats work on the four modes. Connections page shows live gateway settings. AgentOS Control Center `:4173` answers with gx-code. Public model list is four modes. Restore drill of snapshot `eaf40302` passed.

**Human-only:**

1. Store `restic-password` (and `age.key`) somewhere that is not only these two machines — `docs/RECOVERY-SECRET.md`
2. When you can type a sudo password: reboot gx10-02, then gx10-01, one at a time; `gx doctor`. Confirm 8090 stays down and media units stay masked. Confirm OpenWebUI still chats and `gx-gateway-ts-proxy` binds Tailscale `:4000`.
3. Optional later: true offsite restic, Hermes VPS reconcile

**Do not:** delete UNKNOWN files, commit secrets, reboot both nodes at once, restore GX-Playground / media compose unless you intend to bring media back.

Open **Connections** in GX Cluster for Kilo Code / OpenWebUI / curl settings. Reveal the gateway key there; do not put it in chat or git.
