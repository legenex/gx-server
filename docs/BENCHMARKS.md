# Benchmarks

Measured 2026-09-23 unless noted. Quality over peak tok/s.

## gx-mini (Ornith-1.5-9B Q8_0, llama.cpp)

- Files sha256-verified against Hugging Face LFS.
- First start: previous 14g cgroup OOM (exit 137) with ctx 131072. Fixed: 24g, ctx 65536.
- Historical Qwen3.5-4B Q4_K_M on same runtime: ~52 tok/s (gx-cluster TEST_RESULTS). Ornith numbers to be recorded after the replacement becomes healthy.

## gx-code

- Target: `0xKitkat/Ornith-1.5-35B-A3B-Uncensored-GGUF` Q5_K_M (25.3 GB) sha256 `6580229b…b061`.
- Q4_K_M kept as speed fallback (21.7 GB).
- During cutover LiteLLM `gx-code` load-balances live `gx-fast` (vLLM Qwen3.6-35B NVFP4, historically ~55 tok/s) and `gx-reason` (vLLM Qwen3.8-27B NVFP4, historically ~9 tok/s).

## gx-auto

Classifier unit-checked in-process:

- “implement a python API endpoint for login” → `gx-code`
- “hi” → `gx-mini`
- coding request while gx-code busy → stays on `gx-code` (queue), does not fall to mini

## gx-max

Dual-worker workflow in orchestrator (`GX_MAX_MODE=dual-worker`). Occupies both code workers. DeepSeek TP=2 not started in this pass.
