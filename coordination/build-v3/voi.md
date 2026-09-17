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
