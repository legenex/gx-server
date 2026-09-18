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

## gx-fast — `kyaky/Qwen3.6-35B-A3B-Uncensored-NVFP4`

> Re-engined to the uncensored checkpoint in the V2 model set (2026-09-17).
> `legenex/models/registry.json` is the source of truth; the table below
> describes the superseded `nvidia/Qwen3.6-35B-A3B-NVFP4`.

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

## gx-reason — `wyattearp/Qwen3.8-27B-Uncensored-NVFP4`

**LIVE since 2026-09-18 (D-042).** Display name: **Qwen3.8-27B Dense Uncensored
NVFP4**. This replaced both the interim `nvidia/Qwen3.6-27B-NVFP4` (deleted from
gx10-02, not a fallback) and the abandoned
`iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` target — **B-030 is closed as
obsolete**, because that model is no longer wanted.

> **Not** the retired `gx10-vllm/Qwen3.8-27B-Uncensored` runtime (locked
> decision 15), which stays retired. That is a local runtime folder; this is an
> upstream NVFP4 repository pinned to an immutable revision. The names are
> similar and the artefacts are not.

| Field | Value |
|---|---|
| Repository | `wyattearp/Qwen3.8-27B-Uncensored-NVFP4` |
| Revision | `91ec573a3d8e660b78b7161395e4a5b6247c2c8b` (pinned; never served from `main`) |
| Base model | `JonathanColetti/Qwen3.8-27B-Uncensored` |
| Verified | 2026-09-18 — all 22 files checked on disk against that revision, 14 sha256-checked, `.gx-manifest.json` written |
| Quantisation | NVFP4 via **compressed-tensors** (auto-detected from `config.json`; `--quantization modelopt` is wrong for this checkpoint and was removed) |
| License | Apache-2.0, ungated |
| Disk | **26.61 GiB** (28,571,880,859 B, 12 shards + an MTP head) |
| Engine | vLLM, `jstarkg/vllm-gb10-flashnext:0.28-sm121-r6` — the **same image already proven for gx-fast**; arch support confirmed inside the image before any config change |
| Node | gx10-02 |
| Architecture | `Qwen3_5ForConditionalGeneration`, dense 27B, 64 layers, hybrid attention: **48 linear-attention + 16 full-attention**. Only those 16 layers hold a KV cache, which is why 65 536 context is cheap here. |
| Context | 262 144 native; **served at 65 536** (D-009) |
| Vision | **Yes, exercised live** — `qwen3_5_vision` encoder, depth 27. It read the red jacket out of a real generated source image. |
| Tools | **Yes, exercised live** — `--tool-call-parser qwen3_xml` returned a correct `get_weather` call |
| Reasoning | **Yes, exercised live** — separated from `content` by `--reasoning-parser qwen3` |
| Measured RAM | **51.2 GiB** node-level at `--gpu-memory-utilization 0.42` (MemAvailable 114.68 → 63.45 GiB, back to 114.68 after unload). Re-measured for this checkpoint; the old model's 44 GiB was **not** carried over. |
| Measured speed | ~9 tok/s decode (no MTP); **392 s** cold start |
| Speculative decoding | The checkpoint ships `model-mtp.safetensors`, but MTP is **off** for bring-up. Standard inference first; re-enabling it is a separate measured change. |
| Why | Uncensored, dense 27B, multimodal, and the same `qwen3_5` hybrid-attention family gx-fast already runs correctly on vLLM on this exact hardware — so it reuses a confirmed-good engine/architecture pairing rather than introducing an unverified one. |

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

## gx-max — `dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4`

> Since D-032 (2026-09-17) the served checkpoint is the abliterated CRACK
> build (cookbook cell `fp4`). The former rollback
> `nvidia/DeepSeek-V4-Flash-0731-NVFP4` (cell `nvfp4`) was **deleted from both
> nodes** on 2026-09-17 (B-026), so rolling back needs a fresh download.
> The table below describes that deleted checkpoint.

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

### gx-image model: VisionmasterPro_V3 (Build V3, 2026-09-17)

| Field | Value |
|---|---|
| Shown as | **VisionmasterPro_V3** (the file name is never the user-facing name) |
| Requested checkpoint | `pornmasterPro_noobV3VAE` (Civitai model 1045588, version 1767015; the Civitai download needs an API key, HTTP 401) |
| Source used | `votepurchase/pornmasterPro_noobV3VAE` @ `75f59d136b165d48f3e678bb057af99f7cf1a71e` (public, not gated) |
| Format | diffusers SDXL (`StableDiffusionXLPipeline`), UNet F32, `prediction_type: epsilon`, EulerDiscrete |
| Files (node 2) | `image/diffusion_models/pornmasterPro_noobV3VAE/unet.safetensors` sha256 `16fd4046…f5da` (10.27 GB); `image/text_encoders/pornmasterPro_noobV3VAE/clip_l.safetensors` `911844f9…62f6`; `…/clip_g.safetensors` `7556aaa7…340ce`; `image/vae/pornmasterPro_noobV3VAE/vae.safetensors` `98a14dc6…f88` |
| Verification | `hf-verify.py` against the HF LFS oids (manifest `/srv/models/image/pornmasterPro_noobV3VAE/.gx-manifest.json`) |
| Conversion | none: ComfyUI loads the diffusers files directly |
| Licence | creativeml-openrail-m (model card) |
| Runtime | ComfyUI on gx10-02 behind the media router, `image_model: visionmaster-pro-v3`; templates `sdxl-visionmaster-pro-v3{,-img2img,-inpaint}` |
| Footprint | measured live, see `coordination/build-v3/img.md` |


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

## gx-voice / gx-call / gx-live — node-2 tenant services (Build V3, D-040)

| Alias | Repository | Revision | Runtime | Footprint |
|---|---|---|---|---|
| `gx-voice` | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` (+ VoiceDesign, Base, Tokenizer-12Hz, `openai/whisper-large-v3-turbo`) | `0c0e3051…` | transformers on torch 2.14/cu130, `gx-voice-engine:qwen3tts-022e286-t214` | **measured**: 9.45 GiB cold, 6.5 GiB resident, 35.0 s start, RTF 0.72-1.00 |
| `gx-live` | `openbmb/MiniCPM-o-4_5` | `503e7542…` | transformers 4.51 remote code, SDPA (no flash-attn on sm_121), `gx-live-engine:minicpmo45-503e754-t214` | **measured**: 34 GiB cold, 31 GiB resident, ~102-132 s start |
| `gx-call` | `nvidia/NVIDIA-NemotronLabs-VoiceChat-11B` | `a4c40ca5…` | NeMo Speech `StreamingS2SPipeline`, `gx-call-engine` | **not measured** — the model has never been loaded |

Notes that cost time to learn:

* **Native full-duplex is not realtime on GB10.** MiniCPM-o's `as_duplex` mode
  costs 0.74 s per 1 s unit while listening but 1.5-1.6 s while speaking. gx-live
  therefore ships VAD turn-taking with barge-in over a full-duplex transport.
  Measured barge-in latency on the deployed stack: **169 ms**.
* **`mamba-ssm` needs C++20 against torch 2.14.** Its CUDA extension defaults to
  `-std=c++17`, and torch 2.14's ATen headers refuse to compile under it
  (`#error C++20 or later compatible compiler is required to use ATen`). Build it
  with C++20, and restrict `TORCH_CUDA_ARCH_LIST` — the default builds nine
  architectures we will never run.
* Every one of these services is fabric-only, bearer-key authenticated, starts
  its engine on demand through the admission guard and unloads when idle.

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
| Qwen3.8-27B-Uncensored-NVFP4 (gx-reason, LIVE) | node 2 | 26.61 GiB |
| Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10 (unused) | node 2 | 74 GB |
| Qwen3.5-122B-A10B GGUF (rejected, B-011) | node 2 | 73 GB |
| Qwen3.5-4B GGUF | node 1 | 3.2 GB |
| Media models (staged) | node 2 | ~147 GB |

Node 1 had 337 GB free, node 2 had 571 GB free before these downloads.


---

## Memory reality check — measured 2026-09-16

Sizing any tier from a container's `docker stats` or `--memory` cap is wrong on
this hardware (`coordination/BLOCKERS.md` B-021). The CUDA/unified-memory pool
is not charged to the container cgroup on DGX Spark/GB10, so:

* `gx-reason` measures 44 GiB of real node footprint while its cgroup reports
  10.92 GiB;
* a deliberate test capped a `gx-max` rank at `--memory 28g` — below its
  measured working set — and the container was **not** killed by its cgroup;
  the node still ran to 0 MiB MemAvailable and the *global* OOM killer fired.

**Always size from the node's own `/proc/meminfo` MemAvailable**, which is what
the admission guard reads.

| Tier | Engine | Node | Real node footprint | How measured |
|---|---|---|---|---|
| `gx-mini` | llama.cpp | node 1 | ~10 GiB | resident tier, cgroup 6.5 GiB |
| `gx-fast` | vLLM | node 1 | ~25 GiB | `--gpu-memory-utilization` bounded |
| `gx-reason` | vLLM | node 2 | **~44 GiB** | MemAvailable 114 → 70 GiB, loaded |
| `gx-image` / `gx-video` | ComfyUI | node 2 | ~57-73 GiB peak | MemAvailable min 59.7 GiB (image) / 42.8 GiB (video) |
| `gx-max` rank0/rank1 | SGLang TP=2 | both | **~117 GiB peak per rank** | eight runs; nodes driven to 437 MiB–0 MiB |

### Why gx-max was once thought not to fit — SUPERSEDED, gx-max serves

> **This section's conclusion was wrong and is kept only as the record of what
> was measured and how it was misread.** gx-max has served since D-025
> (2026-09-16) and was accepted again on 2026-09-17 through the Control Center
> MAX profile (acquire 671 s, release 50 s). B-022 is RESOLVED. The arithmetic
> below is real; what it does not capture is the load path that makes it work.
> Do not quote this section as a reason not to run gx-max.

The original (superseded) reasoning:

The checkpoint is **163.48 GiB** on disk over 48 shards, of which **155.77 GiB
is MoE expert weights**. At the locked `--tp 2` each rank holds roughly half —
**~82 GiB, two thirds of a 121.63 GiB node** — before any KV cache, and the
loader adds ~26 GiB of pinned host memory during weight placement.

`--mem-fraction-static` does not help: measured at 0.50 and at 0.70 the
load-phase trough is *identical*, because the trough is weights landing in
driver-held memory, not the KV/static pool. It moves only the steady state
(0.80 → 0.70 buys back 12.2 GiB of a hypothetical steady state).

See `coordination/BLOCKERS.md` B-022 (**RESOLVED**, with the correction) and
`TEST_RESULTS.md` §15.1 for the full run table.
