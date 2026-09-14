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

## gx-reason — `et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10`

| Field | Value |
|---|---|
| Verified | `GET https://huggingface.co/api/models/et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10` → **HTTP 200**, `gated: false` |
| Quantisation | NVFP4 experts + FP8 W8A8 dense + BF16 lm_head |
| License | Apache-2.0 |
| Disk | 78.8 GB |
| Engine | **stock** vLLM — packaged as standard compressed-tensors, no fork needed |
| Node | gx10-02 |
| Context | 262 144 native; **served at 131 072** (memory headroom) |
| Vision | Yes |
| Tools | Yes |
| Expected RAM | ~86 GiB — **must own node 2 exclusively**, do not co-schedule with ComfyUI |
| Why | Built specifically for DGX Spark. The runner-up `-Full-GB10` (70.8 GB, all-FP4) is ~45% faster but its author documents a real quality regression from 4-bit dense activations. gx-reason is the *quality* tier, so quality wins. |

**There is no Qwen3.6 or Qwen3.8 in the 100–125B class.** That is a genuine
upstream generation gap, so this tier stays on Qwen3.5. Vendor fallback if the
community build misbehaves: `nvidia/Qwen3.5-122B-A10B-NVFP4` (83.5 GB).

No speculative decoding: this checkpoint's MTP tensors are BF16 raw copies vLLM
cannot load.

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
| Qwen3.5-122B (gx-reason) | node 2 | 78.8 GB |
| Qwen3.5-4B GGUF | node 1 | 3.2 GB |
| Media models (staged) | node 2 | ~147 GB |

Node 1 had 337 GB free, node 2 had 571 GB free before these downloads.
