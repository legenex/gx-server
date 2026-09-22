# YouTube walkthrough outline

Teach the dual GX10 cluster without secrets.

1. Two ASUS GX10 boxes, 121 GB unified memory, NVIDIA GB10, Ubuntu 24.04.
2. Two 200 Gb/s ConnectX-7 rails (192.168.100.0/24 and 192.168.101.0/24) plus Tailscale for SSH.
3. gx10-01 is the front door: LiteLLM :4000, OpenWebUI :3000, GX-Cluster :8088.
4. Four modes only: mini, code, auto, max.
5. mini is always on. code is one independent worker per machine. auto routes. max is solver+reviewer, not a third giant model.
6. `gx backup` is the daily verb. Git is the recipe. Restic is the data. Models are pins in `models.lock`.
7. Demo: OpenWebUI picker, GX-Cluster dashboard, `gx status`, `gx doctor`.
8. Disaster: clone gx-backup, restore password from the surviving node, `./install`, `gx restore latest`.
