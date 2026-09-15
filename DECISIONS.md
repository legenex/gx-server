# Decisions

This file is a short index. The full, append-only, evidence-backed decision
log (D-001 through the latest) lives in **`coordination/DECISIONS.md`** — that
is the canonical record and is what `ARCHITECTURE.md` and `BLOCKERS.md`
reference by ID. Do not start a second numbered sequence here.

## Locked decisions (require a human to change)

See `ARCHITECTURE.md` §1 (`L-1` through `L-9`) and `CLAUDE.md`'s LOCKED table
(`L-1` through `L-10`) for the authoritative, currently-in-force list — kept
there rather than duplicated a third time here, since those are the files
every agent is required to read first. Summary of what "locked" means:

- Two separate 128 GB nodes; never described as one 256 GB pool.
- Node roles fixed: gx10-01 = control/gateway/lifecycle/gx-mini/gx-fast;
  gx10-02 = gx-reason/media/rank 1.
- Tailscale = management only. ConnectX/RoCE = all model and NCCL traffic.
- Kernel pinned to `6.17.0-1032-nvidia` on both nodes. Never 7.0.
- No GPUDirect RDMA / `nvidia-peermem` / GDRCopy hacks.
- `gx-max` = SGLang, TP=2, two nodes, `nvidia/DeepSeek-V4-Flash-0731-NVFP4`.
  Never vLLM, never a different model, never a silent downgrade.
- Stack is LiteLLM + llama-swap + llama.cpp + vLLM + SGLang + ComfyUI. Not
  Ollama.
- Gateway exposes exactly seven aliases: `gx-mini`, `gx-fast`, `gx-reason`,
  `gx-max`, `gx-auto`, `gx-image`, `gx-video`. No `gx-vision`.
- `gx-max` is never started at boot.

## Permanently retired — do not resurrect

- **Qwen3.8 / `vllm-qwen38-uncensored`** — retired 2026-09-14 for holding
  ~80 GiB unmanaged, outside the seven-alias tier set, with no lifecycle
  control. See `coordination/DECISIONS.md` D-015 and `MODELS.md`.
- **`Alibaba/Qwen3.5-35B-A3B-Uncensored-HauhauCS-*`** as an upstream model
  ID — it is a local folder path in older recipes, not a real HuggingFace
  repo (returns HTTP 401). See `CLAUDE.md`.

## How to record a new decision

Append to `coordination/DECISIONS.md` with the next `D-0NN` number: what was
decided, why, and what evidence supported it (a test result, a measurement,
a log line — not a guess). If it changes anything in the locked list above,
it needs explicit human sign-off first — see `coordination/BLOCKERS.md` for
how open questions like that are tracked in the meantime.
