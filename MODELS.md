# Models

Every model below was verified against the live HuggingFace API on 2026-09-14.
No model ID in this file was guessed. Where a hoped-for model does not exist,
that is stated explicitly.

---

## gx-mini — proven working

| Field | Value |
|---|---|
| Model | Qwen3.5-4B, Q4_K_M GGUF + BF16 mmproj |
| Files | `/srv/models/gguf/Qwen3.5-4B/Qwen3.5-4B-Q4_K_M.gguf`, `mmproj-BF16.gguf` |
| Engine | llama.cpp (`legenex/llama-cpp-spark:latest`, local image) |
| Node | gx10-01 |
| Disk | 3.2 GB |
| Context | 65 536 |
| Vision | **Yes** (mmproj, previously verified) |
| Tools | Yes |
| Measured | ~48 tok/s text; vision tested successfully |
| Why | Already proven. Cheap enough to stay hot, multimodal, good router/dispatch tier. |

Do not re-download.

## gx-fast — `nvidia/Qwen3.6-35B-A3B-NVFP4`

| Field | Value |
|---|---|
| Verified | `GET https://huggingface.co/api/models/nvidia/Qwen3.6-35B-A3B-NVFP4` → **HTTP 200**, `gated: false` |
| Quantisation | NVFP4 (NVIDIA ModelOpt) |
| License | Apache-2.0, no acceptance required |
| Disk | 23.5 GB |
| Engine | vLLM — NVIDIA's own card names Blackwell + vLLM as the supported path |
| Node | gx10-01 |
| Context | 262 144 native |
| Vision | Yes |
| Tools | Yes, native Hermes-style tool calling |
| Expected RAM | ~36 GiB resident, leaving ~80 GiB on node 1 |
| Why | The intended "Qwen3.6-35B-A3B class" target turned out to be real. NVIDIA's own NVFP4 build is the strongest available guarantee that the FP4 kernels bind on sm_121a, and it is 3 GB smaller than the `unsloth` equivalent. |

**Architecture note.** This is a `qwen3_5_moe` hybrid: 3× linear-attention (GDN)
to 1× full-attention layers, and only full-attention layers cache KV. With 10
full-attention layers that is ~20.5 KB/token, so 128K context costs only ~2.6 GB
of KV cache. KV is effectively free here.

Alternatives considered:
* `AEON-7/Qwen3.6-35B-A3B-heretic-NVFP4` (23.4 GB) — the uncensored variant, and
  the most-downloaded GB10-tagged model on the Hub. Rejected for the default
  because it needs a third-party source-built vLLM container. Viable swap-in.
* `HauhauCS/Qwen3.6-…-Q4_K_M.gguf` (21.2 GB) — continues the lineage the old
  repo recipes referenced. Note the repo's `Alibaba/Qwen3.5-35B-A3B-Uncensored-HauhauCS-*`
  path was a **local folder, not an upstream ID** — fetching it returns HTTP 401.

## gx-reason — `nvidia/Qwen3.6-27B-NVFP4`

**LIVE since 2026-09-16.** This tier was re-engined after B-011; see the
"superseded" note below for what it replaced and why.

| Field | Value |
|---|---|
| Verified | `GET https://huggingface.co/api/models/nvidia/Qwen3.6-27B-NVFP4` → **HTTP 200**, `gated: false`, `private: false` |
| Quantisation | NVIDIA ModelOpt `MIXED_PRECISION` — W4A16_NVFP4 MLP + FP8 linear-attention projections, FP8 KV cache. vLLM resolves it as `modelopt_mixed`. |
| License | Apache-2.0 |
| Disk | **20.42 GiB** (21,921,697,184 B, 3 shards — measured on disk, matches the HF API exactly) |
| Engine | vLLM, `jstarkg/vllm-gb10-flashnext:0.28-sm121-r6` — the **same image already proven for gx-fast** |
| Node | gx10-02 |
| Architecture | `Qwen3_5ForConditionalGeneration`, dense 27B, 64 layers, hybrid attention (3× linear/GDN : 1× full) |
| Context | 262 144 native; **served at 65 536** (KV headroom, per D-009) |
| Vision | Yes — the checkpoint carries a `vision_config` and image/video processors |
| Tools | Yes (`--tool-call-parser qwen3_xml`) — configured, not yet exercised live |
| Measured RAM | **~44 GiB** node-level with `--gpu-memory-utilization 0.35` (MemAvailable 114 → 70 GiB). Still owns node 2 exclusively — do not co-schedule with ComfyUI. |
| Measured speed | 12.4 tok/s generation; 401 s cold start |
| Why | It is the same `qwen3_5` hybrid-attention family that gx-fast already runs correctly on vLLM on this exact hardware, so it reuses a confirmed-good engine/architecture pairing. Dense 27B activates its full parameter count per token — a real compute step up from gx-fast's ~3B active — while being small enough to dodge B-009's ~55 GiB vLLM ceiling. |

### Superseded gx-reason candidates (do not re-deploy without reading B-011)

Two 122B-class checkpoints were tried for this tier before it was re-engined.
Both are still on node 2's disk and neither is in use:

| Checkpoint | On disk (node 2) | Status |
|---|---|---|
| `unsloth/Qwen3.5-122B-A10B-GGUF` (UD-Q4_K_XL) | 73 GB at `/srv/models/gguf/Qwen3.5-122B-A10B` | **Rejected.** Loads and generates, but every token is garbage on this llama.cpp build's CUDA path (B-011). CPU-only output is coherent, so the checkpoint is fine and the kernels are not. |
| `et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10` | 74 GB at `/srv/models/vllm/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10` | **Never deployed.** Downloaded as the vLLM candidate for this tier; superseded by the decision in D-021 before it was ever served. |

**147 GB of node-2 disk is held by these two unused checkpoints.** Deleting
them is a real reclaim, but it is a destructive, hard-to-undo action on
large downloads and needs a human decision — it is NOT done unilaterally.
Node 2 currently has 265 GB free, so there is no pressure to decide now.

**Note on the old "no Qwen3.6 in the 100-125B class" reasoning:** that gap is
real and still true, but it stopped mattering once the tier was re-scoped from
"biggest model that fits" to "most compute per token on an engine that is
actually correct here". See `coordination/DECISIONS.md` D-021.

## gx-max — `nvidia/DeepSeek-V4-Flash-0731-NVFP4`

| Field | Value |
|---|---|
| Local path | `/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4` (both nodes) |
| Disk | 164 GB, 48 shards, **already present on both nodes** |
| Quantisation | Hybrid FP8 + NVFP4 (NVFP4 MoE, group_size 16) |
| Engine | SGLang `lmsysorg/sglang:dev-v4f-2dgx-v2`, TP=2, nnodes=2 |
| Nodes | gx10-01 (rank 0) + gx10-02 (rank 1) |
| Context | 327 680 |
| Draft model | DSpark speculative head, bundled in the checkpoint |
| Status | **Verified serving 2026-09-14.** See TEST_RESULTS.md. |

LOCKED. Do not change the engine, the model, or the parallelism. See
ARCHITECTURE.md L-6.

## gx-image / gx-video — ComfyUI on node 2

All four intended candidates were verified to exist, current, and **ungated**:

| Alias | Model | HF repo | HTTP | License | Gated |
|---|---|---|---|---|---|
| gx-image | Qwen Image 2512 | `Qwen/Qwen-Image-2512` | 200 | Apache-2.0 | No |
| gx-image | HiDream I1 Full | `HiDream-ai/HiDream-I1-Full` | 200 | MIT | No |
| gx-video | LTX 2.3 | `Lightricks/LTX-2.3` | 200 | LTX-2 Community | No |
| gx-video | Wan 2.2 A14B | `Wan-AI/Wan2.2-I2V-A14B` | 200 | Apache-2.0 | No |

**Wiring gap, found 2026-09-14, not yet closed:** HiDream I1 Full and a
no-LoRA "quality" Wan 2.2 variant are verified/documented above as available
checkpoints, but neither has a `_gx`-enabled template in
`legenex/media/workflows/` — the router can only build a graph from a
template that declares one. Concretely, `gx-image` cannot serve HiDream
today, and `gx-video` has no "hd"-equivalent tier the way `gx-image` does
(standard/hd). Not blocking (Qwen-Image + Lightning and Wan 2.2 + Lightning
both work), but worth a deliberate decision before assuming gx-image/
gx-video are feature-complete.

Non-obvious packaging details:

* Qwen 2512's ComfyUI single-file lives in `Comfy-Org/Qwen-Image_ComfyUI` as
  `qwen_image_2512_fp8_e4m3fn.safetensors` (20.4 GB), **not** in the Qwen repo.
* `Comfy-Org/ltx-2.3` contains **only LoRAs**; the LTX 2.3 transformer/VAE come
  from `Kijai/LTX2.3_comfy` plus the gemma-3-12B encoder from `Comfy-Org/ltx-2`.
* Wan "A14B" is **two** 14B experts (high-noise + low-noise), both required —
  28.6 GB, not 14.3 GB.

Honest performance expectation on GB10 (273 GB/s bandwidth is the binding
constraint, not the 128 GB):

* Qwen-Image 2512 @1024², 50 steps: **61 s** optimised, 212 s unoptimised.
  With the 4-step Lightning LoRA (0.85 GB): **~5–20 s**.
* HiDream I1 Full: ~150–350 s, **no distillation LoRA exists**.
* Wan 2.2 at default steps: 15–30+ min. With lightx2v 4+4-step LoRAs: est. 3–7 min.
* LTX 2.3 distilled: est. 2–10 min — **genuinely unmeasured on GB10**.

**Therefore `gx-image` is an interactive endpoint (seconds) and `gx-video` is a
job queue (minutes).** `gx-video` is exposed as the asynchronous OpenAI
`/v1/videos` create/status/content shape, which LiteLLM speaks natively.

`Lightricks/LTX-2.5` is newer and better-packaged but is **`gated: "auto"`** — it
needs a human to click through at huggingface.co and supply an `HF_TOKEN`.

**LTX 2.3 is currently NOT used.** It carries the LTX-2 Community Licence rather
than Apache/MIT, and its pipeline pulls a Gemma-3 text encoder under the Gemma
Terms. Wan 2.2 A14B (Apache-2.0) covers video on its own, so nothing was
downloaded or accepted under the LTX licence. If LTX is wanted later, a human
should read both licences first — especially for commercial ad-creative work.

## Measured media performance (node 2, direct against ComfyUI)

| Workflow | Setting | Measured |
|---|---|---|
| Qwen-Image-2512 + 4-step Lightning LoRA | 1328x1328 | **12.6 s** |
| Qwen-Image-2512, no LoRA | 1328x1328 | 251 s |
| Wan 2.2 A14B text-to-video | 640x640, 49 frames | **56.7 s** |

The Lightning LoRA is worth ~20x and is what makes `gx-image` an interactive
endpoint rather than a batch job.

## Disk budget

| Item | Node | Size |
|---|---|---|
| DeepSeek V4 Flash | both | 164 GB each (duplicated — no shared FS) |
| Qwen3.6-35B-A3B-NVFP4 | node 1 | 23.5 GB |
| Qwen3.6-27B-NVFP4 (gx-reason, LIVE) | node 2 | 20.4 GiB |
| Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10 (unused) | node 2 | 74 GB |
| Qwen3.5-122B-A10B GGUF (rejected, B-011) | node 2 | 73 GB |
| Qwen3.5-4B GGUF | node 1 | 3.2 GB |
| Media models (staged) | node 2 | ~147 GB |

Node 1 had 337 GB free, node 2 had 571 GB free before these downloads.
