# VOI — gx-voice (Qwen3-TTS 1.7B) and Voice Studio

Owner: voice specialist. Status: **in progress** (the interface below is final;
evidence sections are filled in as they are measured).

## 1. Interfaces (stable, code against these)

### 1.1 Python service object for Creative Flows (FLO)

`App.voice` is a `gx_control_ui.voice.VoiceStudio`. Every method is
synchronous, thread-safe, validates its input and raises
`gx_control_ui.voice.VoiceError` (`.status` HTTP code, `.code` machine code,
`str(exc)` user-safe message). Nothing returns a node-2 path or key.

```python
studio = app.voice

studio.model() -> dict
    # {"alias": "gx-voice", "node": "gx10-02", "state": "unloaded|loading|ready|busy|error",
    #  "variants": {"custom": {...}, "design": {...}, "base": {...}},
    #  "speakers": [{"id": "ryan", "label": "Ryan", "description": ..., "native_language": ...}, ...],
    #  "languages": ["auto", "english", ...], "limits": {...}}

studio.list_voices(*, include_presets: bool = True) -> list[dict]      # saved + built-in presets
studio.get_voice(voice_id: str) -> dict                                  # "vc_<24 hex>" or "preset:<speaker>"
studio.create_voice(body: dict, *, user: str, ip: str = "") -> dict
    # body.kind == "preset":   {"name", "speaker", "instructions"?, "language"?, "description"?, "style"?}
    # body.kind == "designed": {"name", "description", "job_id", "take": int}   (a completed voice_design take)
    # body.kind == "cloned":   {"name", "reference_asset_id": "a_…", "transcript"?: str,
    #                           "consent": {"confirmed": true, "statement": str}, "description"?, "style"?}
studio.update_voice(voice_id, body, *, user) -> dict                      # name/description/instructions/style; new version
studio.delete_voice(voice_id, *, user) -> dict
studio.voice_versions(voice_id) -> list[dict]

studio.submit(body: dict, *, user: str, via: str = "ui", ip: str = "") -> job   # via: ui|playground|api|flow
studio.get(job_id: str) -> job
studio.list_jobs(*, limit=50, status=None, voice_id=None) -> list[job]
studio.cancel(job_id, *, user) -> job
studio.wait(job_id, *, timeout: float = 900, poll: float = 1.0) -> job   # blocks until terminal (flows)
studio.save_take(job_id, take: int, *, user, title: str | None = None,
                 flow: dict | None = None) -> asset                         # MediaLibrary asset dict (idempotent)
studio.take_file(job_id, take: int, fmt: str = "wav") -> pathlib.Path      # local file on gx10-01
studio.upload_reference(data: bytes, filename: str, content_type: str, *, user, title=None) -> asset
```

**Job body (`submit`)** — one of four operations; unknown fields are rejected:

| field | type | used by |
|---|---|---|
| `operation` | `tts` \| `voice_design` \| `voice_clone` \| `dialogue` | all |
| `text` | str, 1–10 000 chars | tts, voice_design, voice_clone |
| `voice_id` | `vc_…` or `preset:<speaker>` | tts |
| `description` | str ≤ 1 000 (the voice to design) | voice_design |
| `reference` | `{"asset_id": "a_…", "transcript"?: str, "consent": {"confirmed": true, "statement": str}}` | voice_clone |
| `segments` | `[{"voice_id", "text", "instructions"?, "pause_ms"?}]`, 1–60 | dialogue |
| `instructions` | str ≤ 500: style, emotion, delivery, pacing words | tts (preset voices), voice_design |
| `language` | `auto` or a model language (`english`, `german`, …) | all |
| `takes` | 1–4 (default 1); take *i* uses `seed + i` | tts, voice_design, voice_clone |
| `seed` | 0–2 147 483 646 (random if omitted; always recorded) | all |
| `speed` | 0.5–2.0 (pitch-preserving time stretch after synthesis) | all |
| `pause_ms` | 0–5 000, silence between paragraphs / dialogue lines (default 350) | all |
| `temperature`, `top_p`, `top_k`, `repetition_penalty` | model sampling | all |
| `title` | str ≤ 200 | all |
| `auto_save` | bool (default false): save every take to the Library when done | all |
| `flow` | `{"flow_id", "flow_run_id", "flow_node_id"}` provenance (stored on the job and on saved assets) | all |

The router picks the model per segment: preset voice → **CustomVoice**;
`voice_design` → **VoiceDesign**; cloned or designed saved voice and
`voice_clone` → **Base** (reusable clone prompt, cached on gx10-02 per
reference). `instructions` are applied only by CustomVoice and VoiceDesign;
for Base segments the job carries a `notes[]` entry saying they were not
applied (never silently).

**Job dict**

```json
{"id": "vj_<32 hex>", "operation": "tts", "status": "queued|waiting_for_resource|loading_model|generating|processing|saving|completed|failed|cancelled",
 "detail": "human sentence", "progress": 0.5, "title": "...", "voice_id": "vc_…",
 "created_at": 0.0, "started_at": 0.0, "finished_at": 0.0,
 "takes": [{"index": 0, "seed": 42, "duration_s": 4.2, "sample_rate": 24000, "rms_dbfs": -21.3,
            "waveform": [[min, max], ...], "asset_id": "a_…|null", "audio_url": "/api/voice/jobs/<id>/takes/0/audio"}],
 "timings": {"queued_s", "model_load_s", "generate_s", "first_audio_s", "audio_s", "rtf"},
 "notes": [], "error": {"code", "message", "retryable"} | null,
 "waiting": {...resource explanation...} | null, "request": {...}, "flow": {...} | null, "via": "flow"}
```

`completed` means every take is on gx10-01 and playable (and, with
`auto_save`, in the Library). Library assets use `type="audio"`,
`operation` = `tts` | `voice_design` | `voice_clone` (dialogue → `tts`),
`source_kind="voice_take"`, `source_ref="<job_id>#<take>"`, and the
`flow_*` columns from `flow`.

### 1.2 HTTP APIs

* Browser (session + CSRF): `/api/voice/*` — see `legenex/playground/API.md`.
* API clients (gateway key allowing `gx-voice`): `/v1/voice/*` on
  GX-Playground `:8090`, and OpenAI-compatible `POST /v1/audio/speech` with
  `model: "gx-voice"` on the LiteLLM gateway `:4000`.

### 1.3 Node-2 supervisor contract (D-038) — for PLT (holds, router/music tenant accounting, Resource Control)

| Item | Value |
|---|---|
| Unit | `gx-voice.service` (`systemctl --user`, gx10-02), symlink into the node-2 checkout |
| Listener | `192.168.100.11:18830` + `127.0.0.1:18830`, bearer key `secrets/gx-voice/api-key` |
| Engine container | `gx-voice-engine` (label `gx.workload=gx-voice`), loopback `127.0.0.1:18831`, process marker `gx_voice_engine` |
| Ledger name | `gx-voice` (class `small`) in `state/guard/node2-residency.json` |
| Holds honoured | `node2.gxmax-hold`, `node2.maintenance-hold`, `gx-max-rank1`, rank-1 deadman, stopped `gx-llama-swap-node02` |
| Pin | `pins.json` key `gx-voice`; honoured only while MemAvailable ≥ reserve and no hold |

`GET /health` (open, no secrets):

```json
{"status": "ok", "service": "gx-voice", "version": "1.0.0",
 "state": "unloaded|loading|ready|busy|error", "engine": "unloaded|loading|ready|unloading|failed",
 "variants_loaded": ["custom"], "busy": false, "pinned": false, "pin_honoured": false,
 "idle_seconds": 12.0, "idle_unload_after_s": 600,
 "queue": {"active": 0, "waiting": 0}, "waiting": null | {"code", "reason", "since"},
 "blocked_by": null | "reason",
 "memory": {"estimate_gib": E, "resident_gib": R|null, "pending_gib": P, "reserve_gib": 30}}
```

`pending_gib`: while loading, the estimate minus what already left
MemAvailable; when ready, the estimate minus the measured resident size
(generation and variant-switch headroom); otherwise 0. The supervisor
subtracts the media router's and gx-music's `pending_gib` before it
admits a load; the router and gx-music should subtract gx-voice's.

`POST /v1/voice/unload` with `{"if_idle": true}` (bearer key): refuses
(409) while a job runs, jobs are queued or a pin is honoured; otherwise
unloads (container removed, ledger released, MemAvailable re-read) and
returns `{"reason", "seconds", "mem_available_before_gib",
"mem_available_after_gib", "container_gone": true}`; `{"noop": true}` when
nothing is loaded.

Drain check for `node2-holds.sh` (same shape as music):
container `gx-voice-engine` absent, no `"gx-voice"` in the ledger,
`pgrep -fc 'gx_voice_engine'` = 0.

## 2. Evidence, footprints, limitations, blockers

(filled in below as measured)

Deployed and measured on 2026-09-17 (21:08-21:33 SAST). Everything below is
from a real run; nothing is estimated.

### 2.1 Deployment state

| Item | State |
|---|---|
| Engine image | **already built** on gx10-02 before this session: `gx-voice-engine:qwen3tts-022e286-t214`, 9.63 GB. Nothing was rebuilt. |
| GPU self-test | `legenex/voice/scripts/engine-selftest.sh` through the admission guard: `device NVIDIA GB10 capability (12, 1) arch ['sm_80','sm_90','sm_100','sm_110','sm_120']`, bf16 matmul ok, `from qwen_tts import Qwen3TTSModel` ok, **no flash-attn** (SDPA). Exit 0. |
| Bearer key | **already present on both nodes** (`/srv/projects/gx-cluster/secrets/gx-voice/api-key`, 0600, 44 chars, identical SHA-256 on gx10-01 and gx10-02). Not regenerated, never printed. The engine's own loopback key is generated by the supervisor into `secrets/gx-voice/engine-key` (0600). |
| Unit | `gx-voice.service` installed on gx10-02 as a symlink into the pull-only checkout (`~/.config/systemd/user/gx-voice.service -> ~/Documents/…/legenex/voice/systemd/gx-voice.service`), `~/.config/gx-voice/gx-voice.env` from the example, `enable --now`. **active / enabled.** |
| `GET http://192.168.100.11:18830/health` | D-038 shape, exactly as section 1.3 specifies: `{"status":"ok","service":"gx-voice","version":"1.0.0","state":"unloaded","engine":"unloaded","variants_loaded":[],"busy":false,"active_jobs":0,"pinned":false,"pin_honoured":false,"idle_seconds":8.2,"idle_unload_after_s":600,"queue":{"active":0,"waiting":0},"waiting":null,"blocked_by":null,"memory":{"estimate_gib":12.0,"resident_gib":null,"pending_gib":0.0,"reserve_gib":30.0}}` |
| Playground `/api/voice/model` | **503 `node_unavailable` is fixed** — now HTTP 200 with `alias=gx-voice`, `node=gx10-02` and the nine preset speakers. |
| Ledger / container name | `gx-voice` in `node2-residency.json`, container `gx-voice-engine`, process marker `gx_voice_engine` — as published to PLT, unchanged. |

### 2.2 Test totals (all real runs, `unittest`, not pytest)

| Command | Result |
|---|---|
| `cd legenex/voice && ./qa.sh` | **QA PASSED** — byte-compile, shell syntax, ruff clean, **46 tests OK** (17.4 s), no literal credentials |
| `cd legenex/voice && python3 -m unittest tests.test_gx_voice -v` | **46 tests OK** |
| `cd legenex/control-ui && .venv/bin/python -m unittest discover -s tests -p 'test_voice.py' -v` | **20 tests OK** (19 before; `test_activity_feed_source` added with the new Logs source) |
| `cd legenex/control-ui && .venv/bin/python -m unittest discover -s tests` | **709 tests OK** (145 s) — the whole Control Center suite still passes |
| `cd legenex/control-ui && npm run qa` | **fails at the ruff gate, not because of VOI.** 6 findings, none in a VOI file: `gx_control_ui/footprints.py:5` E501 and `gx_control_ui/resources.py:315` E501 (**PLT**); `gx_control_ui/music_ai.py:409,486` SIM102 and `tests/test_music_reference.py:13,99` F401/SIM117 (**MUS**). `ruff check gx_control_ui/voice.py gx_control_ui/routes_voi.py tests/test_voice.py e2e/voice_stub.py ../voice/` is clean. Reported, not "fixed" in someone else's code (rule 10). |

### 2.3 Runtime acceptance — REAL generations on gx10-02

`python3 legenex/voice/scripts/live-acceptance.py` (new; drives the **browser
API** of GX-Playground as the loopback `acceptance` account, i.e. exactly the
path the Voice page uses). Evidence:
**`/srv/logs/acceptance/build-v3/voi/live-20260917T193300Z/`**
(`summary.json`, `audio/*.wav|mp3`, `ffprobe.txt`, `asr.json`; console log
`live-20260917T193300Z.log`).

**56 checks passed, 0 failed; 7 real generations in 130 s.**

| # | Capability | Job | Audio | Verdict |
|---|---|---|---|---|
| 1 | TTS, preset voice (`preset:ryan`) | `vj_cf8f0396…` | 7.12 s, 24 kHz mono, −22.72 dBFS, WAV 341 804 B + MP3 144 045 B | **pass** — variant `custom`, RTF 0.733 |
| 2 | Emotion/style: "slowly and very calmly, almost whispering" | `vj_05bc6fc7…` | 8.48 s, −24.67 dBFS | **pass** |
| 3 | Emotion/style: "shout this urgently and very fast" | `vj_ac5ac60e…` | 5.12 s, −19.71 dBFS | **pass** |
| 4 | Voice Design from a description | `vj_4ffa1121…` | 7.52 s, −19.56 dBFS | **pass** — variant `design` |
| 5 | Designed take saved as a voice and reused | `vj_9cce815f…` → `vc_456f0d4373be885d8bb943c0` | 3.84 s, −21.53 dBFS | **pass** — variant `base` |
| 6 | Authorised reference cloning (with consent) | `vj_8feb7dc5…`, consent `vcs_4666c8b8af5c907ea42a25f8` | 2.64 s, −21.19 dBFS | **pass** |
| 7 | Cloned voice saved and reused | `vj_7db48699…` → `vc_9e4f978a9ca3eb57056e0d7b` (consent `vcs_a13daeea…`) | 3.20 s, −23.53 dBFS | **pass** |

Cases 2 and 3 used the **same voice and the same seed (2002)** and differ only
in the instruction words, so the difference is the instructions and nothing
else: **8.48 s at −24.67 dBFS versus 5.12 s at −19.71 dBFS (3.36 s shorter,
4.96 dB louder)**. The two files are byte-different. The same seeds reproduced
the same numbers on two separate runs, so generation is deterministic.

The other required checks, all passing in the same run:

* **Output format selection:** WAV and MP3 of the same take (`ffprobe` confirms
  `pcm_s16le 24000 Hz mono` and `mp3 24000 Hz mono`, both 7.12 s).
* **Generation status:** the job passed through `queued → generating →
  completed` (observed transitions recorded per case in `summary.json`).
* **Playback:** `GET …/takes/0/audio` with `Range: bytes=0-1023` → **HTTP 206**,
  1 024 of 341 804 bytes — what an `<audio>` element does when it seeks.
* **Library entry:** `a_b57c7a3a9f4d65f5ea86e512`, `type=audio`,
  `operation=tts`, `source_kind=voice_take`, `source_ref=vj_cf8f0396…#0`;
  saving the same take twice returned the same asset (idempotent); it is listed
  by `GET /api/media/assets?type=audio`.
* **History row:** all seven jobs appear in `GET /api/voice/jobs` (15 rows).
* **Consent is enforced:** the same clone request **without**
  `consent.confirmed` was refused with **403 `consent_required`**.
* **Instructions are never silently dropped:** cases 5 and 7 (Base model) carry
  the note *"line 1: style instructions are not supported by the voice-clone
  model and were not applied (the delivery follows the reference clip)"*;
  cases 1-3 (CustomVoice) carry no such note.

**Independent verification of the audio** (not the service's own numbers):

* `ffprobe` + `volumedetect` from `linuxserver/ffmpeg` on gx10-01
  (`ffprobe.txt`): every take is `pcm_s16le`, 24 000 Hz, 1 channel, with the
  duration the job reported, and ffmpeg's `mean_volume` matches the job's
  `rms_dbfs` to **0.1 dB** in all seven cases (e.g. calm −24.7 vs −24.67).
* **ASR** with `openai/whisper-large-v3-turbo` @ `41f01f3f` on the CPU inside
  the engine image on gx10-02, `--network none`
  (`legenex/voice/scripts/asr-check.py`, `asr.json`): every take transcribes to
  the requested sentence. WER **0.0-0.167**, and every single difference is a
  spelling variant of the same word (`harbour`→`harbor`, `authorised`→
  `authorized`) or a dropped comma. **No take is silent, truncated or wrong.**

### 2.4 Measured footprint (1 Hz MemAvailable through the first cold load)

`legenex/voice/scripts/measure-footprint.py` on gx10-02, through the
supervisor (so through the node-2 admission guard) — never a bare `docker run`.
Evidence: **`/srv/logs/acceptance/build-v3/voi/footprint-20260917T191026Z/`**
(`mem.csv` 298 samples at 1 Hz, `phases.json`, `summary.json`,
`design-take.wav`). Baseline was taken with node 2 idle
(`resident_models: []`, `video_queue_depth: 0`, `busy: false`).

| | |
|---|---|
| Baseline MemAvailable | **111.51 GiB** |
| Minimum while loaded | **102.06 GiB** |
| **Peak growth (cold, including the load transient)** | **9.45 GiB** |
| Engine container with no model resident | 0.48 GiB |
| **Steady resident, one variant** | **6.5 GiB** (per-variant: custom 6.39, design 6.42, base 6.46; the supervisor's own `resident_gib` reads 6.3-6.9) |
| MemAvailable after unload | **111.50 GiB** — fully returned (+6.91 GiB) |
| **Cold start** | **35.0 s** = 3.5 s container + 31.5 s first variant load |
| Warm variant switch | 26.0-28.7 s (cold 28.7-32.3 s) |
| Unload | 3.8 s |
| First audio | 6.0-8.7 s for an 8.2-8.6 s take (the take is returned whole; no streaming) |
| **RTF** | **0.72-1.00** (0.72-0.76 warm) |

Six real generations ran during the measurement, three cold and three warm,
one per variant, all `completed`.

The configured admission estimate stays at **12 GiB**
(`GX_VOICE_ENGINE_ESTIMATE_GIB`), deliberately above the measured 9.45 GiB
peak: it is the number the guard reserves, and it must also cover generation
and a variant switch. It is not lowered on the strength of one measurement.

FOOTPRINT gx-voice node=gx10-02 cold_gib=9.45 resident_gib=6.5 startup_s=35 measured=2026-09-17 evidence=/srv/logs/acceptance/build-v3/voi/footprint-20260917T191026Z/summary.json

(`cd legenex/control-ui && .venv/bin/python -m gx_control_ui.footprints sync`
has been run; `aliases.gx-voice.measured_footprint` in
`legenex/models/registry.json` now carries these numbers, so Resource Control
no longer says "not measured yet".)

### 2.5 Unload path verified (plt.md 5.1, D-038)

Evidence: **`/srv/logs/acceptance/build-v3/voi/unload-20260917T1930/summary.json`**.
**13 checks passed, 0 failed.**

| Check | Result |
|---|---|
| `POST /v1/voice/unload {"if_idle": true}` **while a job runs** | **409** `conflict` — "a voice job is running; cancel it or wait before unloading". The running job was **not** disturbed and completed normally (8.24 s of audio). |
| **while pinned** (pin set through Control Center Resource Control, `POST /api/resources/gx-voice/pin`, never by hand) | health shows `pinned: true, pin_honoured: true`; unload → **409** "gx-voice is pinned" |
| **idle and unpinned** | **200** `{"reason":"unloaded while idle…","seconds":3.8,"mem_available_before_gib":104.66,"mem_available_after_gib":111.34,"container_gone":true}` |
| container `gx-voice-engine` | **gone** (0 matches in `docker ps -a`) |
| `node2-residency.json` | **`{}`** — no `gx-voice` entry |
| `pgrep -fc 'gx_voice_engine'` | **0** |
| MemAvailable | 104.67 GiB loaded → **111.30 GiB** after (+6.63 GiB) |
| `/health` after | `state: unloaded`, `resident_gib: null`, `pending_gib: 0.0` |
| a second unload | **200 `{"noop": true}`**, not an error |

So the drain check `node2-holds.sh` performs (container absent, no ledger
entry, `pgrep -fc gx_voice_engine` = 0) is satisfied.

**Node 2 is back to idle:** engine unloaded, ledger empty, no gx-voice
container, MemAvailable ≈ 111-117 GiB, supervisor `active` and listening.

### 2.6 What changed in the tree this session

| File | Change |
|---|---|
| `legenex/voice/scripts/live-acceptance.py` | **new** — the acceptance driver above (the README already referenced it; it did not exist) |
| `legenex/control-ui/gx_control_ui/voice.py` | **new** `VoiceStudio.activity(user, since, limit)` — the Logs activity source (plt.md section 6) plus `OPERATION_LABEL`. It returns only operation, voice id, audio seconds, RTF and variant: never the script, the voice description or a consent statement |
| `legenex/control-ui/tests/test_voice.py` | **new** `test_activity_feed_source` (own jobs only, no script text, `normalise()` accepts it) |
| `legenex/control-ui/docs/17-voice.md` | **new** in-UI documentation page (rule 11) |
| `legenex/playground/API.md` | Voice section: added the measured cold-start / RTF timing clients must expect |

No file outside VOI ownership was edited.

### 2.7 Limitations (measured, not guessed)

1. **The first job after an idle period costs ~35 s** before any audio, and a
   job that needs a different variant than the resident one costs another
   26-32 s. With `GX_VOICE_MAX_RESIDENT=1` a Studio session that mixes preset
   voices with saved/cloned voices pays that switch each time it alternates.
   Raising it to 2 would cost ~4.5 GiB more resident; not changed without
   sign-off.
2. **No streaming.** `POST /v1/audio/speech` and the take endpoints return the
   finished file. Time to first audio is the whole generation (RTF ≈ 0.73 plus
   any load).
3. **Style and emotion instructions only reach CustomVoice and VoiceDesign.**
   Cloned and designed saved voices render on Base, which has no instruction
   channel; the job says so in `notes` (proved above), but a user who wants
   emotional control must use a preset voice.
4. **`gx-voice` is not yet in the gateway's `/v1/models`** — see 2.8.
5. **Voice jobs do not yet appear in the Playground Logs feed** — the source
   exists and is tested, but one registration line in the shared `server.py`
   is needed (2.8). The History page is unaffected: it reads
   `/api/voice/jobs`, which works.
6. `/srv/models/voice-data` on gx10-02 (references, prompt cache, job audio,
   the node-side SQLite) is created by the supervisor and is **not** covered by
   Storage & Cleanup yet.
7. The drain's `pgrep -fc 'gx_voice_engine'` matches its **own** shell when the
   pattern is typed literally into `bash -c`. It is correct inside
   `node2-holds.sh` (the pattern is not in that process's own command line),
   but anyone checking by hand over ssh should use `pgrep -fc 'gx_voice[_]engine'`
   or they will see 1 when the answer is 0. Noted so it is not misread as a
   failed drain.
8. Acceptance was interrupted once by a `gx-control-ui` restart from another
   workstream at 21:17:30 (the run died with 502 `bad_gateway`). No gx-voice
   defect; the node-2 job completed regardless. The acceptance script now
   retries idempotent reads and re-signs-in, and the successful run was taken
   under `flock -s /srv/projects/gx-cluster/state/build-v3/restart.lock`.

### 2.8 Integration requests for the lead

**1 — register the Voice activity source (Logs page, plt.md section 6).**
In `legenex/control-ui/gx_control_ui/server.py`, in the existing Build V3 VOI
block in `App.__init__` (right after `self.voice = VoiceStudio(...)`), add one
line:

```python
        self.activity.register("voice", self.voice.activity)
```

`VoiceStudio.activity` is implemented and unit-tested
(`tests/test_voice.py::JobTests::test_activity_feed_source`); it returns items
in the exact shape plt.md section 6 specifies, with no prompt or transcript.

**2 — recreate `gx-litellm`** so the `gx-voice` entry in
`legenex/gateway/litellm/config.yaml` takes effect. Verified 2026-09-17 21:30:
`GET http://127.0.0.1:4000/v1/models` still lists only
`gx-mini, gx-fast, gx-reason, gx-max, gx-auto, gx-image, gx-video`. Per B-027
the recreate command is
`env -i PATH="$PATH" HOME="$HOME" docker compose --env-file .env -f docker-compose.gateway.yml up -d --no-deps litellm`.
Nothing on the VOI side depends on it: the Voice page, `/api/voice/*` and
`/v1/voice/*` already work. Only `POST /v1/audio/speech` with
`model: "gx-voice"` on port 4000 is blocked until then, so that one path is
the only VOI capability not yet proved end to end.

**3 — `Manual.md`,** which has no Voice section at all. Suggested text to drop
into the creative chapter:

> **Voice (gx-voice).** GX-Playground → Create → Voice turns text into speech
> on gx10-02 with Qwen3-TTS 12 Hz 1.7B. Pick one of nine preset speakers or one
> of your saved voices, type the script, and add plain-language delivery
> instructions ("bright and upbeat", "slow and calm"). You can design a brand
> new voice from a description, or clone a 2-60 s recording you have permission
> to use — cloning always asks you to confirm that permission, and the
> confirmation is stored with the fingerprint of the recording. Takes play in
> the browser, download as WAV or MP3, and save to the Library. The model loads
> on demand, so the first request after a quiet period takes about 35 seconds
> before the audio starts; after that a 9-second line takes about 7 seconds.
> Full reference: Control Center → Docs → gx-voice, and
> `legenex/playground/API.md`.

**4 — two free-text fields in `legenex/models/registry.json`** that
`footprints sync` does not touch and that I must not edit by hand. In
`aliases."gx-voice"`, please replace:

```json
  "memory": "not measured yet",
  "measured": null,
```

with:

```json
  "memory": "9.45 GiB peak on a cold load, 6.5 GiB resident with one variant (measured 2026-09-17)",
  "measured": "2026-09-17",
```

`measured_footprint` (written by the sync) is already correct and is what
Resource Control and the Models page read for the numbers — verified live:
`GET /api/resources` now returns `"footprint_gib": 6.5, "measured_ok": true`
for gx-voice, and `GET /api/catalog` carries the full footprint object. Only
the two human-readable strings still say "not measured yet".

**5 — for `CURRENT_STATE.md` / `TEST_RESULTS.md`:** gx-voice is deployed,
measured and accepted; the numbers are in 2.2-2.5 above.

### 2.9 Blockers

**None for VOI.** The only outstanding item is request 2 above (recreating
`gx-litellm`), which is the lead's by BUILD_V3 and does not block anything
else in this workstream.
