# gx-voice and the Voice Studio

`gx-voice` is the cluster's speech tier (Build V3, D-040): voiceovers,
narration, advertising reads, character voices, voice design, authorised voice
cloning and saved voices. It runs on **gx10-02** and is never reachable from a
browser: users reach it through the Voice page in GX-Playground, the
`/v1/voice/*` API on the Playground, or `POST /v1/audio/speech` with
`model: "gx-voice"` on the LiteLLM gateway.

| | |
|---|---|
| Preset and saved-preset voices | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` @ `0c0e3051f131929182e2c023b9537f8b1c68adfe` |
| Voice design from a description | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` @ `5ecdb67327fd37bb2e042aab12ff7391903235d3` |
| Cloning, and every saved designed or cloned voice | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` @ `fd4b254389122332181a7c3db7f27e918eec64e3` |
| Speech tokenizer | `Qwen/Qwen3-TTS-Tokenizer-12Hz` @ `7dd38ad4e9bad454aae9cd937d0cd577604fe229`, bundled byte-identical in all three checkpoints |
| Licence | apache-2.0 (all four), public, not gated |
| Runtime | `qwen-tts` 0.1.1 (`github.com/QwenLM/Qwen3-TTS` @ `022e286b`), image `gx-voice-engine:qwen3tts-022e286-t214`: torch 2.14.0 / torchaudio 2.11.0 cu130, transformers 4.57.3, **PyTorch SDPA attention** (FlashAttention 2 has no aarch64/sm_121 wheel) |
| Node | gx10-02. The supervisor starts at boot and loads nothing; the engine loads with the first job and unloads after 10 idle minutes |
| Measured memory | **9.5 GiB peak** during a cold load, **6.5 GiB resident** with one model variant; MemAvailable returns in full on unload (see "Measured performance") |

`openai/whisper-large-v3-turbo` is also installed on gx10-02. It is **not**
part of the service — `legenex/voice/scripts/asr-check.py` uses it on the CPU
to prove that acceptance takes really say the requested words.

## What it can do

* **Text to speech** with nine preset speakers: `aiden`, `ryan` (English),
  `vivian`, `serena`, `uncle_fu`, `dylan`, `eric` (Chinese), `ono_anna`
  (Japanese), `sohee` (Korean). Any speaker can read any supported language.
* **Emotion and style instructions** in plain words ("shout this urgently",
  "slow, calm, almost whispering"). Honoured by preset voices and voice
  design; a cloned or designed voice renders on the Base model, which has no
  instruction channel, so the job's `notes` say the instructions were not
  applied — never silently.
* **Voice design**: describe a voice ("a warm middle-aged British female
  narrator…") and the model invents a speaker.
* **Authorised cloning** from a 2-60 s reference recording, with a stored
  permission record.
* **Saved voices**: a designed take or an uploaded reference becomes a named,
  versioned voice that sounds the same every time.
* **Dialogue**: 1-60 lines, each with its own voice and instructions.
* **Takes** (1-4 per job, take *i* uses `seed + i`), `seed`, `speed`
  (0.5-2.0, pitch preserving), `pause_ms` and the sampling controls.
* **Output**: 24 kHz mono WAV and MP3 in the Studio; the OpenAI speech
  endpoint also serves FLAC, Opus, AAC and raw PCM.
* **Languages**: `auto`, English, Chinese, German, French, Spanish, Italian,
  Portuguese, Russian, Japanese, Korean.

## Where things live

| Piece | Where |
|---|---|
| Supervisor (the only ingress) | gx10-02, `gx-voice.service` (`systemctl --user`), `192.168.100.11:18830` + `127.0.0.1:18830`, bearer key `/srv/projects/gx-cluster/secrets/gx-voice/api-key` (0600, both nodes) |
| Engine container | gx10-02, `gx-voice-engine`, loopback `127.0.0.1:18831`, its own random key, label `gx.workload=gx-voice` |
| Checkpoints | `/srv/models/voice/` on gx10-02 only (no shared filesystem) |
| Voices, jobs, takes, consents | the application database, migration `040_voice.sql` (`voice_voices`, `voice_jobs`, `voice_takes`, `voice_consents`, `voice_voice_versions`) |
| Rendered takes on gx10-01 | `<media>/voice/<job id>/take-<n>.{wav,mp3}` |
| Service object | `App.voice` (`gx_control_ui/voice.py`), routes in `gx_control_ui/routes_voi.py` |
| Browser page | `legenex/playground/web/js/pages/voice.js` (Create → Voice) |
| Logs | gx10-02: `/srv/logs/gx-voice/{gx-voice,access}.log` and `journalctl --user -u gx-voice` |

## Lifecycle and memory

* **gx-max always wins.** The supervisor watches the same four signals as
  gx-music (`gx-max-rank1`, the rank-1 deadman pid, a stopped
  `gx-llama-swap-node02`, and a fresh `node2.gxmax-hold`). While any of them
  is set it starts nothing and says so; the drain's
  `POST /v1/voice/unload` removes the container, releases the ledger entry
  and leaves no `gx_voice_engine` process.
* **Maintenance** blocks new loads the same way.
* **Admission.** Every load goes through the shared node-2 guard
  (`legenex/orchestrator` `resource_guard.guard_launch`, class `small`), which
  keeps `MemAvailable − estimate − other tenants' pending growth ≥ 30 GiB`.
  The estimate is `GX_VOICE_ENGINE_ESTIMATE_GIB` (12 GiB, deliberately above
  the measured 9.5 GiB peak).
* **Pending memory (D-038).** `GET /health` publishes
  `memory.{estimate_gib, resident_gib, pending_gib, reserve_gib}`. Other node-2
  tenants subtract `pending_gib` before they admit their own load, and
  gx-voice subtracts theirs (media router :18800, gx-music :18820).
* **Making room.** If memory is short, gx-voice asks the media router to free
  idle ComfyUI weights (`gx_media_router.free_node`) or unloads an idle
  gx-music through its own `{"if_idle": true}` endpoint. It never stops another
  tenant's container and never calls ComfyUI `/free` directly (D-036).
* **Variant policy.** One model variant is resident by default
  (`GX_VOICE_MAX_RESIDENT`); switching evicts the least recently used one.
  A job's lines are grouped by variant so a mixed dialogue switches as rarely
  as possible, and the audio is still assembled in script order.
* **Idle unload** after 600 s, unless the workload is pinned in Resource
  Control and MemAvailable is above the reserve.

## Measured performance (gx10-02, 2026-09-17)

Measured by `legenex/voice/scripts/measure-footprint.py`, sampling
`MemAvailable` at 1 Hz through a cold load; evidence:
`/srv/logs/acceptance/build-v3/voi/footprint-20260917T191026Z/`.

| | |
|---|---|
| Baseline MemAvailable | 111.51 GiB |
| Minimum while loaded | 102.06 GiB |
| **Peak growth (cold, incl. the load transient)** | **9.45 GiB** |
| Engine container with no model | 0.48 GiB |
| **Steady resident, one variant** | **6.5 GiB** (the supervisor reports 6.3-6.9 GiB) |
| MemAvailable after unload | 111.50 GiB (fully returned) |
| **Cold start** (container 3.5 s + first variant load 31.5 s) | **35.0 s** |
| Warm variant switch | 26-28 s |
| Unload | 3.8 s |
| First audio | 6.0-8.7 s for an 8.2-8.6 s take |
| Real-time factor | 0.72-1.00 (≈ 0.73 warm) |

## Using it

### Voice page (GX-Playground → Create → Voice)

1. Pick a voice: a preset speaker, or one of your saved voices.
2. Type the script. Blank lines start a new paragraph (a `pause_ms` gap).
3. Optional: instructions ("bright and upbeat, slightly rushed"), language,
   takes, seed, speed.
4. **Generate.** The job shows its status (queued → loading the model →
   speaking → saving → completed) and, if gx10-02 is busy, why it is waiting.
5. Play a take in the browser, download it as WAV or MP3, or **Save to
   Library** (idempotent: saving the same take twice returns the same asset).

To design a voice, switch to *Design*, describe the voice, generate, and
**Save as voice**. To clone, upload a 2-60 s recording, confirm that you have
the speaker's permission, and save it as a voice.

### Control Center

Resource Control shows gx-voice on gx10-02 with its measured footprint, its
pin, and Load / Unload. Lifecycle is Control Center only — the Playground's
`/api/voice/load|unload` answers 403.

### API

See `legenex/playground/API.md` (Voice). Briefly:

```bash
# OpenAI-compatible, through the gateway
curl -sS http://100.105.214.61:4000/v1/audio/speech \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gx-voice","voice":"aiden","input":"Hello from gx-voice.","response_format":"mp3"}' -o speech.mp3

# Voice API on the Playground (a gateway key that allows gx-voice)
curl -sS http://100.105.214.61:8090/v1/voice/speech \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"voice_id":"preset:ryan","text":"Hello.","language":"english"}'
```

## Consent and privacy

* Cloning requires `consent.confirmed: true` with a statement. Without it the
  request is refused with **403 `consent_required`** — proven in acceptance.
* The confirmation is stored in `voice_consents` with the user, the time, the
  client address, the statement and the **SHA-256 of the exact recording**, and
  is linked from every job and voice that used it.
* Reference uploads are sniffed by magic bytes, size-capped (32 MB), decoded
  and level-checked on gx10-02, and stored under content-derived names. The
  client filename is metadata only.
* `POST /v1/audio/speech` keeps no copy of the audio after answering.
* Prompts and transcripts never appear in metrics lines (the shared
  `gxcommon.metrics` helper drops those keys).

## Operations (gx10-02)

```bash
systemctl --user status gx-voice
curl -s localhost:18830/health | python3 -m json.tool
K=$(cat /srv/projects/gx-cluster/secrets/gx-voice/api-key)
curl -s -H "Authorization: Bearer $K" localhost:18830/v1/voice/model | python3 -m json.tool
curl -s -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
     -X POST -d '{"if_idle":true}' localhost:18830/v1/voice/unload
docker logs --tail 100 gx-voice-engine      # only while loaded
tail -f /srv/logs/gx-voice/gx-voice.log
```

Install or update the unit (the node-2 checkout is a pull-only mirror):

```bash
ln -sf ~/Documents/Projects/Server/gx-cluster/legenex/voice/systemd/gx-voice.service ~/.config/systemd/user/
mkdir -p ~/.config/gx-voice
cp -n ~/Documents/Projects/Server/gx-cluster/legenex/voice/systemd/gx-voice.env.example ~/.config/gx-voice/gx-voice.env
systemctl --user daemon-reload && systemctl --user enable --now gx-voice
```

**Rollback:** `systemctl --user disable --now gx-voice` (stopping the
supervisor unloads the engine). The Voice page then reports the service as
unreachable and queues nothing; the LiteLLM `gx-voice` entry can stay and its
requests fail with 503.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Voice page: "the voice service is not reachable" / `/api/voice/model` 503 `node_unavailable` | `gx-voice.service` is not running on gx10-02. `systemctl --user status gx-voice`, then `journalctl --user -u gx-voice`. |
| Jobs stay `waiting_for_resource` | gx-max, Maintenance, or another node-2 tenant holds the memory. The job's `waiting.reason` names it; the engine loads as soon as the reserve fits. |
| A job says instructions were not applied | The line ran on the Base model (a cloned or designed saved voice). Use a preset voice, or a voice design job, for style words. |
| `gx-voice` missing from `/v1/models` on the gateway | The running LiteLLM container predates the `gx-voice` entry in `legenex/gateway/litellm/config.yaml`; recreate it (lead). |
| The engine will not start | `legenex/voice/scripts/engine-selftest.sh` on gx10-02 checks CUDA, the Blackwell kernels and the `qwen_tts` import through the admission guard. |

## Tests

```bash
cd legenex/voice && ./qa.sh                    # 46 hermetic tests: no GPU, no Docker, no model
cd legenex/control-ui && .venv/bin/python -m unittest discover -s tests -p 'test_voice.py'   # 19 tests
python3 legenex/voice/scripts/live-acceptance.py --out /srv/logs/acceptance/build-v3/voi/live-<UTC>
python3 legenex/voice/scripts/measure-footprint.py --out /srv/logs/acceptance/build-v3/voi/footprint-<UTC>   # on gx10-02
```
