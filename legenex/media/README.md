# gx-media — `gx-image` and `gx-video` on node 2

The media tier for the dual GX10 cluster. Two containers on **gx10-02**:

| Service | Address | Role |
|---|---|---|
| `gx-media-router` | `192.168.100.11:18800` | **The only ingress.** OpenAI-shaped HTTP API, authenticated, validating. |
| `gx-comfyui` | `127.0.0.1:8188` | Generation engine. Loopback only. Never exposed. |

LiteLLM's `gx-image` / `gx-video` aliases already point at
`http://192.168.100.11:18800/v1` (`legenex/gateway/litellm/config.yaml`) with
`GX_MEDIA_API_KEY` as the bearer token, so nothing in the gateway has to change.

---

## Why ComfyUI is not the ingress

ComfyUI has no authentication and two endpoints that are, together, a remote
code execution and file disclosure primitive:

* `POST /prompt` executes an **arbitrary node graph** — node classes, model
  filenames and output paths all come from the request body.
* `GET /view?filename=…&subfolder=…&type=…` reads a file off disk and returns it,
  unauthenticated.

So ComfyUI is bound to host loopback and lives on a private compose bridge. The
router is the only process on a routable address. A caller sends
`{"prompt": "...", "size": "1024x1024"}`; the router **builds the graph itself**
from a vetted template in `workflows/`. Node ids, model filenames, samplers and
output prefixes are never taken from the network, and the `/view` arguments the
router uses come from ComfyUI's own `/history` response — never from a caller.

---

## Hardware facts this stack is built around

Measured on gx10-02, not assumed. Re-read these before changing anything.

| Fact | Consequence |
|---|---|
| aarch64, GB10 Blackwell, **compute capability 12.1 (sm_121)**, driver 580.173.02, CUDA 13.0 | CUDA 13 wheels only |
| `torch 2.14.0+cu130` ships `sm_80/90/100/110/120` cubins and **no PTX** | sm_120 cubins execute correctly on sm_121 (same Blackwell major). No `TORCH_CUDA_ARCH_LIST=12.1`, no SageAttention source build. |
| `download.pytorch.org/whl/cu130` serves **cp312 aarch64** wheels | Use that index. Do **not** pin torch low — a low pin pairs with a mismatched `torchaudio` whose native library then fails to load. |
| GPU access is **CDI** | `--device nvidia.com/gpu=all`. There is **no** `nvidia` docker runtime: `--gpus all` and `--runtime nvidia` both fail on these nodes. |
| **121 GiB unified memory**, CPU and GPU compete for it | Never `--gpu-only` / `--highvram`. Never two large models resident at once. |
| `--force-fp16` produces NaNs on Blackwell | Never pass it. It is absent from the Dockerfile deliberately. |
| Triton JIT-compiles a CUDA shim at first text-encode and needs `Python.h` | `python3-dev` in the image is **load-bearing**, not optional. |
| No aarch64 `onnxruntime-gpu` wheel exists | ControlNet preprocessors would silently fall back to CPU. No shipped workflow uses them. |
| Evicting a resident model set costs **270–430 s** | One generation at a time, enforced by a global mutex. |

---

## Memory interlock — read before starting anything

Node 2 is shared. **Check before every start:**

```bash
ssh -o BatchMode=yes legenex-02@gx10-02 'free -g; docker ps --format "{{.Names}} {{.Status}}"'
```

* **`gx-max-rank0` / `gx-max-rank1` running** → **STOP.** That job owns both
  nodes. Do not start the media stack, do not disturb it.
* **`gx-reason` loaded** (~77 GB llama.cpp) → unload it first and wait for
  available memory to come back above ~100 GiB:

  ```bash
  curl -sS -X POST http://192.168.100.11:28080/api/models/unload \
       -H "Authorization: Bearer $GX_SWAP_API_KEY"
  ```

ComfyUI and `gx-reason` must **never** be resident at the same time. Two 77 GB
mmaps on a 121 GiB node put it into sustained page-cache thrashing: the kernel
stays up and answers ICMP, but userspace starves and SSH stops completing its
handshake. Because mmap pages are reclaimable the OOM killer may not fire, so
the node does not recover quickly on its own.

---

## Start / stop

On **node 2**, from a checkout of this directory (`/home/legenex-02/gx-media`):

```bash
# One-time build. The ComfyUI image pulls ~5 GB of cu130 wheels; run it in tmux.
tmux new -s comfybuild
docker compose -f docker-compose.media.yml build

# Start (requires GX_MEDIA_API_KEY in the environment or a .env beside the file;
# compose REFUSES to start the router without it).
export GX_MEDIA_API_KEY=...        # same value as legenex/gateway/.env
docker compose -f docker-compose.media.yml up -d

# Watch it come up. ComfyUI's first start is slow; the healthcheck allows 300 s.
docker compose -f docker-compose.media.yml logs -f

# Stop. This releases all GPU memory.
docker compose -f docker-compose.media.yml down

# Free model memory without stopping the service (e.g. to hand the node back):
curl -sS -X POST http://127.0.0.1:8188/free \
     -H 'Content-Type: application/json' \
     -d '{"unload_models": true, "free_memory": true}'
```

Health, from node 1 — unauthenticated, safe to poll:

```bash
curl -sS http://192.168.100.11:18800/health | python3 -m json.tool
```

---

## API

Authentication: `Authorization: Bearer $GX_MEDIA_API_KEY` on everything except
`/health`. Compared with `hmac.compare_digest`.

### `POST /v1/images/generations` — synchronous

```bash
curl -sS -X POST http://192.168.100.11:18800/v1/images/generations \
  -H "Authorization: Bearer $GX_MEDIA_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "a hummingbird beside a red trumpet flower, morning light",
       "size": "1328x1328", "n": 1, "response_format": "b64_json"}'
```

| Field | Default | Notes |
|---|---|---|
| `prompt` | — | required, ≤ 4000 chars |
| `n` | 1 | 1–4, becomes ComfyUI `batch_size` |
| `size` | `1328x1328` | `WxH`, each 256–2048 and a multiple of 16; `auto` = default. 1328² is Qwen-Image's native resolution. |
| `quality` | `standard` | `standard` → Lightning 4-step (~13 s). `hd` → full sampling (~250 s). |
| `response_format` | `b64_json` | or `url` → `/v1/images/{id}/content/{i}` |
| `seed` | random | 0 … 2⁶³−1; set it for reproducibility |
| `negative_prompt`, `steps`, `cfg`, `sampler_name`, `scheduler` | per workflow | bounded and allowlisted |

Response is OpenAI-shaped (`{"created", "data": [{"b64_json"}]}`) plus a `gx`
block carrying the job id, workflow, size, seed and measured `elapsed_seconds`.

### `POST /v1/videos` — asynchronous

Video takes minutes, so this returns **202** immediately with a job id.

```bash
ID=$(curl -sS -X POST http://192.168.100.11:18800/v1/videos \
      -H "Authorization: Bearer $GX_MEDIA_API_KEY" \
      -H 'Content-Type: application/json' \
      -d '{"prompt": "a slow push-in on a rain-streaked window", "seconds": 3}' \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -sS "http://192.168.100.11:18800/v1/videos/$ID" \
     -H "Authorization: Bearer $GX_MEDIA_API_KEY"          # poll until completed

curl -sS "http://192.168.100.11:18800/v1/videos/$ID/content" \
     -H "Authorization: Bearer $GX_MEDIA_API_KEY" -o out.mp4
```

`seconds` (0.5–20) or `length` in frames. Wan's latent packing requires
`(length - 1) % 4 == 0`; the router snaps to the nearest valid count.

### Other routes

`GET /health` (no auth) · `GET /v1/models` · `GET /v1/images/{id}/content/{i}` ·
`GET /v1/videos/{id}` · `GET /v1/videos/{id}/content`

Errors are OpenAI-shaped: `400` validation, `401` auth, `404` unknown job/route,
`502` ComfyUI rejected or failed the graph (the failing node and exception are
included), `503` the generation slot was busy past the wait (`Retry-After: 30`),
`504` generation exceeded its timeout.

---

## Concurrency

ComfyUI has one queue, and evicting a resident model set costs 270–430 s, so
overlapping generations are actively harmful. **Every** generation — synchronous
image or asynchronous video — holds one global mutex (`GenerationSlot`) for its
whole duration. Images block on it for up to `GX_MEDIA_QUEUE_WAIT` (default
900 s) then return `503`. Videos are queued to a single background worker that
takes the same mutex, so a video in flight serialises against images.

`tests/test_router.py::test_concurrent_image_requests_never_overlap_upstream`
fires four simultaneous requests and asserts the upstream never saw two at once.

---

## Models

Everything is already on node 2 under `/srv/models`. Shared text encoders and
VAEs live exactly once in `/srv/models/shared` and are visible to both the image
and video roots via `comfyui/extra_model_paths.yaml`.

| File | Size | Licence |
|---|---|---|
| `image/diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors` | 20.4 GB | Apache-2.0, ungated |
| `image/loras/Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors` | 1.70 GB | Apache-2.0 |
| `image/diffusion_models/hidream_i1_full_fp8.safetensors` | 17.1 GB | (alternative, no shipped workflow) |
| `video/diffusion_models/wan2.2_t2v_{high,low}_noise_14B_fp8_scaled.safetensors` | 14.3 GB each | Apache-2.0 — **both experts required** |
| `video/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_{high,low}_noise.safetensors` | 1.23 GB each | 4-step distill |
| `shared/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | 9.38 GB | Qwen-Image text encoder |
| `shared/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors` | 6.74 GB | Wan text encoder |
| `shared/vae/{qwen_image_vae,wan_2.1_vae}.safetensors` | 254 MB each | |

**`Lightricks/LTX-2.3` is deliberately absent.** It is under the LTX-2 Community
Licence, not Apache/MIT. That is a human decision, not an agent's — Wan 2.2 is
Apache-2.0 and covers video without it.

### Workflow templates

`workflows/*.api.json` are ComfyUI API-format graphs plus a `_gx` metadata block
declaring which node inputs a caller may influence, by logical name. Anything not
listed in `_gx.bindings` is unreachable from the network. Node input names were
taken from the installed ComfyUI source, not from memory.

| Template | Model | Measured |
|---|---|---|
| `qwen-image-2512-lightning` | Qwen-Image-2512 fp8 + 4-step Lightning LoRA | **12.6 s** @ 1328², 4 steps, cfg 1.0 |
| `qwen-image-2512-quality` | Qwen-Image-2512 fp8, full sampling | ~251 s @ 1328², 50 steps |
| `wan22-t2v-a14b-lightning` | Wan 2.2 T2V-A14B fp8, two experts + 4-step LoRAs | **56.7 s** @ 640², 49 frames |

The Lightning LoRA is a ~20× speedup at visually comparable quality, so it is the
default. It **requires** `steps=4` and `cfg=1.0`: raising cfg re-enables the
classifier-free guidance the LoRA was distilled to remove, and burns the output.

---

## Tests

No GPU, no ComfyUI, no network. Hermetic and safe to rerun.

```bash
cd router && ./qa.sh
```

36 tests: workflow template loading and binding safety, boundary validation
(size/n/seed/prompt/length/format, valid and invalid paths), the global mutex
(including release-on-exception), the job store's bounded eviction, HTTP auth on
every route, and — via a faithful stub of ComfyUI's wire protocol — the real
`ComfyClient` against `/prompt`, `/history`, `/view`, `/system_stats` and both of
ComfyUI's failure shapes (graph rejected at submit, node raising mid-execution).

`qa.sh` also byte-compiles the package, validates the compose YAML, and scans the
tree for credentials.

---

## Layout

```
legenex/media/
├── Dockerfile                      ComfyUI: aarch64 / CUDA 13 / sm_121, pinned commit
├── docker-compose.media.yml        both services, CDI, the loopback/fabric split
├── comfyui/extra_model_paths.yaml  three model roots, shared encoders stored once
├── workflows/*.api.json            vetted graphs + _gx binding declarations
└── router/
    ├── Dockerfile                  python:3.12-slim, stdlib only, non-root, read-only fs
    ├── qa.sh                       the QA gate
    ├── gx_media_router/
    │   ├── config.py               env-resolved, frozen, bounds-checked
    │   ├── errors.py               structured errors -> HTTP status codes
    │   ├── validation.py           the boundary; nothing downstream re-checks
    │   ├── workflows.py            template loading, validation, binding
    │   ├── comfy.py                ComfyUI client (submit / poll / fetch)
    │   ├── jobs.py                 the global generation slot + bounded job store
    │   ├── service.py              application layer, async video worker
    │   └── server.py               HTTP ingress
    └── tests/                      36 tests, stdlib unittest
```

The router has **no third-party dependencies** — deliberate. Nothing to pin,
nothing to audit, no aarch64 wheel availability risk, and the security-critical
component is never the slow one to patch.

---

## Security posture

* ComfyUI: no published routable port, private bridge, runs as uid 1000, no root.
* Router: `read_only` root filesystem, `cap_drop: ALL`, `no-new-privileges`, uid
  10800, tmpfs `/tmp`, no writes to disk at all.
* Compose **refuses to start** the router if `GX_MEDIA_API_KEY` is unset.
* Request bodies capped at 64 KB; prompts at 4000 chars; every numeric bounded.
* Responses carry `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`,
  `Content-Security-Policy: default-src 'none'`, `Referrer-Policy: no-referrer`.
* Job ids are `uuid4`-derived and route-matched against `[A-Za-z0-9-]{1,64}`;
  no caller-supplied string ever reaches a filesystem path.
* `HF_HUB_OFFLINE=1` on the engine — it has no business calling out.

## Known limitations

* One generation at a time, cluster-wide. That is a property of ComfyUI, not a
  router limitation.
* No cancellation endpoint yet: a submitted job runs to completion or timeout.
* Job state is in memory. A router restart loses job ids; the output files remain
  under `/srv/models/comfy-output/{gx-image,gx-video}`.
* `response_format: "url"` returns a **relative** path. LiteLLM resolves it
  against the configured `api_base`; a direct caller must prepend the host.
* Image-to-image, inpainting and ControlNet are not exposed. Adding one means
  adding a vetted template, not widening the API.
