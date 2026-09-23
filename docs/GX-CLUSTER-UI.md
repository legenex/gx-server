# GX-Cluster UI

URL: `http://127.0.0.1:8088` (Tailscale `100.105.214.61:8088`). User systemd `gx-control-ui.service`, enabled.

Primary nav (Playwright 2026-09-23, user `acceptance`):

- Dashboard — two machine cards, AUTO/MINI/CODE/MAX chips, storage free **192.3 GiB / 78%** on gx10-01 (matches `df` 193 G / 78%)
- Models — includes **gx-code loaded** plus gx-mini/gx-auto; gx-max unloaded (dual-worker, not SGLang)
- Jobs — orchestrator lifecycle, not simulated
- Logs — predefined streams, redacted
- Machines (`#/cluster`) — two 121 GiB nodes, RoCE explanation
- Backup & Recovery (`#/backup`) — last snapshot **20260923T091634Z**, offsite “absent”, Back Up Now / Verify
- Settings — project 0.19.0, kernel pin 6.17.0-1032-nvidia on both nodes, git `legenex/gx-server`

Live vs host (same window):

| Fact | UI | Host |
|---|---|---|
| Nodes online | gx10-01 + gx10-02 reachable | SSH + Tailscale up |
| Memory | ~122 GiB cards | `free` 121 GiB; available ~60 GiB / ~83 GiB |
| Disk gx10-01 | 78% / 192 GiB free | `df` 78% / 193 G |
| Temp | GPU ~52 °C / ~47–50 °C | `nvidia-smi` 52 / 47–50 |
| Loaded | gx-mini, gx-code, gx-auto (gx-fast/gx-reason aliased to gx-code) | docker gx-mini + gx-code both nodes |
| Backup stamp | 20260923T091634Z | `~/Documents/Backups/LATEST` |
| OpenWebUI | service 200 | `:3000` 200 |
| AgentOS | `:4173` up | health JSON llm gx-code |
| Gateway | LiteLLM 200 | `:4000` |
| Overall | warn | media router gx10-02 HTTP 503 (media stack, not text modes) |

Backup **Verify** calls `restic check --read-data-subset=2%` (HTTP 200, “no errors were found”). **Back Up Now** starts `backup/restic.sh backup` in a thread. Destructive restore is CLI-only.

Extra pages (Resource Control, Storage, Docs) remain secondary. Playground stays at :8090.
