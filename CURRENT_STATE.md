# Current state

**This file must always reflect reality.** If you are a new agent resuming this
work, read this first, then ARCHITECTURE.md (what is locked), then BLOCKERS.md.

Last updated: 2026-09-14 18:16 CEST, by the lead agent on gx10-01.

---

## One-paragraph summary

gx-max — the two-node SGLang DeepSeek V4 Flash engine — **works and is serving**.
That was the open question at the start of the session, and the pinned
6.17.0-1032 kernel fixed the RDMA failure that kernel 7.0 had caused. The
orchestrator (gx-auto routing + gx-max lifecycle) is built, tested and verified
live. Gateway configs are authored but **not yet running**. gx-fast and gx-reason
weights are downloading. Nothing media-related exists yet.

## Hardware

| | gx10-01 (node 1, control) | gx10-02 (node 2, compute) |
|---|---|---|
| Kernel | `6.17.0-1032-nvidia` | `6.17.0-1032-nvidia` (identical) |
| Arch / Python / Docker | aarch64 / 3.12.3 / 29.2.1 | identical |
| GPU / driver / CUDA | GB10, 580.173.02, CUDA 13.0 | identical |
| RAM | 121 GiB | 121 GiB |
| Swap | 63 GiB (`/swap.img` + `/swapfile-sglang` 48 G) | 63 GiB (same two files) |
| Disk free | ~326 GB of 916 GB | ~541 GB of 916 GB |
| sudo | **password required** | **password required** |
| User lingering | enabled | **disabled** |

GPU passthrough is **CDI** (`--device nvidia.com/gpu=all`) on both nodes. There
is no `nvidia` docker runtime and no `/etc/docker/daemon.json`.

## Fabric

| Rail | node 1 | node 2 | state |
|---|---|---|---|
| A `enp1s0f0np0` / `rocep1s0f0` | 192.168.100.10 | 192.168.100.11 | ACTIVE, 0.211 ms, 0% loss |
| B `enP2p1s0f0np0` / `roceP2p1s0f0` | 192.168.101.10 | 192.168.101.11 | ACTIVE, 0.338 ms, 0% loss |

Both rails carry NCCL traffic (measured 772 MB for one generation, split evenly).
Tailscale is management/SSH only — confirmed by measurement, not assumption.

**SSH note:** `ssh legenex-02@gx10-02` works and routes over Tailscale
(100.73.238.4). SSH directly to `192.168.100.11` is **refused** (publickey) — the
fabric addresses are not set up for SSH. That is fine for management, but the
older `legenex/scripts/gx-max-now.sh` assumes `legenex-02@192.168.100.11` for its
rsync and would fail as written.

## What is running right now

| Service | Where | Port | State |
|---|---|---|---|
| gx-max rank 0 (SGLang) | node 1 | 30000 | **running, healthy, serving** |
| gx-max rank 1 (SGLang) | node 2 | — | **running** |
| gx-orchestrator | node 1 | 18900 (loopback + docker bridge) | **running** |
| LiteLLM gateway | node 1 | 4000 | **not started** — config authored only |
| llama-swap node 1 | node 1 | 28080 | **not started** |
| llama-swap node 2 | node 2 | 28080 | **not started** |
| gx-mini (llama.cpp) | node 1 | 19001 | **stopped** (evicted by gx-max) |
| ComfyUI | node 2 | — | **does not exist yet** |

gx-max currently holds the memory of both nodes (~14 GiB available on node 1,
~18 GiB on node 2). Nothing else of size can run until it is released.

## Tier status

| Alias | Model | Engine | Node | State |
|---|---|---|---|---|
| gx-mini | Qwen3.5-4B Q4_K_M + BF16 mmproj | llama.cpp | 1 | weights present, container stopped |
| gx-fast | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM | 1 | **downloading** |
| gx-reason | `et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10` | vLLM | 2 | **downloading** |
| gx-max | `nvidia/DeepSeek-V4-Flash-0731-NVFP4` | SGLang TP=2 | 1+2 | **WORKING** |
| gx-auto | — | orchestrator | 1 | **working** (logic verified; live tiers pending) |
| gx-image | Qwen-Image 2512 / HiDream I1 | ComfyUI | 2 | model IDs verified; nothing built |
| gx-video | LTX 2.3 / Wan 2.2 A14B | ComfyUI | 2 | model IDs verified; nothing built |

## Repository layout (what this session added)

```
ARCHITECTURE.md          locked decisions, layer separation, request paths
CURRENT_STATE.md         this file
MODELS.md                verified checkpoints, licences, footprints, rationale
TEST_RESULTS.md          what was actually tested, with evidence
coordination/
  LEADER_STATUS.md       what the lead has done / is doing
  WORKER_TASKS.md        tasks for the gx10-02 worker agent
  DECISIONS.md           append-only decision log with rationale
  BLOCKERS.md            what needs a human
legenex/
  lifecycle/             gx-max acquire / release / status (bash)
  orchestrator/          gx-auto router + gx-max lifecycle (python, stdlib only)
  gateway/               LiteLLM + llama-swap configs + compose
  scripts/               model download helper
```

## Known gaps

See BLOCKERS.md for the full list with severities. The two that matter most:

* **B-001** the kernel pin has no `apt-mark hold`, and kernel 7.0 is still
  installed on both nodes. That is the exact kernel that broke gx-max.
* **B-003** SGLang `:30000` is bound `0.0.0.0` with no auth, reachable from the
  LAN and the tailnet.

## How to resume

```bash
cd /home/legenex/Documents/Projects/Server/gx-cluster
./legenex/lifecycle/gx-max-status.sh          # is the big engine up?
curl -s localhost:18900/health/detailed       # orchestrator + tier view
cd legenex/orchestrator && python3 -m unittest discover -s . -p 'test_*.py'
```
