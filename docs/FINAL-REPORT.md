# GX Backup final report

**Date:** 2026-09-23 (SAST)

## Machine status

| Node | Status |
|---|---|
| GX10-01 | Healthy. Ubuntu 24.04.5, kernel 6.17.0-1032-nvidia, GB10, 121 GiB, NVMe 51% used. Tailscale 100.105.214.61. RoCE 200 Gb/s both rails. |
| GX10-02 | Healthy via SSH. Same OS/kernel/GPU class. Disk 79% used (185 G free). Tailscale 100.73.238.4. |

## Discovered

Existing gx-cluster already served LiteLLM, llama-swap, gx-mini (Qwen3.5-4B), gx-fast (Qwen3.6-35B NVFP4), gx-reason (Qwen3.8-27B), media/voice/music, OpenWebUI, AgentOS. `/home/Legenex` does not exist. Two AgentOS checkouts. No MVA Progress Tracker repo.

## Backup

- restic 0.18.1 + age 1.2.1 in `~/.local/bin` (no sudo).
- Latest identifier: `20260922T225812Z`
- gx10-01 snapshot `dd27fe41` (also copied to gx10-02 as `2a96d580`)
- gx10-02 snapshot `01595307` (also copied to gx10-01 as `445c33d1`)
- Integrity: `restic check --read-data-subset=5%` → **no errors**
- Restore tests: `models.lock` sha256 match; `litellm.sql.gz` sha256 match; OpenWebUI tar lists `webui.db-wal`
- Location: `~/Documents/Backups/restic/`
- Timer: `gx-backup.timer` 03:30
- GitHub: private `legenex/gx-backup` (created if `gh` permits)
- Offsite mutable backup: **absent**

## Filesystem

Archived duplicates under `Documents/Archive/pre-cleanup-20260922T230642Z/`. Live AgentOS remains `/home/legenex/agentos`. Projects already under Documents/Projects.

## Models

| Mode | Model | Quant | Runtime | Notes |
|---|---|---|---|---|
| gx-mini | junafinity/Ornith-1.5-9B-uncensored-GGUF-8bit @ 10d5524c | Q8_0 | llama.cpp | sha256 verified; 24g cgroup; ctx 65536 |
| gx-code | 0xKitkat/Ornith-1.5-35B-A3B-Uncensored-GGUF @ ab0eed77 | Q5_K_M | llama.cpp (cutover) | LiteLLM currently load-balances gx-fast + gx-reason until Ornith workers are resident |
| gx-auto | router | — | orchestrator | coding stays on gx-code when busy |
| gx-max | dual-worker | — | orchestrator | solver+reviewer; DeepSeek optional |

Gateway: `http://127.0.0.1:4000/v1` aliases include gx-auto, gx-mini, gx-code, gx-max.

GX-Cluster: `http://127.0.0.1:8088`

OpenWebUI: `http://127.0.0.1:3000`

AgentOS config: `/home/legenex/agentos/hermes/model-routing.yaml`

## Commands

```
gx status
gx backup
gx doctor
gx models
gx auto
gx mini
gx code
gx max
gx logs
gx benchmark
```

## Limitations / blockers

- Passwordless sudo unavailable: no apt, no /etc edits, no reboot from this session.
- Fully independent offsite mutable backup absent.
- MVA Progress Tracker not found; sample routing documented only.
- Hermes VPS reconcile not applied remotely.
- Ornith gx-mini first start OOM’d at 14g; limit raised to 24g. Now resident and answering `Pong` in ~1s.
- gx-code Ornith Q5_K_M resident on gx10-01 (`is_palindrome` in 1.51s, 21 tokens). gx10-02 `gx-code-02` still uses live gx-reason until that node’s llama.cpp worker is started.
- Live measurements: gx-mini ~11 t/s counting 1–20; gx-code-01 ~53 t/s on a short function after thinking disabled; gx-max dual-worker 7.3s for `17*19=323`.
