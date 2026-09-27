# MiaAI DeepSeek V4.1 Flash EXL3 runtime — pinned facts (research 2026-09-27)

Source: https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-EXL3-2x-DGX-Sparks
Pinned commit: **6f7d1590ad49a2b8995188e45d7b9db31e677452** (main, 2026-09-19, verified GPG, merge PR #21)
License: AGPL-3.0 (launcher/overlay) + MIT for LICENSE.MIT files. Model weights MIT (DeepSeek). PRESERVE ATTRIBUTION.

## Architecture
- vLLM-based: base image `vllm/vllm-openai:deepseekv41-flash-0909` (linux/arm64, vLLM 0.1.dev20904+g179dd0fa9) + EXL3 overlay (ExLlamaV3 v1.4.5, e648f1a1). Prebuilt public image: `ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks:2.9bpw` (~9 GiB pull, no compile if recipe stamp matches).
- OpenAI-compatible API on :8888. Served model id: `DeepSeek-v4.1-Flash-EXL3`.
- TP=2 over ConnectX-7, nnodes=2, mp backend, master port 29521. DGX Spark: no GPUDirect RDMA; staged shared/pinned memory expected (matches mission §3).
- Native sm_121a cubins.

## Weights (total ~387 GiB, resumable downloads)
| Source | Into | Size |
|---|---|---:|
| Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw (39 EXL3 shards) | MODEL_HOST ./model | ~197 GiB |
| deepseek-ai/DeepSeek-V4.1-Flash — shards 47+48, index, config.json ONLY (Engram tables) | ENGRAM_DIR ./engram-src | ~190 GiB |

- Engram tables NEVER quantized, never copied into EXL3 tree. FP8 e4m3fn rows; reading as uint8 = garbage (test_engram_dequant guards).
- `--hf-overrides '{"engram_table_dir":"/engram-src"}'`.
- `./download.sh` stages without booting. Needs `huggingface_hub[hf_transfer]`. HF token exists at ~/.cache/huggingface/token (verified 2026-09-27).
- Worker weight sync: default nfs (needs NFSv4 export — NOT currently running on gx10-01, and no sudo); `WEIGHT_SYNC=rsync` = plain node-local copy (no sudo, fits worker disk after cleanup); `zfs` needs sudo → impossible.
- `./start.sh pack`: Engram rows onto each node's NVMe, ~94 GiB/node, +25–50% prefill. Optional.

## Memory envelope (per node, GiB)
EXL3 weights/rank 99.5 + KV pool 2.5 (pinned via KV_CACHE_MEMORY_BYTES) + context/NCCL/cudagraphs ~5-7 + vLLM/OS/docker/desktop ~9. Head MemAvailable after 34k agent replay ≈ 5.9. Long-prefill floor: 2.1 GiB at 601k prompt (older 1024 chunk). Every 0.5 GiB KV pool ≈ 1 GiB prefill headroom cost. `--oom-score-adj 1000` on both containers. memguard.sh ships DISABLED (DSV41_MEM_GUARD=0) — sustained use says not needed; cap host jobs with systemd-run MemoryMax instead. Shipped defaults: MAX_MODEL_LEN=600000, MAX_NUM_SEQS=2, MAX_NUM_BATCHED_TOKENS=1536, DSV41_IO_THREADS=96, KV pool 2.5 GiB, PREFIX_CACHE_RETENTION_INTERVAL=4096.

## Speculation (DSpark)
- Draft experts in checkpoint (mtp.*, dspark_block_size=5, 128 draft experts/top-3). No separate drafter.
- `--speculative-config '{"method":"dspark","num_speculative_tokens":3}'`; k=3 (DSPARK_TOKENS) measured fastest on prose.
- SPEC_METHOD=none frees ~3.5 GiB; ×1 stream faster WITH speculation (31.6 vs 23 tok/s), ×4 aggregate faster WITHOUT (53.7 vs 42.8).

## Performance reference (2× GB10, TP=2, DSpark k=3, stock)
- Decode: ×1 31.6 tok/s (TTFT 221 ms), ×2 42.5 aggregate (21.6/stream, TTFT 347 ms), ×4 42.8 aggregate.
- Prefill: ~970–1055 tok/s (8k–128k), 872.6 tok/s at 256k. 995 tok/s median on 34,357-token agent replay.
- Cooperative MoE optional overlay: decode +25–35% (C1 31.45→40.23, C2 45.87→61.06 tok/s), binary pin sha256 a09a589cbdcecb5372991c7b091d732236d58bc5f5aea14ab91e38e426f08d78. OFF by default; evaluate later.

## Vision
Supported and verified by Mia (OCR 10/10, multi-image attribution, image behind 200k prefix at 1017 tok/s). LANGUAGE_MODEL_ONLY=0 default, MAX_NUM_BATCHED_TOKENS >= 1536 REQUIRED (chunk floor 1025). 128-wide in-image window clamp on SM12x (bidirectional visibility off) — fine for OCR; caveat on dense doc/spatial work.

## Reasoning + tools + template
- chat_template_kwargs: enable_thinking (default true), reasoning_effort "low"=50 / "high"=75 / "max"=100 or int 1–100 (default high).
- Parsers: --tokenizer-mode deepseek_v41 --tool-call-parser deepseek_v41 --reasoning-parser deepseek_v41.
- Official sampling: temperature=1.0, top_p=0.95. Smokes: enable_thinking=false.
- `--enable-prompt-tokens-details` for cached-token fields.

## CX7 notes from Mia
Pins: spark1 enp1s0f1np1/rocep1s0f1 ↔ spark2 enp1s0f0np0/rocep1s0f0. NCCL cannot use 10.0.0.x loopback aliases. GID index PER-NIC — all-zero entry dies ~60s in with ibv_modify_qp errno 61. Preflight checks each rank.

## Uncensoring (mission §9) — documented overlay path IN the Mia README
1. Sidecar (~650 MB, gated auto-approval): drowzeys/DeepSeek-V4.1-Flash-Abliterated-Cybersecurity-Unleashed — file **mia_exl3_wo_b_l10_35.safetensors** (NOT wo_b_l10_35.safetensors = FP8 for other packs). Replaces layers.10–35 attn.wo_b (EXL3 mul1 K=5) on a COPY of the 2.9bpw pack. L0–9/L36–39/MTP/Engram/experts stay stock.
2. Apply helper: drowzeys/keys-DeepSeek-V4.1-Flash-Abliterated-Mia-2x-Spark-EXL3 (GitHub).
3. Set MODEL_HOST to applied dest; ENGRAM_DIR stays native 47+48.
- Verified metadata (HF API 2026-09-27): sidecar sha 87bb9850e6f49fd20ad9c8516b7817215cbbf5fc, repo MIT, gated:auto, ~1.77 GB total storage (both safetensors + docs).
- Full alternative checkpoint: dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw — MIT, sha 8a27b35fc5b145fa05ee965c7d7b243b047915f7, 39 shards, 105.2B params (mostly EXL3 I16), usedStorage 210.67 GB, multimodal, deepseek_v41 arch. Would still need Engram 47+48 source. Evaluate only if overlay insufficient.

## Lifecycle
`./start.sh [start|stop|restart|status|logs [worker]|share|pack]`. Boot ≈ 25 min. First run downloads missing weights, pulls image, ships to worker (rsync staged tar by default). Existing .env NOT rewritten on upgrade. start.sh has boot-margin preflight, per-prefill allocator release, post-load page-cache drop, hang detector (py-spy after 420s quiet boot). Head = rank 0, worker = rank 1 via SSH.

## Launch env knobs (from commit 6f7d159)
SERVED_MODEL_NAME, PORT(8888), TP=2, NNODES=2, HEAD_IP, MASTER_PORT(29521), QUANTIZATION, MAX_MODEL_LEN=600000, GPU_MEM_UTIL=0.88, MAX_NUM_SEQS=2, MAX_NUM_BATCHED_TOKENS=1536, LONG_PREFILL_TOKEN_THRESHOLD, KV_CACHE_DTYPE (DO NOT SET — fp8_ds_mla auto), SPEC_METHOD, DSPARK_TOKENS, PREFIX_CACHE_RETENTION_INTERVAL=4096, LANGUAGE_MODEL_ONLY, DSV41_IO_THREADS=96, KV_CACHE_MEMORY_BYTES=2684354560, KV_BLOCK_SIZE=64, WORKER_USER/GID, AUTO_DOWNLOAD, WEIGHT_SYNC, IMAGE_SHIP, ENGRAM-related DSV41_* vars.

## Host-side tests (no torch) to run post-clone
python3 tests/test_numeric_config.py test_engram_src.py test_responses_content_types.py test_engram_secondary.py test_k_map.py test_memory_log.py test_exl3_lm_head.py test_sm120_block64.py test_h2d_stage.py; scripts/weight_budget.py --tp 2. Post-serve: bash tests/test_smoke.sh (17*19 → 323).
