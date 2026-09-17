# Models

The eight aliases (seven on the gateway, plus gx-music on the music API) and the exact models behind them (pinned revisions are in
`legenex/models/registry.json` and on each card in **Models**).

## Which model should I use?

| If you need… | Use | Why |
|---|---|---|
| A quick answer, extraction, classification, a simple image question | `gx-mini` | Always loaded, ~52 tok/s, vision and tools |
| Coding, tools, agents, Kilo Code, longer answers | `gx-fast` | 35B MoE (3B active), kept warm, ~55 tok/s, 131k context |
| Hard maths, architecture, difficult debugging | `gx-reason` | Single-node reasoning tier on gx10-02 |
| The strongest model for the hardest work, and you can wait | `gx-max` | DeepSeek V4 Flash 0731 across both nodes; ~10 min cold start |
| "Just pick for me" (works well in Kilo) | `gx-auto` | Routes on the task, not on the size of the tool list |
| A picture, or an edit of a picture | `gx-image` | Qwen-Image-2512 / Qwen-Image-Edit-2511 |
| A short clip, an animated image, or an edited video | `gx-video` | Wan 2.2 A14B |

| Alias | Model (Hugging Face) | Node(s) | Engine | Context / output | Vision | Tools | Uncensored | Start |
|---|---|---|---|---|---|---|---|---|
| `gx-mini` | `HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive` (Q4_K_M) | gx10-01 | llama.cpp | 65,536 / 8,192 | yes | yes | yes | resident |
| `gx-fast` | `kyaky/Qwen3.6-35B-A3B-Uncensored-NVFP4` | gx10-01 | vLLM | 131,072 / 32,768 | yes | yes | yes | resident (cold load ~4 min) |
| `gx-reason` | `nvidia/Qwen3.6-27B-NVFP4` (**interim**, see below) | gx10-02 | vLLM | 65,536 / 16,384 | yes | yes | no | on demand, ~6–7 min |
| `gx-max` | `dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4` | both | SGLang TP=2 | 327,680 / 65,536 | no | yes | yes | explicit, ~10 min |
| `gx-auto` | router | gx10-01 | orchestrator | 57,344 / 8,192 | via tier | yes | via tier | always |
| `gx-image` | Qwen-Image-2512 + Qwen-Image-Edit-2511 | gx10-02 | ComfyUI | n/a | n/a | n/a | yes | first job loads |
| `gx-video` | Wan 2.2 T2V/I2V A14B + rzgar uncensored LoRAs | gx10-02 | ComfyUI | n/a | n/a | n/a | yes | first job loads |

## gx-mini

* **Model:** HauhauCS Qwen3.5-4B Uncensored "Aggressive", Q4_K_M GGUF with the
  BF16 vision projector, on llama.cpp. 4B dense, hybrid Gated DeltaNet
  attention.
* **Behaviour:** always loaded (preloaded when llama-swap starts), 65,536
  tokens per request, two requests in parallel. Thinking is off at the
  gateway.
* **Measured:** 52 tok/s; 0.1 s warm time to first token; vision (shapes,
  colours, digits) and tool calls correct.

## gx-fast

* **Model:** kyaky Qwen3.6-35B-A3B Uncensored, NVFP4 (compressed-tensors),
  on vLLM 0.28. 35B total, 3B active (8 of 256 experts). Vision tower
  included.
* **Behaviour:** kept loaded next to gx-mini (both together leave about
  46 GiB free on gx10-01). Thinking is off by default; send
  `"chat_template_kwargs": {"enable_thinking": true}` to turn it on
  (reasoning then arrives in `reasoning_content`).
* **Measured:** 55 tok/s; 0.08 s warm time to first token; coding, tools,
  vision and a 1,900-token answer correct.

## gx-reason

* **Target model:** `iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` (92.7B,
  abliterated, single-DGX-Spark layout). It is **gated** on Hugging Face and
  no token with access is configured, so it is not installed yet.
* **Serving now (interim):** `nvidia/Qwen3.6-27B-NVFP4`, a stock (not
  uncensored) 27B dense model, so the tier keeps working.
* **To finish:** Model Manager → Hugging Face access → save a read token
  whose account accepted the model terms, then install and assign the target.

## gx-max

* **Model:** dealign.ai DeepSeek-V4-Flash-0731 "CRACK" (abliterated). Its
  configuration and tensor layout are identical to the official
  `deepseek-ai/DeepSeek-V4-Flash-0731` (native FP8 + FP4), and it runs with
  the official SGLang DGX Spark recipe for that checkpoint.
* **Topology:** SGLang, tensor parallel 2 across both nodes, rank 0 on
  gx10-01, rank 1 on gx10-02, over both ConnectX rails.
* **Behaviour:** starting it stops every other model on both nodes. It is
  never started at boot and gx-auto never starts it. It releases itself after
  30 idle minutes.
* **Measured:** ready in 595 s; 40–47 tok/s; factual, reasoning and
  executable-code checks correct; 4–6 GiB of RDMA traffic per generation.

## gx-auto

Reads the task (Kilo's `<task>` / `<user_message>`), not the attached tool
schema or system prompt:

| Request | Tier |
|---|---|
| "are you there?", greetings, "what can you help me with in this repo?" | gx-mini |
| real repository changes, coding, tool loops | gx-fast |
| hard debugging, architecture, proofs | gx-reason |
| explicit "entire codebase / formal verification" work | gx-max, only if already running |

A tool-result turn in an agent loop stays on the tier of the original task.
`max_tokens` is capped to the chosen tier's output limit.

## gx-image

* **Generation:** Qwen-Image-2512 fp8 with the Lightning 4-step LoRA and, by
  default, the NSFW-capable perpetual3x adapter (exact 2512 base,
  non-commercial licence). About 30 s for 1024×1024.
* **Editing and variations:** Qwen-Image-Edit-2511 fp8mixed with its
  Lightning 4-step LoRA. The optional NSFW edit adapter is a Qwen-Image
  (original) LoRA, not an exact 2511 match, so it is off unless requested.
* Details: [Media: generate and edit](/#/docs/media).

## gx-video

* **Text-to-video and image-to-video:** Wan 2.2 A14B (two experts each) with
  the rzgar uncensored LightX2V 4-step LoRAs; 640×640, 3 s in about a minute.
* **Video editing:** the first frame is edited with Qwen-Image-Edit-2511 and
  the Wan 2.2 image-to-video experts re-render the clip from it.
* Details: [Media: generate and edit](/#/docs/media).
