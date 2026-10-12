# GX-Cluster UI

URL: `http://127.0.0.1:8088` (Tailscale `100.105.214.61:8088`). User systemd `gx-control-ui.service`, enabled.

The active cluster is text / coding / agent only. GX-Playground (`:8090`) and the media stack are retired from active operation (source remains in `legenex/gx-server` and restic).

Primary nav (Playwright 2026-09-24 closeout, user `acceptance`):

- Dashboard — GX10-01 / GX10-02, AUTO/MINI/CODE/MAX, gateway, Tailscale, RoCE, memory, disk, temperature, AgentOS. Overall **Healthy** when the four-mode cluster is up.
- Models — **gx-mini**, **gx-code**, **gx-auto**, **gx-max** only. gx-code shows Ornith 35B Q5_K_M and workers 01/02. gx-max shows dual-worker solver/reviewer.
- Jobs — orchestrator / gx-max dual-worker and Control-UI operations
- Logs — Gateway, nodes, gx-mini, gx-code-01/02, gx-auto, AgentOS-adjacent control plane, OpenWebUI, Backup, GX-Cluster
- Machines (`#/cluster`) — two 121 GiB nodes, RoCE rails
- Backup & Recovery (`#/backup`) — last snapshot, Restic integrity, restore drill, offsite **Not configured**
- Connections (`#/connections`) — live gateway URL, masked/reveal API key, Kilo Code, OpenWebUI, curl/Python, Test Gateway
- Settings — nodes, gateway, four modes, backup, OpenWebUI, logging, health. No playground/media endpoints.

Playwright text snapshots: `state/ui-snaps-20260924/`. Forbidden strings (GX-Playground, Images/Video/Music/Voice, gx-image, gx-fast, gx-reason, Media profile) were absent on all seven primary pages.

Live vs host (same window, 2026-09-24):

| Fact | UI | Host |
|---|---|---|
| Nodes online | gx10-01 + gx10-02 reachable | SSH + Tailscale up |
| Disk gx10-01 | 76% / 211 GiB free | `df` 76% / 212 G |
| Disk gx10-02 | 56% / 378 GiB free | `df` 57% / 379 G after media-weight reclaim |
| Loaded | gx-mini, gx-code, gx-auto, gx-max | llama-swap gx-mini + gx-code both nodes |
| Backup stamp | see LATEST | `~/Documents/Backups/LATEST` |
| OpenWebUI | service 200 | `:3000` 200 |
| Gateway | LiteLLM 200 | `:4000` |
| Overall | Healthy | four-mode cluster; media router not probed |

Backup **Verify** calls `restic check --read-data-subset=2%`. **Back Up Now** starts `backup/restic.sh backup` in a thread. Destructive restore is CLI-only.

Port 8090 is not listening. `gx-playground.service` is masked. gx10-02 `gx-call` / `gx-live` / `gx-music` / `gx-voice` are masked. `gx-media` compose is down.
