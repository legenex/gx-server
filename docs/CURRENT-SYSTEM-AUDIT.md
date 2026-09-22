# Current system audit

Observed 2026-09-23 from gx10-01. Machine state is authoritative. Earlier gx-cluster documentation is treated as a hint.

## GX10-01 (orchestration node — this session)

| Field | Observed |
|---|---|
| Hostname | `gx10-01` (`/etc/hosts` also has `gx10-6ad5`) |
| OS | Ubuntu 24.04.5 LTS (noble) |
| Kernel | `6.17.0-1032-nvidia` aarch64 |
| CPU | 20 cores: 10× Cortex-X925 @ 3900 MHz + 10× Cortex-A725 @ 2808 MHz |
| GPU | NVIDIA GB10 (Blackwell, RTX), persistence on, GSP 580.173.02, VBIOS 9A.0B.25.00.00, part 2E12-275-A1, UUID GPU-d059351e-85cb-3845-4936-cbd160b2fd92 |
| Driver / CUDA | NVIDIA 580.173.02, CUDA 13.0 (nvcc V13.0.88) |
| RAM | 121 GiB unified (`MemTotal` 127535336 kB), swap 63 GiB |
| Disk | NVMe `nvme0n1` 931.5 G ESL01TBTLCZ-27J2-TYN, p1 EFI 512M, p2 ext4 `/` 931 G. Use: 441 G / 916 G (51%), 429 G free |
| User | `legenex` uid 1000, groups: adm, sudo, docker |

### Network

| Iface | Address | Notes |
|---|---|---|
| enP7s7 | 10.60.21.37/24 | LAN, default route via 10.60.21.1 |
| enp1s0f0np0 | 192.168.100.10/24 | ConnectX-7 rail A, **200000 Mb/s**, RDMA `rocep1s0f0` ACTIVE |
| enP2p1s0f0np0 | 192.168.101.10/24 | ConnectX-7 rail B, **200000 Mb/s**, RDMA `roceP2p1s0f0` ACTIVE |
| tailscale0 | 100.105.214.61 | Tailscale, MagicDNS `taila7ef6a.ts.net` |
| docker bridges | 172.17–172.26 | several compose networks |

Ping to gx10-02 over both rails: 0.14–0.21 ms, 0% loss.

SSH config (`~/.ssh/config`): `gx10-02` → 100.73.238.4 user `legenex-02` key `~/.ssh/id_ed25519_gxcluster`. Also `10.60.21.41` same user/key. LAN IP of gx10-02 is 10.60.21.41.

Firewall: `sudo` requires a password in this session; UFW/iptables not enumerated as root.

### Software

- Docker Engine 29.2.1, Compose v5.0.2, Buildx v0.31.1. Root `/var/lib/docker`. CDI NVIDIA GPU.
- Python 3.12.3, Node v22.23.2 (nvm), pnpm, yarn
- User venvs: `~/.venvs/{gx-img-eval,hf-download,huggingface}`
- Syncthing user service (Nick Wiki, Legenex Wiki)
- `gh` authenticated as `legenex` (repo, workflow, gist, read:org)

### Listening services (selected)

3000 OpenWebUI, 4000 LiteLLM, 4173 python, 4180/5180 node, 5432 postgres (pageflo), 6379 redis, 8080/8081 node on Tailscale, 8088 Control UI, 8090 Playground, 8443 Playground HTTPS, 8384 Syncthing, 18900 orchestrator, 19001 llama-swap gx-mini publish, 28080 llama-swap, 15432 litellm postgres, 3389/3390 GNOME remote desktop.

### Docker (running)

| Name | Image | Role |
|---|---|---|
| gx-mini | legenex/llama-cpp-spark:latest | llama.cpp Qwen3.5-4B Uncensored Q4_K_M, alias gx-mini, ctx 131072, port 19001 inside netns |
| gx-fast | jstarkg/vllm-gb10-flashnext:0.28-sm121-r6 | vLLM Qwen3.6-35B-A3B-Uncensored-NVFP4, gpu-util 0.34, max-len 131072 |
| gx-litellm | ghcr.io/berriai/litellm:main-stable | OpenAI-compatible gateway :4000 |
| gx-litellm-db | postgres:16-alpine | LiteLLM postgres :15432 |
| gx-llama-swap-node01 | gx-llama-swap:latest | model lifecycle |
| open-webui | ghcr.io/open-webui/open-webui:main | host network :3000, volume `open-webui` |
| pageflo-gx10-preview-postgres-1 / redis-1 | postgres:16 / redis:7 | PageFlo preview |
| financialos-app/worker/db | financialos + postgres:17 | FinancialOS |

Stopped: datascraper, reachinbox-mcp, financialos-test.

GPU processes: VLLM::EngineCore ~41 GiB, llama-server ~207 MiB.

### User systemd (enabled/running)

gx-control-ui, gx-orchestrator, gx-playground, gx-git-watch, agentos-control-center, agentos-supervisor, syncthing, legenex-wiki-dashboard, nick-wiki-dashboard. Timers: gx-git-autosync, gx-git-daily-audit, gx-hostwatch, wiki rebuilds.

### Models on disk (`/srv/models`)

- gguf: Qwen3.5-4B (3.2 G) and HauhauCS Uncensored Aggressive (current gx-mini)
- vllm: Qwen3.6-35B-A3B-Uncensored-NVFP4 21 G, Qwen3.8-27B-Uncensored-NVFP4 27 G, Qwen3.8-27B-DFlash2 4.1 G
- deepseek: DeepSeek-V4-Flash-0731-CRACK 156 G
- Total ~214 G on node 1

### Home layout (messy)

`/home` has `legenex` and `odin-agent` only. **`/home/Legenex` does not exist.**

`/home/legenex` contains AgentOS copies (`agentos`, `agentos-manual`, `agentos-local-preserved`, `agentos-pre-git-*`, `agentos-control-center-verify`), `Documents/Projects/{AgentOS,DataScraper,FinancialOS,GrowthOS,gx-backup,PageFlo,ProjectOS,Server/gx-cluster}`, nccl trees, vscode-remote-full-reset (~3.9 G), backups, models stubs, reachinbox-mcp, vllm scripts.

Largest home items: `.vscode-server` 8.9 G, Documents 5.0 G, vscode-remote-full-reset 3.9 G, `.cache` 2.8 G, `.npm` 2.5 G.

### Git repositories

| Path | Remote | Status |
|---|---|---|
| `/home/legenex/agentos` | github.com:legenex/agent-os.git | main ahead 2 |
| `/home/legenex/Documents/Projects/AgentOS` | https://github.com/legenex/agent-os.git | dirty (pageflo yaml, untracked plugin) |
| `/home/legenex/Documents/Projects/Server/gx-cluster` | legenex/gx-server + community upstream | clean vs origin/main |
| PageFlo, FinancialOS, ProjectOS, Obsidian wikis | present | not fully inspected for dirty state in this pass |

Two AgentOS checkouts exist (different inodes). Uncommitted work must be snapshotted before any move.

### Existing cluster product

`/home/legenex/Documents/Projects/Server/gx-cluster` is a large live system (LiteLLM + llama-swap + orchestrator + Control UI + Playground + media/voice/music/live/call). Public aliases historically: gx-mini, gx-fast, gx-reason, gx-max (DeepSeek TP=2), gx-auto, gx-image, gx-video, gx-voice, gx-music.

This GX Backup project **simplifies the operator-facing text modes to four** without destroying media services.

### Existing backups

- `/home/legenex/backups` small tarballs (AgentOS dashboard, OpenWebUI 2026-09-01)
- No restic/borg repo existed before this project
- Syncthing for wikis is not a cluster backup

## GX10-02

| Field | Observed via SSH |
|---|---|
| Hostname | `gx10-02` |
| User | `legenex-02` |
| OS / kernel | Ubuntu 24.04.5 LTS, `6.17.0-1032-nvidia` aarch64 |
| GPU | NVIDIA GB10, driver 580.173.02, 48°C, 0% util at sample |
| RAM | 121 GiB, ~70 GiB used, 50 GiB available, swap 63 GiB (2 G used) |
| Disk | NVMe 931.5 G, `/` 684 G used / 185 G free (**79%**) |
| LAN | 10.60.21.41 |
| RoCE | 192.168.100.11, 192.168.101.11 |
| Tailscale | 100.73.238.4 |

### Docker (running)

- `gx-reason` — vLLM Qwen3.8-27B-Uncensored-NVFP4
- `gx-media-router` — unhealthy at sample, 192.168.100.11:18800
- `gx-llama-swap-node02` — healthy

Host python supervisors: 18820 music, 18830 voice, 18840 call, 18850 live.

### Models on disk

Large: deepseek 156 G, image 71 G, video 58 G, voicechat 42 G, vllm 31 G, shared 31 G, music 28 G, live 19 G, llm 17 G, voice 13 G. **Disk pressure is real.** Unused 122B-class checkpoints were previously noted in gx-cluster docs as reclaim candidates; not deleted in this audit.

Home on gx10-02 is relatively small; Documents/Projects/Server exists. Duplicate Chrome .deb files (~142 M × 4) in `$HOME`.

## OpenWebUI

Compose `/opt/open-webui/compose.yaml`, container `open-webui`, host network port 3000, volume `open-webui`. `OLLAMA_BASE_URL` still set to 11434 (Ollama not the live text path). Persistent data in Docker volume — must dump before config changes.

## AgentOS

Live checkout `/home/legenex/agentos` (ahead of origin) and `/home/legenex/Documents/Projects/AgentOS`. Hermes routing currently targets LiteLLM `http://100.105.214.61:4000/v1` with aliases gx-fast / gx-reason / gx-mini. Control center user service running.

## MVA Progress Tracker

No dedicated repository found. MVA material lives in Obsidian Legenex Wiki and AgentOS `projects/lead-gen`. Sample integration will be documented.

## 200 Gbps / RDMA

Both ConnectX-7 rails up at 200 Gb/s full duplex. RDMA links ACTIVE. Model traffic in LiteLLM config uses 192.168.100.11 (fabric), not Tailscale.

## Secrets observed (not recorded here)

LiteLLM master key, swap key, media/voice keys, UI password, database URL live in the gx-litellm container environment and gateway `.env`. They must stay out of Git. Age public key for this project: `configs/age.pubkey`.

## Implications for GX Backup

1. Take encrypted restic snapshots of both nodes **before** moving home directories.
2. Protect both AgentOS checkouts including uncommitted files.
3. Dump Postgres (LiteLLM, PageFlo, FinancialOS) and the OpenWebUI volume.
4. Pin Ornith GGUF files rather than copying 200 G+ of public weights into every snapshot.
5. gx10-02 has only 185 G free — cross-node copies must exclude `/srv/models`.
6. Do not require sudo; restic/age live in `~/.local/bin`.
