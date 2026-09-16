# Models

## Which model should I use?

| If you need… | Use | Why |
|---|---|---|
| A quick answer, extraction, classification, short vision task | `gx-mini` | Always loaded, sub-second, vision-capable |
| An agent that calls tools, reads images, writes longer answers | `gx-fast` | 35B MoE on vLLM, native tool calling, 65k context |
| Careful reasoning, maths, tricky code, planning | `gx-reason` | Dense 27B with separated reasoning output |
| The strongest model for long, high-stakes work, and you can wait | `gx-max` | DeepSeek V4 Flash across both nodes; ~9 min cold start |
| "Just pick for me" | `gx-auto` | Orchestrator chooses mini / fast / reason per request |
| A picture | `gx-image` | Qwen-Image-2512 through ComfyUI |
| A short video clip | `gx-video` | Wan 2.2 T2V through ComfyUI, asynchronous |

| Alias | Node(s) | Engine | Context | Vision | Tools | Cold start |
|---|---|---|---|---|---|---|
| `gx-mini` | gx10-01 | llama.cpp | 65,536 | yes | yes | resident (always hot) |
| `gx-fast` | gx10-01 | vLLM | 65,536 | yes | yes | minutes, on demand |
| `gx-reason` | gx10-02 | vLLM | 65,536 | yes | yes | ~6–7 min, on demand |
| `gx-max` | both | SGLang TP=2 | 327,680 (gateway caps input at 65,536) | no | yes | ~8.5–9.5 min |
| `gx-auto` | router | orchestrator | 24,576 (safe common size) | via chosen tier | yes | n/a |
| `gx-image` | gx10-02 | ComfyUI | n/a | n/a | n/a | first image loads weights |
| `gx-video` | gx10-02 | ComfyUI | n/a | n/a | n/a | first video loads weights |

## gx-mini

* **Model:** Qwen3.5-4B (Q4_K_M GGUF) with a BF16 vision projector, on
  llama.cpp.
* **Best at:** fast chat, summaries, JSON extraction, simple vision (read a
  chart, spot shapes, read digits).
* **Behaviour:** never unloaded (llama-swap `ttl: 0`). About 48 tokens/s.
  Thinking is disabled so answers come straight back.
* **Limits:** 65,536 tokens shared across two parallel slots; 4,096 output
  tokens.

## gx-fast

* **Model:** `nvidia/Qwen3.6-35B-A3B-NVFP4` on vLLM.
* **Best at:** tool-using agents, vision, general assistant work.
* **Behaviour:** loads on the first request (a few minutes on a cold node) and
  unloads after 30 minutes idle. Tool calls use the `qwen3_xml` parser and
  come back as standard OpenAI `tool_calls`.
* **Limits:** 65,536 context, 8,192 output tokens.

## gx-reason

* **Model:** `nvidia/Qwen3.6-27B-NVFP4` (dense) on vLLM, on gx10-02.
* **Best at:** multi-step reasoning, maths, careful coding.
* **Behaviour:** loads on demand (measured 401 s cold) and unloads after
  15 minutes idle. Its thinking is returned separately as
  `reasoning_content`, and it counts against the output budget, so give it a
  generous `max_tokens` (1,500–4,000 for real problems).
* **Limits:** 49,152 input, 16,384 output tokens. About 12 tokens/s.

## gx-max

* **Model:** `nvidia/DeepSeek-V4-Flash-0731-NVFP4` on SGLang
  (`lmsysorg/sglang:dev-v4f-2dgx-v2`), **TP=2, nnodes=2**: rank 0 on
  gx10-01, rank 1 on gx10-02.
* **Best at:** the hardest and longest tasks.
* **Behaviour:** not running by default. A direct `gx-max` request (or LOAD in
  the control UI) takes over **both** nodes. Normal models are drained, the
  engine loads for about 9 minutes, then serves at roughly 41–45 tokens/s.
  It releases itself after 30 minutes idle and the normal models come back.
* **Never downgrades:** if gx-max cannot be brought up, the request fails
  with HTTP 503 `gx_max_unavailable`. It is never answered by another model.
* See [gx-max explained](/#/docs/operations) for the full lifecycle.

## gx-auto

* **What it is:** a routing alias. The orchestrator reads the request (length,
  images, tools, wording) and forwards it to `gx-mini`, `gx-fast` or
  `gx-reason` through the gateway.
* **gx-auto never acquires gx-max.** If a prompt looks gx-max-worthy but
  gx-max is not already running, gx-auto picks the best available tier
  instead. If gx-max is already READY, gx-auto may use it.
* The response header `X-GX-Routed-To` names the tier that answered (visible
  when you call the orchestrator directly; the playground shows the model
  that answered).

## gx-image

* **Model:** Qwen-Image-2512 fp8 with the 4-step Lightning LoRA (default) or
  full sampling (`quality: "hd"`).
* **Endpoint:** `POST /v1/images/generations` on the gateway.
* **Speed:** about 13–30 s per image with Lightning, about 4 minutes with
  `hd`.
* Sizes: width and height 256–2048, multiples of 16. 1328×1328 is native.

## gx-video

* **Model:** Wan 2.2 T2V-A14B fp8 (two experts plus 4-step LoRAs).
* **Endpoint:** the media router's asynchronous API (`POST /v1/videos`, then
  poll, then fetch the MP4). There is no OpenAI video standard, so this is
  not a normal chat/image call.
* **Reachability:** the router listens on the fabric (`192.168.100.11:18800`),
  so scripts must run on gx10-01. From anywhere else, use the control UI
  playground.
* **Speed:** about 1 minute for 2–3 s of 640×640 video at 16 fps.
