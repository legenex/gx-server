# gx-music — ACE-Step 1.5 XL on node 2

`gx-music` is the cluster's music generation tier. It runs on **gx10-02** and is
private: users reach it through **gx10-01** (GX-Playground / gateway), never
directly.

| Piece | Where | Role |
|---|---|---|
| `gx-music.service` (supervisor, this directory) | gx10-02, `systemctl --user` | **The only ingress.** `192.168.100.11:18820` (fabric) and `127.0.0.1:18820`. Validation, auth, queue, job/asset store, engine lifecycle, admission guard. Stdlib Python, ~60 MB RSS. Starts at boot; **does not load the model at boot**. |
| `gx-music` container (engine) | gx10-02, `127.0.0.1:18811` | Upstream `acestep.api_server` with the model loaded. Started on the first job, stopped when idle or when gx-max claims the node. |
| Helper containers | gx10-02, `--network none` | `ffmpeg`/`ffprobe` from the engine image: transcode and probe without loading the model. |

## Model — what is installed and why

| Component | Hugging Face repo | Revision (pinned) | Size |
|---|---|---|---|
| DiT (the music model) | `ACE-Step/acestep-v15-xl-turbo` | `d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee` | 18.6 GiB |
| 5 Hz language model (song planner) | `ACE-Step/acestep-5Hz-lm-4B` | `0a3ec94b557aea7d508da38b31cfe7341f6ff737` | 7.8 GiB |
| VAE + text encoder (`vae/`, `Qwen3-Embedding-0.6B/`) | `ACE-Step/Ace-Step1.5` | `19671f406d603126926c1b7e2adc169acbcade22` | 1.4 GiB |
| Runtime | `github.com/ace-step/ACE-Step-1.5` | `ca1e85fe9430179831e6bc6be790c332190a3866` | image `gx-music-engine:acestep15-ca1e85f-t214` |

Checkpoints: `/srv/models/music/acestep/checkpoints/` (node 2 only).

**Why XL turbo + 4B LM.** ACE-Step 1.5 XL has three 4B DiT variants:

* **turbo**: 8 steps, no CFG, "very high" quality, fastest. Tasks: text2music, cover, repaint.
* **sft**: 50 steps with CFG. Same task set as turbo.
* **base**: 50 steps. Adds extract, lego and complete.

The upstream GPU guide recommends the 4B LM for ≥24 GB of memory. Turbo gives the
best quality per second for an interactive product, and it covers generate, remix,
edit and extend.

**Not installed: XL base.** Each XL variant is another ~20 GB. XL base is the
only way to get extract/lego/complete, and installing it is a separate product
decision (disk is no longer the constraint: node 2 has ~328 GB free). Adding it later is a download plus `GX_MUSIC_DIT=…`;
the capability table switches automatically (`validation.Capabilities`).

## Capabilities (exactly what the service accepts)

| Capability | Supported | How |
|---|---|---|
| Natural-language description | yes | `description` → the 5 Hz LM writes caption, lyrics and metadata (`sample_mode`) |
| Prompt / style prompt | yes | `prompt` (≤ 512 chars incl. tags) |
| Style tags | yes | `style_tags[]`, appended to the caption; `GET /v1/music/tags?q=` searches the model's own 178k-term genre vocabulary plus curated groups |
| Lyrics (structured) | yes | `lyrics` (≤ 4096); section tags `[Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro] [Instrumental]` … |
| Instrumental | yes | `instrumental: true` (lyrics become `[Instrumental]`) |
| Vocal language | yes | 50 languages + `unknown` |
| Duration | yes | 10–600 s |
| BPM / key / time signature | yes | 30–300; `C major`, `F# minor`, `Am`; `2/4 3/4 4/4 6/8` |
| Seed / batch | yes | `seed` (item *i* uses `seed+i`), `batch_size` 1–4 |
| Inference steps / sampler | yes | 1–20 (default 8), `ode`/`sde` |
| LM planning ("thinking") | yes | `thinking` (default true), `lm_temperature`, `lm_cfg_scale`, `lm_top_p`, `enhance_prompt` |
| Guidance / CFG / shift | **no** (turbo is distilled) | rejected with 400; enabled automatically for sft/base |
| Reference audio (style/timbre) | yes | `reference: {upload_id | job_id}` |
| Remix / cover | yes | `POST /v1/music/remix` (`strength`, `noise_strength`) |
| Repaint / edit section | yes | `POST /v1/music/edits` (`start`, `end`, `mode`, `strength`, `crossfade`) |
| Extend / continue | yes | `POST /v1/music/extend` (`seconds`, `direction: end|start`), i.e. upstream repaint outpainting |
| Extract stems / lego / complete | **no** | needs XL base (not installed) |
| Output | yes | 48 kHz stereo; WAV 32-bit float master, FLAC 24-bit, MP3 320 kbps |

## API

Auth: `Authorization: Bearer <key>`. The key is in
`/srv/projects/gx-cluster/secrets/gx-music/api-key` (0600, node 2). Only
`/health` is unauthenticated. Errors use a single shape:
`{"error": {"code", "message", "retryable"}}`. Messages are user-safe; details
go to the log.

```
GET    /health
GET    /v1/music/model                      identity, revisions, capabilities, disk, engine state, memory, policy, stats
GET    /v1/music/tags?q=&limit=
POST   /v1/music/load                       503 gx_max_active while gx-max holds node 2
POST   /v1/music/unload                     409 while a track is generating
POST   /v1/music/generations                202 + job
POST   /v1/music/remix                      202 + job   {source:{job_id,index}|{upload_id}, prompt, strength}
POST   /v1/music/edits                      202 + job   {source, start, end, prompt?, lyrics?}
POST   /v1/music/extend                     202 + job   {source, seconds, direction}
POST   /v1/music/uploads                    201 + upload  (raw body, X-Filename header; WAV/FLAC/MP3/OGG/M4A ≤ 64 MB, 1–600 s)
GET    /v1/music/uploads/{upl-…}
GET    /v1/music/jobs?status=active|queued|completed|failed|…&limit=
GET    /v1/music/events?limit=
GET    /v1/music/{mus-…}
GET    /v1/music/{mus-…}/lineage            ancestors + descendant tree
GET    /v1/music/{mus-…}/content?index=0&format=wav|flac|mp3     (Range supported)
POST   /v1/music/{mus-…}/cancel
DELETE /v1/music/{mus-…}                    terminal jobs only
```

Edits, remixes and extends **inherit** the parent's prompt, tags, lyrics and
language when the request leaves them empty.

### Job states (shared vocabulary with GX-Playground)

`queued → waiting_for_resource → loading_model → preparing → generating →
processing → saving → completed | failed | cancelled`

`progress` is the engine's own fraction when it reports one, otherwise `null`.
It is never invented. `detail` is a human sentence. Cancelling a running job
lets the current render finish and then discards it, because upstream has no
task cancel.

Example:

```bash
K=$(cat /srv/projects/gx-cluster/secrets/gx-music/api-key)
curl -sS -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  http://192.168.100.11:18820/v1/music/generations -d '{
    "prompt": "upbeat synth-pop song", "style_tags": ["80s","female vocals"],
    "lyrics": "[Verse]\n...\n[Chorus]\n...", "duration": 60, "bpm": 118, "key": "A minor", "seed": 42}'
```

## Storage

| What | Where (node 2) |
|---|---|
| Job + upload metadata | `/srv/models/music-data/db/gx-music.sqlite3` (WAL) |
| Track files | `/srv/models/music-data/jobs/<job>/track-<i>.{wav,flac,mp3}` |
| Uploads | `/srv/models/music-data/uploads/<upl-…>.<ext>` |
| Engine scratch | `/srv/models/music-data/api_audio/` (emptied as jobs are collected) |
| Supervisor state | `/srv/projects/gx-cluster/state/gx-music/` (engine env file, 0600) |
| Logs | `/srv/logs/gx-music/gx-music.log` (JSON), `access.log` |

The API never returns filesystem paths. Clients get ids and `/content` URLs.

## Lifecycle and memory policy

* **Class `medium`, estimate `GX_MUSIC_ENGINE_ESTIMATE_GIB`** (measured peak in
  `TEST_RESULTS`). Every load goes through `gx_orchestrator.resource_guard`
  (the same formula and ledger as `gx-safe-run.sh`). The reserve is 30 GiB.
* **Refused → `waiting_for_resource`**, retried every 20 s for up to 30 min,
  then `failed / insufficient_memory` (retryable).
* **Idle ComfyUI weights** may be freed before a refused load is retried
  (`GX_MUSIC_EVICT_COMFY_WEIGHTS`, default on). The free goes through the
  media router (`docker exec gx-media-router python -m
  gx_media_router.free_node`), the same path gx-reason's start uses. The
  router refuses while a generation runs or a video is queued, and it clears
  its resident-model record, so its next image or video job is admitted as
  **cold** (60/76 GiB), never as warm (8 GiB). Music never calls ComfyUI
  directly. gx-reason is **never** evicted by music
  (`GX_MUSIC_EVICT_REASON=0`).
* **Maintenance mode** (`state/guard/node2.maintenance-hold`, written by the
  Control Center): no new engine loads; queued jobs wait with the reason
  "Maintenance mode"; a running track finishes, then the engine is unloaded.
* **Pin** (`state/guard/pins.json` has a `gx-music` entry): the idle unload
  is skipped while MemAvailable stays at or above the 30 GiB reserve and no
  gx-max hold or Maintenance is active. A pin never blocks gx-max or another
  tenant's admission.
* **Idle unload**: `docker stop` after `GX_MUSIC_IDLE_UNLOAD_S` (600 s) with
  no queued work. Unload = container removed + ledger released. There is no
  soft unload.
* **gx-max always wins.** The engine will not load, and is torn down within
  15 s (or immediately if it is still loading), when any of these is true:
  * `gx-max-rank1` exists;
  * gx-max's node-2 deadman is alive;
  * `gx-llama-swap-node02` is stopped (the gx-max drain stops it first);
  * `/srv/projects/gx-cluster/state/guard/node2.gxmax-hold` is fresh.

  Queued jobs wait. A job interrupted mid-render fails as `engine_interrupted`
  (retryable).
* **Supervisor stop** unloads the engine; a supervisor restart fails in-flight
  jobs honestly (`interrupted`) and adopts a healthy running engine.

## Operations

```bash
systemctl --user status gx-music
curl -s localhost:18820/health
K=$(cat /srv/projects/gx-cluster/secrets/gx-music/api-key)
curl -s -H "Authorization: Bearer $K" localhost:18820/v1/music/model | python3 -m json.tool
curl -s -H "Authorization: Bearer $K" -X POST localhost:18820/v1/music/unload
docker logs --tail 100 gx-music            # engine (only while loaded)
tail -f /srv/logs/gx-music/gx-music.log
```

Config: `~/.config/gx-music/gx-music.env` (no secrets). Every variable is in
`gx_music/config.py`.

Rebuild the engine (node 2):

```bash
cd legenex/music/engine
docker build -t gx-music-engine:acestep15-ca1e85f-t214 .     # canonical; needs ~20 GB free at peak
../scripts/engine-selftest.sh                                 # GPU arch + cuBLAS + import check
```

**GB10 torch override.** Upstream's `uv.lock` pins torch 2.10.0+cu130. On
GB10 (sm_121) that build fails *every* cuBLAS gemm with
`CUBLAS_STATUS_INVALID_VALUE`, fp32 included, and swapping cuBLAS alone does
not fix it. The image replaces it with the set `gx-comfyui` proved on this
hardware: torch 2.14.0 / torchaudio 2.11.0 / torchvision 0.29.0 (cu130).
torchao's C++ extensions are then skipped; gx-music does not use torchao
quantization. On 2026-09-17 node 2 was then too full for the canonical build, so
`Dockerfile.gb10-overlay` applied the same override on top of the base build;
the running image `gx-music-engine:acestep15-ca1e85f-t214` came from it.

## Tests

```bash
cd legenex/music && ./qa.sh                        # hermetic: no GPU, no Docker daemon
python3 scripts/live-acceptance.py --out /tmp/e.json   # REAL generations (node 2)
```

## Security

* The supervisor is the only routable listener and refuses wildcard binds.
* The engine is on host loopback behind its own random key: upstream exposes
  `/v1/audio?path=`, training routes and model switching, which must never be
  reachable.
* Uploads are sniffed by magic bytes, size-capped, decoded by `ffprobe` in a
  network-less container, and stored under generated names. Client filenames
  are metadata only.
* Source references are ids validated by regex and resolved server-side.
  Engine-returned paths are checked to stay under the data root.
* The engine runs as uid 1000 with a `--memory` cap and `--oom-score-adj 900`.
  Helper containers use `--network none`, 4 GiB and 4 CPUs.
