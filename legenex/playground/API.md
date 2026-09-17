# GX-Playground: browser API contract

GX-Playground (`http://100.105.214.61:8090/`) serves its single-page app and
forwards the calls below, unchanged, to the Control Center backend on
127.0.0.1:8088. Anything not listed returns 404 from the Playground.

## Conventions

* **Session.** The session is an HttpOnly, SameSite=Strict cookie
  (`gxui_session`). It is shared with the Control Center, because both run on
  the same host.
* **CSRF.** Every `POST` sends `X-CSRF-Token` with the value from
  `GET /api/session`, `Content-Type: application/json` and a JSON body.
  Uploads are the exception: their raw body is the file, and the type is the
  file's own `Content-Type`.
* **Errors.** Errors are JSON:
  `{"error": {"message": "...", "code": "..."}}`. Messages are safe to show.
* **Auth failure.** `401` means the session expired: show the sign-in screen.
* **Progress.** Real progress is a number from 0 to 1, or `null`. When it is
  `null`, the UI shows an indeterminate state. It never makes up a
  percentage.

## Session

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/session` | `{authenticated, user?, csrf?, configured}` |
| POST | `/api/login` | `{username, password}` → `{authenticated, user, csrf}` + cookie |
| POST | `/api/logout` | `{}` |
| GET | `/pg/config` | `{control_center_url, version}` (Playground itself) |
| GET | `/pg/health` | `{status, version, upstream: {ok}}` (Playground itself, no auth) |

## Dashboard and resources

`GET /api/creative/overview` returns:

```json
{
  "resources": <summary, see below>,
  "active": [<media job> | <music job>],     // not finished
  "recent": [<asset>],                       // 12 newest Library items
  "counts": {"image": n, "video": n, "audio": n},
  "errors": [{"kind", "message", "at"}],
  "music_error": null | "text",
  "capacity": {"image": {"count", "bytes"}, "video": {...}, "audio": {...}}
}
```

`GET /api/resources/summary` returns:

```json
{
  "profile": "auto", "profile_label": "Auto", "maintenance": false,
  "profiles": [{"id": "auto|text|media|music|max", "label", "summary"}],
  "rows": [{"key": "text|image|video|music|max", "label", "status", "detail"}],
  "queued": 0,
  "control_center_url": "http://100.105.214.61:8088/#/resources"
}
```

`status` is one of: Ready, Loading, Working, Waiting, Unloading, Paused,
Unavailable, Idle, Starting, Running, Releasing.

Other resource calls:

* **`GET /api/resources/profile/plan?to=<id>`** returns
  `{from, to, label, summary, stays[], may_drain[], drains_now[], active[], queued{}, conflicts[], needs_confirm, confirm_phrase}`.
* **`POST /api/resources/profile`** takes `{profile, confirm?}`.
  * When `needs_confirm` is true, send `confirm: true`, or the
    `confirm_phrase` string (`"gx-max"` for Max).
  * The Playground cannot select `maintenance` (403).
* **`GET /api/resources/explain/<alias>`** returns
  `{code, reason, detail, need_gib?, available_gib?, blocking[], next}`.

## Media (images and video)

`GET /api/media/options` returns `{kinds, image_sizes[], video_sizes[],
image_models[], edit_modes[], default_image_model: {generate, edit}, stats}`.

`image_models` (Build V3) is the gx-image model list. The single source is the
media router's `gx_media_router/image_models.py`; Creative Flows and any other
client should read it here rather than hard-code it:

```json
{"id": "visionmaster-pro-v3", "label": "VisionmasterPro_V3", "family": "sdxl",
 "operations": ["generate", "edit"], "description": "...",
 "sizes": ["1024x1024", "832x1216", ...], "default_size": "832x1216",
 "defaults": {"steps": 28, "cfg": 5.0, "sampler_name": "euler_ancestral", "scheduler": "normal", "edit_mode": "restyle"},
 "masks": true, "negative_prompt": true, "qualities": [],
 "edit_modes": [{"id": "change", "label": "Change / replace", "description": "...",
                 "strength_applies": true, "denoise_range": [0.6, 1.0], "requires_mask": true,
                 "preserves_source_latent": false}, ...]}
```

Ids: `qwen-image-2512` (operations `generate`), `qwen-image-edit-2511`
(`edit`, `variation`), `visionmaster-pro-v3` (`generate`, `edit`). Show
`label`, never a file name. `strength_applies: false` means the backend
ignores strength for that mode (do not offer the control).

`POST /api/media/jobs` takes `{kind, ...}` and returns 202 with a media job.
The fields per kind:

| kind | fields |
|---|---|
| `t2i` | `prompt*`, `image_model` (generate models), `negative_prompt`, `size` (the model's `sizes`), `n` 1-4, `quality` standard/fast/hd (Qwen only), `steps` 1-100, `guidance` 0-20, `seed`, `title`, `uncensored` (bool, Qwen), `quality_tags` (bool, VisionmasterPro_V3) |
| `edit` | `prompt*`, `source_id*` (image asset), `image_model` (edit models), `edit_mode` (the model's `edit_modes`), `edit_quality` fast/quality (Qwen), `strength` 0-1 (kept only when the mode's `strength_applies`), `negative_prompt`, `mask`, `mask_source`, `mask_rects`, `steps` 1-50, `seed`, `title`, `uncensored` |
| `variation` | `source_id*` (image), `prompt` (optional), `strength` 0-1 (below 0.5 keeps the scene; higher re-imagines it), `seed`, `title` |
| `t2v` | `prompt*`, `size` (video_sizes), `seconds` 0.5-10, `fps` 8-24, `seed`, `title` |
| `i2v` | `prompt*`, `source_id*` (image), `size`, `seconds`, `fps`, `seed`, `title` |
| `v2v` | `prompt*`, `source_id*` (video), `size`, `seconds`, `strength` 0.05-1, `seed`, `title` |

Edit masks: `mask` is a `data:image/png;base64,` URL of at most 48 KB and
at most 1024 px per side, white = may change, with the source's aspect ratio
(±2 %). The server decodes it and refuses an empty mask. `mask_source` is
`painted`, `rectangles` or `painted+rectangles`; `mask_rects` is up to 20
`{x, y, w, h}` fractions (metadata only). The job's `params.mask` and the
Library asset's `settings.mask` keep `{source, width, height, coverage,
sha256, bytes, rects}`, never the image. A masked edit therefore cannot be
re-run from its recipe: paint the mask again. Errors are 400 with a readable
message ("VisionmasterPro_V3 needs a mask for 'Change / replace' ...", "the
mask is empty ...", "... redraw it on this source").

Library image assets record `model_alias` `gx-image`, `model_repo`,
`model_revision`, `workflow`, and in `settings`: `image_model`,
`image_model_label`, `image_model_family`, `edit` (`{edit_mode, denoise,
strength_applied, masked, workflow}`) and `mask`.

A media job looks like this:

```json
{"id": "16 hex", "kind", "label", "alias": "gx-image|gx-video",
 "phase": "queued|waiting|generating|saving|ready|failed|cancelled",
 "detail", "waiting": null | {"code", "reason", "detail", "need_gib", "available_gib", "blocking", "next"},
 "cold_start": bool|null, "created", "started", "ended", "elapsed_seconds",
 "router_job", "assets": ["a_..."], "error", "prompt", "source_id", "params": {...}}
```

The remaining media endpoints:

* `GET /api/media/jobs` returns `{jobs: [...]}`, newest first.
* `GET /api/media/jobs/<id>` returns one job.
* `POST /api/media/jobs/<id>/cancel` takes `{}`. It works only while the job
  is queued or waiting.

Media jobs also carry `error_code` (machine-readable, for example
`out_of_memory`, `lora_not_found`, `cancelled`) and `error_hint` (a readable
sentence for that code, or `null`).

## Video: Wan 2.2 LoRAs, presets, history (Build V3 WAN)

Text to video with LoRAs goes through these routes; the job itself is an
ordinary media job (`GET /api/media/jobs/<id>`). The browser sends library
**entry ids**, choices and strengths only; file names always come from the
server's catalogue.

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/video/config` | `{defaults: {strength_high: 0.8, strength_low: 0.8, strength_min, strength_max, strength_step, multi_lora_strength: 0.5, max_stack, size, seconds, fps, negative_prompt, advanced}, limits, sizes: [{value, aspect}], samplers[], schedulers[], apply[], model: {id, label, workflow, available, lora_support, high_model, low_model, base_loras[]}, max_frames}` |
| GET | `/api/video/loras[?refresh=1]` | library (below) |
| POST | `/api/video/loras/rescan` | `{}` → library |
| POST | `/api/video/loras/order` | `{ids: ["l_…", …]}` → library |
| GET | `/api/video/loras/l_<16 hex>` | one entry |
| POST | `/api/video/loras/l_<16 hex>` | any of `{display_name, description, tags[], default_high, default_low, enabled, allow_unknown}` → entry |
| POST | `/api/video/pairs` | `{high_file, low_file}` (catalogue names) → entry; 409 `pair_conflict` |
| POST | `/api/video/pairs/remove` | `{entry_id}` → library; 409 `not_paired` |
| POST | `/api/video/pairs/restore` | `{name}` (undo the split of an automatic pair) → library |
| GET | `/api/video/presets` | `{presets: [preset]}` |
| POST | `/api/video/presets` | `{name, description?, data}` → preset; 409 `preset_exists` |
| GET/POST | `/api/video/presets/wp_<16 hex>` | preset / update `{name?, description?, data?}` |
| POST | `/api/video/presets/wp_<16 hex>/duplicate` | `{name?}` → new preset |
| POST | `/api/video/presets/wp_<16 hex>/delete` | `{confirm: true}` |
| POST | `/api/video/presets/wp_<16 hex>/resolve` | `{overrides}` → `{preset, body, valid, warnings[]}` (see below) |
| POST | `/api/video/workflow` | generate body → `{workflow, workflow_version, seed, loras[], chains, frames, graph}` (nothing runs) |
| POST | `/api/video/generate` | generate body → 202 media job |
| POST | `/api/video/jobs/<16 hex>/cancel` | `{}` → media job (queued or waiting only; 409 otherwise) |
| GET | `/api/video/generations?q=&status=&limit=&offset=` | `{total, items[], limit, offset}`; `status` is queued, waiting, generating, saving, ready, failed, cancelled or active |
| GET | `/api/video/generations/<16 hex>` | one record with `workflow` (graph) and `asset` |
| GET | `/api/video/generations/<16 hex>/workflow` | the workflow JSON as a download (`?inline=1` to view) |
| GET | `/api/video/errors?limit=` | `{items: [{id, created_at, prompt, status, error_code, error_message, error_detail, loras, router_job, comfy_prompt_id}]}` |

**Generate body** (also the preset `resolve` result):

```json
{"prompt": "…", "negative_prompt": "…", "size": "640x640", "seconds": 3.0, "fps": 16,
 "seed": 123 | null, "title": "…", "preset_id": "wp_…" | null,
 "advanced": {"shift": 5.0, "cfg": 1.0, "steps": 4, "boundary": 2, "sampler_name": "euler", "scheduler": "simple"},
 "loras": [{"entry_id": "l_…", "enabled": true, "apply": "pair|high|low|both|null",
            "strength_high": 0.8, "strength_low": 0.8}],
 "flow_id": "…", "flow_run_id": "…", "flow_node_id": "…"}
```

* `apply` defaults to the only option an entry has (`pair`, `high` or
  `low`); a general or unresolved entry needs an explicit choice
  (`lora_apply_required`), and `both` is refused for high- and low-noise files.
* A missing strength uses the entry's default, then `defaults`.
* Disabled items are recorded but not applied. A preset item may also carry
  `high_file`, `low_file`, `file` and `display_name`, used to find the entry
  again after the library changed; they never select a file by themselves.

**Library entry:**

```json
{"id": "l_…", "kind": "pair|high|low|general|unknown",
 "pair_state": "paired|high_only|low_only|general|unresolved|broken_high|broken_low",
 "pair_source": "auto|manual|null", "display_name", "description", "tags": [],
 "high_file", "low_file", "file", "files": [catalogue file], "size", "discovered_at",
 "default_high", "default_low", "enabled", "allow_unknown", "position",
 "compatibility": "compatible|incompatible|unknown", "usable", "unresolved",
 "problems": [], "apply_options": ["pair"] }
```

The library response is `{entries[], unpaired_files[], shadowed_files[],
missing_files[], roots[], scanned_at, comfy: {checked_at, error,
lora_loader_available}, problems[], defaults}`. A catalogue file has `name`
(as ComfyUI sees it), `path` (on gx10-02), `size`, `mtime`, `valid`,
`error`, `key_format`, `tensors`, `rank`, `hidden_dim`, `blocks`, `family`,
`compatibility`, `compatibility_reason`, `noise`, `noise_source`,
`noise_reason`, `metadata`, `header_sha256`, `comfy_visible`,
`shadowed_by`, `usable`.

**Error codes** (400 unless noted): `prompt_required`, `lora_not_found`,
`lora_high_missing`, `lora_low_missing`, `lora_invalid_file`,
`lora_incompatible`, `lora_unknown_compatibility`, `lora_not_visible`,
`lora_branch_mismatch`, `lora_apply_required`, `lora_disabled`,
`lora_duplicate`, `lora_too_many`, `invalid_request`,
`router_unavailable` (502), `preset_not_found` / `not_found` (404).

**Creative Flows.** A Video node references a preset by its stable id and
calls `POST /api/video/presets/<id>/resolve` with `{overrides: {prompt, seed,
size, seconds, fps, negative_prompt, title, loras, advanced, flow_id,
flow_run_id, flow_node_id}}`, then posts `body` to `/api/video/generate`
(server side: `app.wan.resolve_preset(id, overrides)` then
`app.wan.generate(body, user=…)`). The example presets have fixed ids:
`wp_5c1e7a2b90d34f01` Cinematic Realism, `wp_7f3a9c4d12e84b02` High Detail,
`wp_2b8d6e1f47a54c03` Character Consistency, `wp_9e4c0b7a35f14d04` Motion
Style, `wp_1a6f8e2c59b74e05` Custom 1.

## Library (images, video, audio)

`GET /api/media/assets` searches the Library.

* **Query parameters:** `q`, `type` (image|video|audio), `model` (alias),
  `operation`, `favourite` (1|0), `sort`, `limit` (1-200) and `offset`.
* **Sort values:** newest, oldest, title, size, duration.
* **Result:** `{total, items: [<asset>], facets: {models, operations}, counts: {image, video, audio}}`.

An asset looks like this:

```json
{"id": "a_<24 hex>", "type": "image|video|audio", "ext", "media_type", "title", "created_at",
 "operation": "generate|edit|variation|i2v|v2v|upload|remix|repaint|extend",
 "model_alias", "model_repo", "model_revision", "workflow", "prompt", "negative_prompt",
 "seed", "steps", "guidance", "strength", "width", "height", "duration", "fps",
 "file_size", "sha256", "parent_id", "parent_deleted", "favourite", "job_id",
 "settings": {"requested": {...}, ...}, "variants": {"wav": {"bytes","sha256"}, ...},
 "lyrics", "tags": [], "bpm", "music_key", "time_signature", "sample_rate", "channels",
 "waveform": [[min, max], ...],
 "url", "thumbnail_url", "download_url",
 "stream_url", "downloads": {"wav": url, "flac": url, "mp3": url}}
```

`stream_url` and `downloads` are present for audio only.

The remaining Library endpoints:

* **Read one asset.** `GET /api/media/assets/<id>` returns the asset plus
  `children[]` and `ancestors[]` (brief items).
* **Lineage tree.** `GET /api/media/assets/<id>/lineage` returns
  `{asset, root, tree: [...]}`. Each tree node carries the brief fields
  (`id`, `type`, `operation`, `title`, `prompt`, `created_at`, `model_alias`,
  `parent_id`, `thumbnail_url`) plus `children: [...]`.
* **File.**
  `GET /api/media/assets/<id>/file[?download=1][&format=wav|flac|mp3]`
  supports Range requests.
* **Thumbnail.** `GET /api/media/assets/<id>/thumbnail` returns JPEG, or the
  image itself for image assets. Audio assets have no thumbnail (404).
* **Update.** `POST /api/media/assets/<id>` takes `{title?, favourite?}`.
* **Delete.** `POST /api/media/delete` takes `{ids: [...], confirm: true}`
  and returns `{deleted, missing}`.
* **ZIP.** `POST /api/media/zip` takes `{ids}` and returns
  `{token, count, bytes, url}`. `GET url` downloads it, once, within
  15 minutes.
* **Upload image/video.** `POST /api/media/upload` sends the raw body
  (`Content-Type: image/*|video/*`, `X-Title: <urlencoded>`) and returns the
  asset (operation `upload`).

## Music (gx-music, ACE-Step 1.5 XL turbo on gx10-02)

`GET /api/music/model` returns:

```json
{"identity": {"dit_repo", "dit_revision", "lm_repo", "lm_revision", "shared_repo", "shared_revision",
              "runtime_repo", "runtime_ref", "image"},
 "capabilities": {"operations": {"generate": "text2music", "remix": "cover", "edit": "repaint",
                                  "extend": "repaint", "extract": null, "lego": null, "complete": null},
                  "controls": {name: {type, min, max, values, labels, default, note}},
                  "remix_controls", "edit_controls", "extend_controls", "max_duration_s"},
 "engine": {"state": "unloaded|loading|ready|unloading|failed", "last_load_seconds", ...},
 "queue": {"active", "current_job"}, "last_success": {...} | null}
```

Build the controls from `capabilities.controls`. A control that is not there
is not supported: there is no DiT guidance on turbo, and extract, lego and
complete are `null`.

The tag and job endpoints:

* **Tags.** `GET /api/music/tags?q=&limit=` returns
  `{groups: {genre: [...], mood, instrument, vocal, production, tempo, era}, suggestions: [...]}`.
* **List jobs.** `GET /api/music/jobs[?status=&limit=]` returns
  `{jobs: [<music job>]}`.
* **Submit.** `POST /api/music/jobs` takes `{operation, ...}` and returns 202
  with a music job. The fields per operation are in the table below.

| operation | fields |
|---|---|
| `generate` | `prompt`, `style_tags[]`, `lyrics`, `instrumental`, `description` (LM writes everything; do not combine it with lyrics, bpm, key, time signature or duration), `vocal_language`, `duration`, `bpm`, `key`, `time_signature` ("2"/"3"/"4"/"6"), `seed`, `batch_size`, `inference_steps`, `infer_method`, `thinking`, `enhance_prompt`, `lm_temperature`, `lm_cfg_scale`, `lm_top_p`, `title`, `reference_asset_id` |
| `remix` | `source_asset_id*`, `prompt`, `style_tags`, `lyrics`, `strength` 0-1, `noise_strength`, `seed`, `title` |
| `edit` (repaint) | `source_asset_id*`, `start*`, `end*` (seconds), `prompt`, `lyrics`, `mode` (conservative, balanced, aggressive), `strength`, `seed`, `title` |
| `extend` | `source_asset_id*`, `seconds*`, `direction` end/start, `prompt`, `lyrics`, `seed`, `title` |

A music job looks like this:

```json
{"id": "mus-<32 hex>", "operation", "status": "queued|waiting_for_resource|loading_model|preparing|generating|processing|saving|completed|failed|cancelled",
 "phase": same as status, or "saving" while gx10-01 stores the tracks, "phase_detail",
 "detail", "progress": null|0..1, "created_at", "started_at", "finished_at", "elapsed_s", "title",
 "request": {...}, "tracks": [{"index", "seed", "duration_s", "bpm", "key", "time_signature", "caption", "lyrics",
             "waveform", "files": {"wav": {"bytes","sha256"}, ...}}],
 "timings": {...}, "model": {...}, "error": null|{"code","message","retryable"},
 "waiting": null|{"code","reason","detail","need_gib","available_gib","blocking","next"},
 "imported": bool, "library_assets": ["a_..."], "parent_asset_id", "reference_asset_id"}
```

The remaining music endpoints:

* `GET /api/music/jobs/<mus-id>` returns one job.
* `GET /api/music/jobs/<mus-id>/lineage` returns the job's lineage.
* `POST /api/music/jobs/<mus-id>/cancel` takes `{}`. A job that is already
  running finishes its render, which is then thrown away.
* `POST /api/music/upload` sends the raw audio body
  (`Content-Type: audio/*`, `X-Filename`, `X-Title`) and returns a Library
  asset (type audio, operation upload). Use it as a remix source or a
  reference.

The tracks of a completed job appear in the Library as audio assets with
`job_id = mus-...`. Play them from `stream_url` and download them from
`downloads`.

## Public music API (gateway keys)

`/v1/music/*` on the Playground port takes a gateway key that allows
`gx-music`, sent as `Authorization: Bearer sk-...`. It does not accept
session cookies. Each key sees only its own jobs.

| Method | Path | |
|---|---|---|
| POST | `/v1/music/generations`, `/remix`, `/edits`, `/extend` | submit (source: `{job_id, index}` or `{upload_id}`) |
| POST | `/v1/music/uploads` | raw audio, `X-Filename` |
| GET | `/v1/music/{id}` | job |
| GET | `/v1/music/{id}/content?index=0&format=wav\|flac\|mp3` | 409 until the track is saved |
| GET | `/v1/music/{id}/lineage` | |
| POST | `/v1/music/{id}/cancel` | |
| GET | `/v1/music/jobs`, `/v1/music/model`, `/v1/music/tags` | |

Load and unload return 403: lifecycle belongs to the cluster.

## Voice (gx-voice, Qwen3-TTS 12Hz 1.7B on gx10-02) — Build V3 VOI

The Control Center owns saved voices, voice jobs and takes (application
database, migration `040_voice.sql`). gx10-02 renders the audio. The backend
picks the model for every line:

| Voice | Model (repository @ revision) |
|---|---|
| preset (`preset:<speaker>`, or a saved preset voice) | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` @ `0c0e3051` |
| a description (`voice_design`) | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` @ `5ecdb673` |
| a saved designed or cloned voice, or `voice_clone` | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` @ `fd4b2543` |

Preset speakers: `aiden`, `ryan` (English), `vivian`, `serena`, `uncle_fu`,
`dylan`, `eric` (Chinese), `ono_anna` (Japanese), `sohee` (Korean). Any
speaker can speak any supported language. Languages: `auto`, `english`,
`chinese`, `german`, `french`, `spanish`, `italian`, `portuguese`, `russian`,
`japanese`, `korean`.

### Session routes

| Method | Path | |
|---|---|---|
| GET | `/api/voice/model` | identity, variants, speakers, languages, limits, engine state, `gateway_url` |
| GET | `/api/voice/voices` | `{"voices": [...]}`: saved voices, then the nine presets (`builtin: true`) |
| POST | `/api/voice/voices` | create a voice (below); 201 |
| GET | `/api/voice/voices/{vc_…\|preset:…}` | one voice |
| POST | `/api/voice/voices/{vc_…}` | update `name`, `description`, `instructions`, `language`, `style`, `metadata`, `transcript`; records a new version |
| POST | `/api/voice/voices/{vc_…}/delete` | `{"confirm": true}` |
| GET | `/api/voice/voices/{vc_…}/versions` | `{"versions": [{version, snapshot, changed_by, created_at}]}` |
| GET | `/api/voice/jobs?limit=&status=&voice_id=` | `{"jobs": [...]}` newest first |
| POST | `/api/voice/jobs` | submit a job (below); 202 |
| GET | `/api/voice/jobs/{vj_…}` | job |
| POST | `/api/voice/jobs/{vj_…}/cancel` | |
| POST | `/api/voice/jobs/{vj_…}/delete` | `{"confirm": true}`; takes saved to the Library stay |
| GET | `/api/voice/jobs/{vj_…}/takes/{0-3}/audio?format=wav\|mp3[&download=1]` | the take (Range supported) |
| POST | `/api/voice/jobs/{vj_…}/takes/{0-3}/save` | `{"title"?}` → Library asset (idempotent) |
| POST | `/api/voice/upload` | raw audio body (`Content-Type: audio/*`, `X-Filename`, `X-Title`), 2-60 s, ≤ 32 MB. gx10-02 checks it first; returns a Library asset (operation `upload`, `source_kind` `voice_reference`) |

Load and unload (`POST /api/voice/load|unload`) are Control Center only;
through the Playground they return 403.

**Create a voice.**

```json
{"kind": "preset",   "name": "Promo Host", "speaker": "aiden", "instructions": "energetic, upbeat",
 "language": "english", "style": {"speed": 1.1, "pause_ms": 250}}
{"kind": "designed", "name": "Captain", "job_id": "vj_…", "take": 1, "description": "…"}
{"kind": "cloned",   "name": "My voice", "reference_asset_id": "a_…", "transcript": "exact words of the clip",
 "consent": {"confirmed": true, "statement": "I confirm that I own this recording or have the speaker's permission …"}}
```

* Names are unique (case-insensitive) and may not be a preset name (409
  `name_taken`).
* A designed voice uses a completed `voice_design` take as its reference
  clip (the take is saved to the Library as operation `voice_design`).
* A cloned voice without `consent.confirmed: true` is refused with 403
  `consent_required`. The confirmation, the user, the client address and the
  SHA-256 of the exact recording are stored in `voice_consents`.
* `style` keys: `speed` (0.5-2), `pause_ms` (0-5000), `temperature`,
  `top_p`, `top_k`, `repetition_penalty`. They are defaults for jobs that
  use the voice.

**Submit a job** (`POST /api/voice/jobs`; unknown fields are 400):

| Field | |
|---|---|
| `operation` | `tts` \| `voice_design` \| `voice_clone` \| `dialogue` |
| `text` | 1-10 000 characters (not for `dialogue`). Blank lines start a new paragraph |
| `voice_id` | `tts`: `vc_…` or `preset:<speaker>` |
| `description` | `voice_design`: the voice to create (≤ 1 000) |
| `reference` | `voice_clone`: `{"asset_id", "transcript"?, "consent": {"confirmed": true, "statement"}}` |
| `segments` | `dialogue`: 1-60 `{"voice_id", "text", "instructions"?, "pause_ms"?}` |
| `instructions` | style, emotion, delivery and pacing words (≤ 500). Applied by preset voices and voice design only; the job's `notes` say when a line ignored them |
| `language` | see above; default the voice's language, else `auto` |
| `takes` | 1-4; take *i* uses `seed + i` |
| `seed` | recorded; random when omitted |
| `speed` | 0.5-2.0, a pitch-preserving time stretch of the finished take |
| `pause_ms` | 0-5000 between paragraphs and dialogue lines (default 350) |
| `temperature`, `top_p`, `top_k`, `repetition_penalty` | sampling |
| `title` | ≤ 200 |
| `auto_save` | save every take to the Library when the job completes |

**Job.**

```json
{"id": "vj_…", "operation": "tts", "status": "queued|waiting_for_resource|loading_model|generating|processing|saving|completed|failed|cancelled",
 "detail": "speaking line 1, take 2 (preset voice)", "progress": 0.5, "title": "…", "voice_id": "preset:ryan",
 "takes": [{"index": 0, "seed": 42, "duration_s": 4.2, "sample_rate": 24000, "rms_dbfs": -20.1, "peak": 0.8,
            "waveform": [[-0.4, 0.5], …], "formats": ["mp3", "wav"], "asset_id": null,
            "audio_url": "/api/voice/jobs/vj_…/takes/0/audio"}],
 "timings": {"generate_s": 3.1, "first_audio_s": 1.2, "audio_s": 4.2, "rtf": 0.74, "variants": ["custom"]},
 "notes": [], "error": null, "waiting": null, "flow": null, "request": {…}}
```

`completed` means every take is on gx10-01 and playable (a take rendered on
gx10-02 but still downloading shows `saving`). A saved take is a Library
asset with `type: audio`, `operation` `tts` / `voice_design` / `voice_clone`
(dialogue → `tts`), `source_kind: voice_take`, `source_ref: "vj_…#<take>"`,
`model_alias: gx-voice`, and the reference clip as `parent_id` for cloned and
designed voices.

## Public voice API (gateway keys)

Authenticated like `/v1/music`: a gateway key that allows `gx-voice`
(`Authorization: Bearer sk-...`), never a cookie. Each key sees only its own
jobs. Bodies are the same as the session routes without `operation`.

| Method | Path | |
|---|---|---|
| POST | `/v1/voice/speech` | a `tts` job |
| POST | `/v1/voice/design` | a `voice_design` job |
| POST | `/v1/voice/clone` | a `voice_clone` job (`reference.consent` required) |
| POST | `/v1/voice/dialogue` | a `dialogue` job |
| POST | `/v1/voice/uploads` | raw reference audio → `{"asset_id", "duration_s"}` |
| GET | `/v1/voice/jobs`, `/v1/voice/jobs/{vj_…}` | own jobs; `takes[].files` holds the content URLs |
| GET | `/v1/voice/jobs/{vj_…}/takes/{n}/content?format=wav\|mp3` | 409 until `completed` |
| POST | `/v1/voice/jobs/{vj_…}/takes/{n}/save` | → `{"asset_id"}` |
| POST | `/v1/voice/jobs/{vj_…}/cancel` | |
| GET | `/v1/voice/model`, `/v1/voice/voices`, `/v1/voice/voices/{id}` | |
| POST | `/v1/voice/voices` | create a voice (same bodies as above) |

Load and unload return 403. Rate limits: 120 requests and 30 jobs per minute
per key.

**OpenAI-compatible speech (LiteLLM gateway, port 4000).**

```bash
curl -sS http://100.105.214.61:4000/v1/audio/speech \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-voice", "voice": "aiden", "input": "Hello from gx-voice.",
       "response_format": "mp3", "instructions": "warm and upbeat"}' -o speech.mp3
```

* `voice`: a preset speaker, a saved voice name (unique) or a voice id.
  OpenAI voice names such as `alloy` are rejected (400) rather than
  substituted.
* `response_format`: `mp3` (default), `opus`, `aac`, `flac`, `wav`, `pcm`
  (raw 24 kHz mono 16-bit little-endian).
* `speed`: 0.5-2.0. `instructions`: as above. `input`: ≤ 4 096 characters.
* The response is the whole file (no streaming). gx10-02 keeps no copy of
  the audio after answering.
