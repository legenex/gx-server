# GX-Cluster — Open WebUI Computer workspace instructions

You are working on the live two-node GX-Cluster from this workspace (`/projects/gx-cluster` is the real checkout on gx10-01). Inspect first. Preserve architecture. Do not redesign the model stack.

## Nodes

- gx10-01 is control, development, and orchestration (user `legenex`, Tailscale `100.105.214.61`).
- gx10-02 is secondary compute, media, and reasoning (user `legenex-02`, Tailscale `100.73.238.4`, SSH `gx10-02`).

## Networks

- Tailscale is management only.
- ConnectX/RoCE (`192.168.100.x` / `192.168.101.x`) carries distributed model traffic.
- Do not route distributed model traffic over Tailscale.

## Locked runtime

- Keep the pinned NVIDIA kernel `6.17.0-1032-nvidia` on both nodes.
- gx-max remains SGLang TP=2 across both nodes. It does not silently fall back. It does not auto-start at boot.
- Do not resurrect retired models (including Qwen3.8 runtimes and `vllm-qwen38-uncensored`).
- Do not change llama-swap architecture, LiteLLM routing, ConnectX/RoCE, firmware, or the NVIDIA kernel for ordinary work.
- Protect memory headroom. Large models do not auto-start at boot.

## How to work

- Inspect the live system before changing it.
- Work autonomously through implement → test → diagnose → repair → retest.
- A container started is not proof of success. Require actual inference or an equivalent runtime test.
- Do not put secrets in this file, in Git, or in chat logs.
