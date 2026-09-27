# Mission Evidence — GX10 DeepSeek V4.1 Flash Clean Rebuild

## 2026-09-27 — Recovery asset protection (PHASES 3-5)

gx-cluster Git: branch main == origin/main at autosync 6113144 (2026-09-27
00:44). Tag `pre-deepseek-v41-rebuild-20260927` created + pushed →
github.com/legenex/gx-server (verified push output).

gx-backup Git: was 3 modified package-manifest files (auto-refresh);
committed as `autosync: refresh package manifests...` then tag
`pre-deepseek-v41-rebuild-20260927` created + pushed →
github.com/legenex/gx-backup (5dd38ee..a65852f + new tag).

Manual archives (originals kept in place; copies verified byte-identical):
| File | Size (B) | SHA-256 |
|---|---|---|
| gx-backup.zip | 23,196,389 | 8a79456abde16a81182e8dd715217bbe90698199760252b681675b8be4757221 |
| Server Backup.zip (gx-cluster incl .git) | 150,658,586 | cf75c6607148f2f1dd5018dead19352b88a4a05128d72efd9d6383e56f30330c |

Protected copies: /home/legenex/Documents/Backups/GX/{gx-backup.zip,Server Backup.zip}

Node ground truth (2026-09-27): gx10-01 / = 916G, 697G used, 173G avail,
/srv/models 268G. gx10-02 / = 916G, 499G used, 371G avail, /srv/models 241G,
/srv/ai-stack 6.8G. Kernel 6.17.0-1032-nvidia; driver 580.173.02; GB10.
Tailscale gx10-01 = 100.105.214.61. SSH legenex-02@gx10-02 works.

## 2026-09-27 — PHASE 11: ConnectX/RoCE fabric validation (PASS)

Rails (both 200 Gb/s, link up, zero RX/TX errors or drops):
- rail1: enp1s0f0np0 / HCA rocep1s0f0 — gx10-01 192.168.100.10 ↔ gx10-02 192.168.100.11
- rail2: enP2p1s0f0np0 / HCA roceP2p1s0f0 — gx10-01 192.168.101.10 ↔ gx10-02 192.168.101.11
- RoCE v2 GID index 3 on every active HCA, both nodes (GID tables captured in logs).

Ping both rails: 0% loss, RTT 0.17–0.79 ms.
NCCL 2.30.7+cuda13.0 (host-built ~/nccl), all_gather_perf 2-node over mpirun:
- **CRITICAL: NCCL_IB_GID_INDEX=3 required** — without it QP setup hangs (exactly
  the per-NIC GID pitfall in Mia's README). With GID 3 pinned, QPs connect on both devs.
- NCCL bound: [0]rocep1s0f0:1/RoCE [1]roceP2p1s0f0:1/RoCE on BOTH nodes (proof log
  /srv/logs/fabric-nccl-20260927.log).
- Measured (all_gather, -x NCCL_IB_GID_INDEX=3, NCCL_SOCKET_IFNAME=enP7s7):
  256 MB: algbw 26.2 / busbw 13.1 GB/s; 4 GB: algbw 43.4 / busbw 21.7 GB/s. 0 wrong values.
  (≈174 Gbps aggregate across both 200G rails, staged/pinned memory — no GPUDirect, as expected.)
Bootstrap/management: enP7s7 (10.60.21.37/41), LAN SSH to node2 verified passwordless.
Test harness: /srv/models/fabric-test/wrap.sh + mpirun (SSH alias for 10.60.21.41 → legenex-02 added).
Raw logs: /srv/logs/fabric-nccl-{20260927,bw-20260927}.log

## PHASE 12: headless prerequisites (validated, live test deferred to first gx-max load)
- No sudo → cannot stop gdm/Xorg system units. Reversible approach chosen:
  `loginctl terminate-session <graphical session>` (user may terminate OWN session; keeps
  SSH/Tailscale/systemd user services; gdm shows login screen for restoration).
- DEFERRED live test: this mission harness itself runs inside the desktop session
  (TERM_PROGRAM=vscode, DISPLAY=:1) — terminating the session now would kill the mission.
  Plan: at first real gx-max load, launch via systemd-run/setsid from SSH context, verify SSH
  + Tailscale health, then terminate graphical session; document console-login restoration.

## 2026-09-27 — Uncensoring sources verified (live HF API + docs)
- drowzeys overlay: gated-auto, NOT granted to token account → BLOCKER B-M1.
  Apply procedure (hardlink splice of attn.wo_b L10-35, 104 tensors) captured
  in MIA-RUNTIME.md; would need GPU_MEM_UTIL=0.85 + VLLM_SPARSE_INDEXER_MAX_LOGITS_MB=256.
- dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw: sha 8a27b35fc5b145fa05ee965c7d7b243b047915f7,
  UNGATED, MIT, 39 shards, ~197-210 GiB, multimodal, drop-in for the Mia pack
  (README verified: same ENGRAM_DIR source, same image, validated 2× GB10).
  HarmBench-320 ASR 99.4% (vs base 21-36%), MMLU non-ethics −0.58pp, DSpark ~45%
  acceptance, vision preserved. Serving notes: MAX_MODEL_LEN 262144 validated,
  KV pool 1GiB, GPU_MEM_UTIL 0.85, MAX_NUM_BATCHED_TOKENS 2048,
  VLLM_SPARSE_INDEXER_MAX_LOGITS_MB=256 REQUIRED.

## 2026-09-27 — Hostinger KVM4 (srv1906439) live verification (read-only SSH)
- Access: ssh hermes-vps (hermes@191.215.40.202, key agentos_vps) — BatchMode OK.
- REALITY vs mission brief: Ubuntu 24.04.4, **2 vCPU, 7.8 Gi RAM, 96 GB root with
  84G used (13G free, 88%)** — NOT 4 vCPU/15GiB/193G. Disk is the constraint.
- Tailscale IS installed + active: 100.70.255.106 (docs were right; mission brief stale).
- Running: hermes-dashboard (127.0.0.1:3001 node app), hermes-gateway (0.0.0.0:9119,
  127.0.0.1:8642), postgres on 127.0.0.1:5432 + :5433, ports 80/443 served (root-managed,
  no nginx access as hermes), buzz relay. DashFlo: dashflo-update.service FAILED user unit;
  no DashFlo container visible as hermes (docker access inconclusive).
- Deploy implications: control-plane stack must be small (13G free), avoid 3001/9119/8642/
  5432/5433/80/443; hermes account only (root = human-gated break-glass).
- DashFlo untouched per mission §41; user removes it separately.
