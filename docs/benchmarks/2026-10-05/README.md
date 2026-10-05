# DeepSeek V4.1 Flash EXL3: baseline benchmark and upstream review (2026-10-05)

**Status:** baseline recorded; **no runtime change applied**. The A/B of
candidate #1 is ready but was not run: restarting the live engine needs owner
approval in the operator session.

## Live configuration (verified 2026-10-05)

| Item | Value |
|---|---|
| Served id | `DeepSeek-v4.1-Flash-EXL3` (gx-max / gx-auto → orchestrator :18900 → vLLM :8888) |
| Weights | dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw (EXL3 v1.4.2, rev 8a27b35), `/srv/models/dsv41/uncensored`, 197 GiB |
| Image | `ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks:2.9bpw` (local ID sha256:a80eafcd…, built 2026-09-27; no registry digest) |
| Runtime | vLLM 0.1.dev20904+g179dd0fa9 (DSV4.1 branch) + Mia EXL3 overlay; exllamav3 1.4.5; V2 model runner |
| CUDA / torch / NCCL | CUDA 13.0.88 (driver 580.173.02); torch 2.13.0+cu130; NCCL 2.30.7 |
| Attention / KV | DSV4 sparse MLA via FlashInfer SM120; `fp8_ds_mla` KV, block 64; KV pinned 2.5 GiB (773k tokens) |
| Parallelism | TP=2, 2 nodes, mp backend; NCCL over RoCEv2 on rail 1 only (`NCCL_IB_HCA=rocep1s0f0`) |
| Scheduler | max-num-seqs 2, max-num-batched-tokens 2048, chunked prefill, prefix caching (lifetime hit rate ~83%) |
| CUDA graphs | on (piecewise 1–24 + full); DSpark MTP speculative decoding k=3 (mean acceptance ~2.2) |
| EXL3 MoE | fused `exl3_moe`, tier legacy, `grouped_calls=0`: E3 v2 grouped kernels **not active** |
| Restart cost | ~7 min (worker weight load over NFS 353 s); the orchestrator says ~9 min drain+restart |
| Health | 12 h without errors, retries, NCCL warnings or preemptions; RDMA error counters 0 on both nodes |

## Baseline (gx-max via LiteLLM, streaming, medians)

Benchmark: `scripts/gxbench.py`. Headers are `X-GX-Reasoning: none` and
`X-GX-Priority: background`. Settings: temperature 0, 256 output tokens, a
random nonce prefix (no prefix-cache hits). Raw data:
`results-baseline-20261005*.json` in this folder.

| Case | Input tok | TTFT | Decode tok/s | Total |
|---|---|---|---|---|
| short ×3 | 79 | 0.45 s | 25.0 | 10.7 s |
| medium ×3 | 2,130 | 3.64 s | 24.8 | 13.8 s |
| long ×2 | 15,894 | 24.2 s (~660 tok/s prefill) | 26.2 | 34.1 s |
| conc 2 ×3 | 79 | 0.70 s | 18.5/req, 33.1 aggregate | — |
| conc 4 ×2 | 79 | 8.0 s (queued) | 18.2/req, 31.0 aggregate (capped by max-num-seqs 2) | — |

| Node | Mem used | MemAvailable min | Swap | GPU util (load) |
|---|---|---|---|---|
| gx10-01 | 117.7–118.9 GiB | 2.8 GiB | 15 GiB | ~72% |
| gx10-02 | 116.0–116.1 GiB | 5.5 GiB | 5.4 GiB | ~71% |

## Candidates

| # | Change | Class | Notes |
|---|---|---|---|
| 1 | `EXL3_FAT_GROUPED=1`, `EXL3_TEMP_ROWS_FUSED=16` (E3 v2 grouped MoE prefill, recommended by the pinned kit's `.env.example`) | BENCHMARK FIRST — **top** | Env-only. Roll back by removing the two lines and restarting. Watch the head's ~3 GiB MemAvailable. |
| 2 | `./start.sh pack` (Engram rows to local NVMe on each node) | BENCHMARK FIRST | Removes worker Engram NFS traffic from rail 1. Kit claims +25–50% prefill. Needs a maintenance window. |
| 3 | `NCCL_IB_HCA` on both PCIe halves (`rocep1s0f0,roceP2p1s0f0`) | BENCHMARK FIRST | Check the rail-2 GID first. Mainly helps prefill all-reduce. |
| 4 | MTU 9000 / RDMA MTU 4096 | BENCHMARK FIRST (low) | Host netplan change on both nodes. |
| 5 | Mia PR #6 MoE-X decode kernel | BENCHMARK FIRST (lab only) | Unmerged; needs an image rebuild. |
| 6 | Cooperative MoE overlay | INCOMPATIBLE / blocked | The pinned `.so` is not published. |
| 7 | vLLM 0.30/0.31, exllamav3 1.5.x | INCOMPATIBLE-RISKY | V4.1 gains target SM100; EXL3 is not upstream; the overlay is pinned. |
| 8 | Disable spec decode / change k | NO BENEFIT | k=3 measured fastest at concurrency ≤2. |
| 9 | Raise max-num-seqs / batched tokens | NO BENEFIT / risky | Memory wall; c4 aggregate = c2. |
| 10 | KV dtype / display-memory unlock | NO BENEFIT / INCOMPATIBLE | Desktops run on both nodes. |
| 11 | Re-scope the stale `GX_GATEWAY_KEY` virtual key (403 on gx-max); reconcile registry drift (rails, max_context, kv_bytes) | SAFE BENEFICIAL | LiteLLM admin + registry. No engine restart. Owner action. |

## A/B procedure for candidate #1 (ready, needs owner go-ahead)

1. Gate on idle: vLLM shows 0 running and 0 waiting requests for 60 s.
2. Back up `mia-dsv41/.env` with a timestamp, then add the two lines.
   - First confirm how the orchestrator overlays env at launch: the live
     engine has `GPU_MEM_UTIL=0.85` while `.env` says 0.88.
3. Restart via the RECOVERY.md lifecycle. Confirm `grouped_calls>0` on both
   ranks and no OOM or NCCL errors.
4. Run health checks:
   - real completions on gx-max and gx-auto
   - output sanity check
5. Re-run `scripts/gxbench.py` with identical arguments.
6. Keep only if all of these hold:
   - medium/long TTFT improves ≥10%
   - decode and conc2 aggregate are not worse by >3%
   - MemAvailable stays above ~1.5 GiB
   - no errors

   Otherwise restore `.env`, restart, and verify.

   Expected downtime is 7–9 min, or 15–18 min if rolled back.
