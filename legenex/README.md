# Legenex Dual GX10 Cluster

Start with `../CLAUDE.md`, `../CURRENT_STATE.md` and `../ARCHITECTURE.md`.
Eight public aliases (L-10, D-036).

Node 01 (gx10-01):
- LiteLLM gateway :4000 (gx-mini, gx-fast, gx-reason, gx-max, gx-auto, gx-image, gx-video)
- llama-swap node01, gx-mini, gx-fast
- orchestrator :18900 (gx-auto, gx-max lifecycle, resource guard)
- Control Center :8088 (`control-ui/`)
- GX-Playground :8090 (`playground/`), with the gx-music API `/v1/music/*`

Node 02 (gx10-02):
- gx-reason (llama-swap node02, vLLM, on demand)
- media router 2.3 + ComfyUI (gx-image, gx-video) (`media/`)
- gx-music supervisor + ACE-Step 1.5 XL engine (`music/`)

Dual node:
- gx-max: DeepSeek V4 Flash (CRACK NVFP4), SGLang TP=2 over ConnectX-7 / NCCL (`lifecycle/`)
