# Tasks for the gx10-02 worker

Written by the LEAD agent on gx10-01. Copy of record lives in the repo; a copy is
pushed to `/home/legenex-02/gx-worker/WORKER_TASKS.md`.

Report results in `/home/legenex-02/gx-worker/WORKER_RESULTS.md`.
**Do not write into the git repo on gx10-01.**

---

## Standing rules

1. **Never** stop, kill or restart `gx-max-rank1` unless the lead asks.
2. **Never** upgrade the kernel. It is pinned at `6.17.0-1032-nvidia` because
   kernel 7.0 broke RDMA memory registration. Do not run `apt upgrade`,
   `apt autoremove`, or any firmware update.
3. **Never** change MTU, Netplan, RDMA config, ConnectX firmware, or routing.
4. GPU passthrough on this node is **CDI**: `--device nvidia.com/gpu=all`.
   There is no `nvidia` docker runtime. `--gpus all` will not work.
5. No sudo is available. Use Docker and `systemctl --user`.
6. Do not start large models without checking free memory first; gx-max takes
   the whole node when it is up.

## T-1 — Enable user lingering (blocked, needs the human)
`loginctl show-user legenex-02` reports `Linger=no`, so user services will not
survive logout or reboot on this node. Try `loginctl enable-linger legenex-02`;
if polkit refuses without a password, record it in WORKER_RESULTS.md and stop.

## T-2 — Watch the gx-reason download
A tmux session `gxdl` is fetching
`et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10` (78.8 GB) to
`/srv/models/vllm/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10`, logging to
`/srv/logs/download-gx-reason.log`.

Report: completion, final `du -sh`, and whether all shards are present. If it
dies, restart it with `~/gx-scripts/download-model.sh` (same arguments) and say
so.

## T-3 — Confirm a vLLM image that actually has sm_121a NVFP4 kernels
This is the single biggest technical unknown for gx-reason and gx-fast. The
checkpoint author claims stock vLLM works; another community build insisted a
source build of vLLM 0.23.0 was needed for GB10.

Find out which vLLM container image genuinely carries compiled NVFP4 kernels for
`sm_121a` on aarch64. **Do not download a 30 GB image speculatively** — check
tags/manifests and documentation first, and report what you find before pulling.

## T-4 — ComfyUI feasibility on this node (research + Dockerfile only)
No official ARM64 + sm_121 ComfyUI image exists. The working recipe is a
self-built CUDA 13 image + PyTorch cu130 aarch64 wheels (verified available
through 2.14.0+cu130) + SageAttention built with `TORCH_CUDA_ARCH_LIST="12.1"`.

Write a Dockerfile at `~/gx-worker/comfyui/Dockerfile`. **Do not build it yet**
and do not download model weights yet — gx-max currently owns this node's memory
and node 2 must stay clear.

Known constraints to encode: CUDA ≤12.8 cannot emit sm_121; no PyPI torch; no
x86 base image; `--force-fp16` produces NaNs on Blackwell; avoid `--gpu-only`
and `--highvram`; there is no aarch64 `onnxruntime-gpu` wheel, so ControlNet
preprocessors will silently fall back to CPU.

## T-5 — Report node-2 memory the moment gx-max releases
When the lead releases gx-max, immediately record `free -g` and confirm how much
is genuinely reclaimed. gx-reason needs ~86 GiB and must own this node.
