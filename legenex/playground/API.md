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

`GET /api/media/options` returns `{kinds, image_sizes[], video_sizes[], stats}`.

`POST /api/media/jobs` takes `{kind, ...}` and returns 202 with a media job.
The fields per kind:

| kind | fields |
|---|---|
| `t2i` | `prompt*`, `negative_prompt`, `size` (image_sizes), `n` 1-4, `quality` standard/fast/hd, `steps` 1-100, `guidance` 0-20, `seed`, `title`, `uncensored` (bool) |
| `edit` | `prompt*`, `source_id*` (image asset), `strength` 0.05-1, `steps` 1-50, `seed`, `title` |
| `variation` | `source_id*` (image), `prompt` (optional), `strength`, `seed`, `title` |
| `t2v` | `prompt*`, `size` (video_sizes), `seconds` 0.5-10, `fps` 8-24, `seed`, `title` |
| `i2v` | `prompt*`, `source_id*` (image), `size`, `seconds`, `fps`, `seed`, `title` |
| `v2v` | `prompt*`, `source_id*` (video), `size`, `seconds`, `strength` 0.05-1, `seed`, `title` |

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
  `{asset, root, tree: [{brief, children: [...]}]}`.
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
