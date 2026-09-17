# gx-voice — Qwen3-TTS 12Hz 1.7B on node 2

`gx-voice` is the cluster's speech tier (Build V3, D-040): voiceovers,
narration, advertising reads, character voices, voice design, voice cloning
and saved voices. It runs on **gx10-02** and is private. Users reach it
through **gx10-01**: the Voice Studio in GX-Playground, the `/v1/voice/*` API,
and the OpenAI-compatible `POST /v1/audio/speech` on the LiteLLM gateway.

| Piece | Where | Role |
|---|---|---|
| `gx-voice.service` (supervisor, this directory) | gx10-02, `systemctl --user` | **The only ingress.** `192.168.100.11:18830` (fabric) and `127.0.0.1:18830`. Validation, the model router, the queue, job/reference store, saved-voice replica, engine lifecycle and the node-2 admission guard. Stdlib Python. Starts at boot and **loads no model at boot**. |
| `gx-voice-engine` container (`engine/`) | gx10-02, `127.0.0.1:18831` | `gx_voice_engine.py`: the Qwen3-TTS model objects behind a loopback HTTP server with its own random key. Started on the first job, stopped when idle or when gx-max or Maintenance claims the node. |
| ffmpeg helper | gx10-02 | `docker exec` into the running engine, otherwise a throwaway `--network none` container of the same image: decode references, time stretch, MP3/FLAC/Opus/AAC encoding. |
| Voice Studio / API | gx10-01 | `legenex/control-ui/gx_control_ui/voice.py` (`App.voice`), `routes_voi.py`, migration `040_voice.sql`, `legenex/playground/web/js/pages/voice.js`. |

## Models — what is installed and why

Verified on the Hugging Face API and with `legenex/scripts/hf-verify.py`
(manifests in each folder). All apache-2.0, public, not gated. Files live in
`/srv/models/voice/` (node 2 only).

| Router variant | Repository | Revision | Size | Used for |
|---|---|---|---|---|
| `custom` | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | `0c0e3051f131929182e2c023b9537f8b1c68adfe` | 4.21 GiB | the nine preset speakers with style / emotion instructions |
| `design` | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | `5ecdb67327fd37bb2e042aab12ff7391903235d3` | 4.21 GiB | a new voice from a text description |
| `base` | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | `fd4b254389122332181a7c3db7f27e918eec64e3` | 4.23 GiB | cloning from reference audio; every saved designed or cloned voice |
| (tokenizer) | `Qwen/Qwen3-TTS-Tokenizer-12Hz` | `7dd38ad4e9bad454aae9cd937d0cd577604fe229` | 0.64 GiB | bundled byte-identical (sha256 `836b7b35…`) as `speech_tokenizer/` in all three; the four copies are hard links |
| Runtime | `github.com/QwenLM/Qwen3-TTS` (`qwen-tts` 0.1.1) | `022e286b98fbec7e1e916cb940cdf532cd9f488e` | image `gx-voice-engine:qwen3tts-022e286-t214` | torch 2.14.0 / torchaudio 2.11.0 cu130 (the GB10 set), transformers 4.57.3, PyTorch SDPA attention (FlashAttention 2 has no aarch64/sm_121 wheel) |

`openai/whisper-large-v3-turbo` @ `41f01f3fe87f28c78e2fbf8b568835947dd65ed9`
(MIT, 1.5 GiB) is also in `/srv/models/voice/`. It is **not** part of the
service: `scripts/asr-check.py` uses it on the CPU to prove that acceptance
takes say the requested words.

**Router.** Each line of a job goes to exactly one variant:

| Voice spec | Variant |
|---|---|
| `{"kind": "preset", "speaker": …}` | `custom` |
| `{"kind": "design", "description": …}` | `design` |
| `{"kind": "reference", "reference_id": …}` | `base` |
| `{"kind": "saved", "voice_id": …}` | whatever the saved voice is (preset → `custom`, designed/cloned → `base`) |

A **designed voice** is saved as its chosen VoiceDesign take plus the preview
text. New lines use `base` with that clip, so the voice sounds the same every
time (re-running VoiceDesign with the same description gives a different
speaker). **Instructions** (style, emotion, pacing words) are honoured by
`custom` and `design` only; for `base` lines the job's `notes` say they were
not applied.

**No weights per voice.** A voice is a reference clip
(`references/ref-<sha256[:32]>.wav`, 24 kHz mono) plus a cached clone prompt
(`prompts/<ref-id>/<sha256>.pt`: the speaker embedding and the reference codec
codes).

**Variant memory policy.** At most `GX_VOICE_MAX_RESIDENT` variants (default
**1**) are resident in the engine; switching evicts the least recently used
one first. With more than one resident they share one speech tokenizer. A
job's lines are grouped by variant, starting with the resident one, so a
mixed dialogue switches as few times as possible; the audio is still
assembled in script order.

## Capabilities

| | |
|---|---|
| Operations | `tts`, `voice_design`, `voice_clone`, `dialogue` (1-60 lines, each with its own voice) |
| Script | up to 10 000 characters per line, 20 000 per job; blank lines are paragraphs |
| Chunking | sentences grouped into ≤ 300-character chunks; 140 ms breath inside a paragraph, `pause_ms` (0-5000, default 350) between paragraphs and lines; 8 ms fades at every join |
| Takes | 1-4; take *i* uses `seed + i`; chunk *j* of a take derives its seed deterministically |
| Languages | auto, English, Chinese, German, French, Spanish, Italian, Portuguese, Russian, Japanese, Korean |
| Preset speakers | aiden, ryan, vivian, serena, uncle_fu, dylan, eric, ono_anna, sohee |
| Reference clips | WAV, FLAC, MP3, OGG, M4A, WebM; 2-60 s; ≤ 32 MB; content-addressed (the same file is stored once) |
| Controls | `instructions`, `language`, `speed` 0.5-2.0 (ffmpeg `atempo`, pitch preserved), `pause_ms`, `temperature`, `top_p`, `top_k`, `repetition_penalty` |
| Output | 24 kHz mono 16-bit WAV and 192 kbps MP3 per take; the speech endpoint also gives FLAC, Opus (Ogg), AAC (ADTS) and raw PCM |
| Safety checks | silence (< -60 dBFS), too short (< 0.2 s) and runaway lines (reaching the token cap) fail the job honestly; each chunk's `max_new_tokens` = 160 + 6 × characters (≤ 8192) |
| Not supported | true streaming (the Python runtime renders a whole chunk at a time); instructions on cloned/designed voices (the model has none); FlashAttention |

## API

Auth: `Authorization: Bearer <key>` from
`/srv/projects/gx-cluster/secrets/gx-voice/api-key` (0600, both nodes). Only
`/health` is open. Errors: `{"error": {"code", "message", "retryable"}}`.

```
GET    /health                                      state, memory (D-038), queue, waiting reason
GET    /v1/models                                   [gx-voice]
POST   /v1/audio/speech                             OpenAI: {model, input, voice, response_format?, speed?, instructions?, language?, seed?}
GET    /v1/voice/model                              identity, variants, speakers, languages, limits, engine, policy
POST   /v1/voice/load     {"variant"?}              503 while gx-max or Maintenance holds node 2
POST   /v1/voice/unload   {"if_idle"?}              409 while a job runs; with if_idle also while jobs are queued or a pin is honoured
POST   /v1/voice/jobs                               202 + job
GET    /v1/voice/jobs?status=&limit=
GET    /v1/voice/jobs/{vox-…}
GET    /v1/voice/jobs/{vox-…}/content?take=&format=wav|mp3|flac|opus|aac|pcm   (Range supported)
POST   /v1/voice/jobs/{vox-…}/cancel                a running job stops after the current line
DELETE /v1/voice/jobs/{vox-…}                       terminal jobs only
POST   /v1/voice/references                         raw body, X-Filename → {"id": "ref-…", "duration_s", …}
GET    /v1/voice/references/{ref-…}
DELETE /v1/voice/references/{ref-…}                 409 while a saved voice uses it
GET    /v1/voice/voices                             saved-voice replica
PUT    /v1/voice/voices/{vc_…}                      upsert {name, voice, instructions, language, version}
DELETE /v1/voice/voices/{vc_…}
GET    /v1/voice/events?limit=
```

Job body (the gx10-01 Control Center builds it):

```json
{"operation": "dialogue", "title": "Promo", "language": "english", "takes": 2, "seed": 42,
 "speed": 1.0, "pause_ms": 400, "sampling": {"temperature": 0.9},
 "segments": [
   {"text": "Welcome back!", "voice": {"kind": "preset", "speaker": "ryan"}, "instructions": "excited"},
   {"text": "Glad to be here.", "voice": {"kind": "saved", "voice_id": "vc_…"}, "pause_ms": 250}],
 "client_ref": "vj_…"}
```

Job states: `queued → waiting_for_resource → loading_model → generating →
processing → completed | failed | cancelled`. `progress` is the fraction of
rendered lines. `timings`: `queued_s`, `engine_start_s`, `resource_wait_s`,
`variant_load_s`, `generate_s`, `first_audio_s` (from the first line render
to the first finished chunk of take 1), `audio_s`, `rtf` (model seconds per
second of speech), `postprocess_s`, `total_s`.

`/v1/audio/speech` renders one take, returns the whole file and deletes the
audio on gx10-02 at once (the job row stays, marked `audio_removed`). Voice
names resolve as: a `vc_…` id, a preset speaker (`Uncle Fu`, `preset:uncle_fu`),
or a unique saved-voice name. OpenAI names such as `alloy` are refused.

## Storage (node 2)

| What | Where |
|---|---|
| Jobs, references, voice replica, events | `/srv/models/voice-data/db/gx-voice.sqlite3` (WAL) |
| Takes | `/srv/models/voice-data/jobs/<vox-…>/take-<i>.{wav,mp3,…}`; terminal jobs older than 14 days are removed |
| Reference clips | `/srv/models/voice-data/references/ref-….{orig.<ext>,wav}` |
| Clone prompt cache | `/srv/models/voice-data/prompts/<ref-id>/<sha256>.pt` |
| Models | `/srv/models/voice/` |
| Supervisor state | `/srv/projects/gx-cluster/state/gx-voice/` (engine env file, 0600) |
| Logs | `/srv/logs/gx-voice/gx-voice.log` (JSON), `access.log` |

gx10-01 keeps the downloaded takes in `/srv/projects/gx-cluster/media/voice/`
and saved takes in the Media Library.

## Lifecycle and memory (D-038)

* **Class `small`, estimate `GX_VOICE_ENGINE_ESTIMATE_GIB`** (default 12,
  measured, see `coordination/build-v3/voi.md` §2). Every engine start goes
  through `gx_orchestrator.resource_guard` with the locked 30 GiB reserve
  (`GX_GUARD_RESERVE_GIB` can only be raised).
* **Other tenants' pending memory counts.** Before a start the supervisor
  reads the open `/health` of the media router (18800) and gx-music (18820)
  and adds their `memory.pending_gib` to its own estimate.
* **Making room (sanctioned paths only).** If the start is refused, it tries
  once to free idle memory: an idle gx-music engine through
  `POST /v1/music/unload {"if_idle": true}` (only if that frees enough, never
  under the Music, Maintenance or Max profile), otherwise idle ComfyUI weights
  through `docker exec gx-media-router python -m gx_media_router.free_node`
  (never under the Media profile). It never stops another tenant's container.
* **Published on `/health`:** `state` (`unloaded | loading | ready | busy |
  unloading | error`), `memory.estimate_gib`, `memory.resident_gib`
  (measured from MemAvailable while loaded), `memory.pending_gib`,
  `queue`, `waiting` (code, reason, since), `pinned`, `pin_honoured`,
  `idle_seconds`, `blocked_by`.
* **Waiting:** a refused job is `waiting_for_resource` with a numeric reason,
  retried every 15 s for up to 30 min, then `failed / insufficient_memory`.
* **gx-max always wins.** No load, and an unload within one 15 s reaper
  tick, while `gx-max-rank1` exists, the rank-1 deadman lives,
  `gx-llama-swap-node02` is stopped, or `state/guard/node2.gxmax-hold` is
  fresh. A job interrupted mid-render fails as `engine_interrupted`
  (retryable).
* **Maintenance** (`node2.maintenance-hold`): no new loads; a running job
  finishes, then the engine is unloaded.
* **Pin** (`pins.json` key `gx-voice`): skips the idle unload while
  MemAvailable ≥ the reserve and no hold is active.
* **Idle unload** after `GX_VOICE_IDLE_UNLOAD_S` (600 s): container removed,
  ledger released, MemAvailable re-read.
* **Restart:** a supervisor restart fails in-flight jobs as `interrupted`
  and adopts a healthy running engine; stopping the supervisor unloads the
  engine.
* **Drain check** for `legenex/lifecycle/node2-holds.sh`: no
  `gx-voice-engine` container, no `"gx-voice"` in
  `node2-residency.json`, `pgrep -fc gx_voice_engine` = 0.

## Operations (gx10-02)

```bash
systemctl --user status gx-voice
curl -s localhost:18830/health | python3 -m json.tool
K=$(cat /srv/projects/gx-cluster/secrets/gx-voice/api-key)
curl -s -H "Authorization: Bearer $K" localhost:18830/v1/voice/model | python3 -m json.tool
curl -s -H "Authorization: Bearer $K" -X POST localhost:18830/v1/voice/unload
docker logs --tail 100 gx-voice-engine          # only while loaded
tail -f /srv/logs/gx-voice/gx-voice.log
```

Install or update the unit (the node-2 checkout is a pull-only mirror):

```bash
ln -sf ~/Documents/Projects/Server/gx-cluster/legenex/voice/systemd/gx-voice.service ~/.config/systemd/user/
mkdir -p ~/.config/gx-voice && cp -n ~/Documents/Projects/Server/gx-cluster/legenex/voice/systemd/gx-voice.env.example ~/.config/gx-voice/gx-voice.env
systemctl --user daemon-reload && systemctl --user enable --now gx-voice
```

Build and check the engine image:

```bash
docker build -t gx-voice-engine:qwen3tts-022e286-t214 legenex/voice/engine
legenex/voice/scripts/engine-selftest.sh       # through the admission guard; CUDA, Blackwell kernels, qwen_tts import
```

Rollback: `systemctl --user disable --now gx-voice` (the engine is removed on
stop); the Control Center then reports the voice service as unreachable and
queues nothing. The LiteLLM `gx-voice` entry can stay (requests fail with 503).

## Tests

```bash
cd legenex/voice && ./qa.sh                    # hermetic: no GPU, no Docker daemon, no model
python3 scripts/live-acceptance.py --out /srv/logs/acceptance/build-v3/voi/run.json   # REAL generations (gx10-02)
```

`tests/voice_fakes.py` wires the real supervisor to a stub engine; the
Control Center tests and the GX-Playground browser suite use it
(`legenex/control-ui/e2e/voice_stub.py`), so they exercise the real node-2 API.

## Security and privacy

* The supervisor is the only routable listener and refuses wildcard binds;
  the engine is on host loopback behind its own key.
* Reference uploads are sniffed by magic bytes, size-capped, decoded by
  ffmpeg in the engine image, normalised, level-checked and stored under
  content-derived names. Client filenames are metadata only.
* All paths come from validated ids; engine paths are checked to stay under
  the data root.
* Cloning requires a stored permission confirmation on gx10-01 (who, when,
  from where, and the SHA-256 of the recording).
* The OpenAI speech endpoint does not keep the audio.
* The engine runs as uid 1000 with `--memory 32g` and `--oom-score-adj 900`;
  helper containers use `--network none`.
