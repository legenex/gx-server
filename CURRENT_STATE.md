# Current state

**This file must always reflect reality.** If you are a new agent resuming this
work, read this first, then ARCHITECTURE.md (what is locked), then BLOCKERS.md.

Last updated: 2026-09-14 20:20 CEST, by the lead agent on gx10-01.

---

## One-paragraph summary

**Four of the seven aliases work end-to-end through one endpoint.** gx-mini, gx-fast,
gx-max and gx-auto are verified serving real inference through the LiteLLM
gateway on `127.0.0.1:4000` (and over Tailscale), and the full gx-max lifecycle
— drain, acquire both nodes, serve, release, restore — passes. gx-reason is
**not working**: the 122B model loads and generates but returns garbage tokens
(B-011). gx-image/gx-video are **not built**. **Node 2 is currently wedged**
(B-012) and needs attention before gx-reason or media work can continue.

## ⚠ Immediate issue: node 2 is wedged

Node 2's kernel is alive — ICMP on `192.168.100.11` replies with 0% loss and
sub-millisecond RTT — but **userspace is starved**: SSH hangs over both
Tailscale and the fabric, its llama-swap does not answer, and Tailscale reports
it offline.

Cause: two 77 GB models were resident at once (gx-reason plus a diagnostic
container), which put a 121 GiB node into sustained mmap thrashing. Because
mmap pages are reclaimable the OOM killer does not necessarily fire, so it can
thrash rather than shed load. See BLOCKERS.md B-012.

**If it has not recovered on its own, node 2 needs a power cycle.** Node 1 is
completely unaffected and continues to serve gx-mini, gx-fast and gx-auto.

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
| LiteLLM gateway | node 1 | 4000 (loopback + tailnet) | **running, healthy** |
| gx-orchestrator | node 1 | 18900 (loopback + docker bridge) | **running, healthy** |
| llama-swap | node 1 | 28080 | **running, healthy** |
| Postgres (LiteLLM) | node 1 | 15432 | running |
| gx-mini (llama.cpp) | node 1 | 19001 | loaded |
| gx-fast (vLLM) | node 1 | via llama-swap | loaded |
| gx-max rank 0/1 (SGLang) | 1+2 | 30000 | **stopped** — released after the lifecycle test |
| llama-swap | node 2 | 28080 | **unreachable** — node 2 wedged |
| gx-reason (llama.cpp) | node 2 | via llama-swap | **unreachable / broken output** |
| ComfyUI | node 2 | — | **does not exist yet** |

Node 1 has ~80 GiB available with gx-mini and gx-fast both loaded. Node 2's
state is unknown beyond kernel liveness.

## Tier status

| Alias | Model | Engine | Node | State |
|---|---|---|---|---|
| gx-mini | Qwen3.5-4B Q4_K_M + BF16 mmproj | llama.cpp | 1 | **WORKING** — 50.6 tok/s, vision verified |
| gx-fast | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM | 1 | **WORKING** — 72.8 tok/s, tools + vision verified |
| gx-reason | `unsloth/Qwen3.5-122B-A10B-GGUF` UD-Q4_K_XL | llama.cpp | 2 | **BROKEN** — loads and generates but output is garbage (B-011) |
| gx-max | `nvidia/DeepSeek-V4-Flash-0731-NVFP4` | SGLang TP=2 | 1+2 | **WORKING** |
| gx-auto | — | orchestrator | 1 | **WORKING** — verified routing across live tiers |
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
