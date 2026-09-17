# Media: generate and edit

`gx-image` and `gx-video` run on gx10-02 behind the media router. Clients
reach them only through the gateway with a key that allows those aliases.
Nothing is filtered: the image and video stacks are uncensored.

## In GX-Playground

Creative work lives in **GX-Playground** (`http://100.105.214.61:8090/`,
same sign-in as the Control Center). The Images and Video workspaces run
generate, edit, variation, text-to-video, image-to-video and video edit. The
Music studio runs gx-music. Each job shows its phase (queued → waiting for
resource → loading model → generating → saving → complete, or failed /
cancelled) and, while it waits, **why** it is waiting (for example "Waiting
for gx-reason to unload", with the memory needed and available).

Results go to the **Library**, which stores every item permanently on
gx10-01 in `/srv/projects/gx-cluster/media` with its full recipe and lineage.
Edits never overwrite: an edit is a new item linked to its source. Library
actions: grid/list, search, filters, sort, preview/play, open in editor,
rename, favourite, duplicate, download, lineage, select / select all / clear,
bulk favourite, bulk delete and bulk ZIP.

## Models

| Operation | Model |
|---|---|
| text → image | **Qwen Image 2512** (default): Qwen-Image-2512 fp8 + Lightning 4-step + NSFW-capable adapter (perpetual3x Tumblr-NudeShot, non-commercial licence) |
| text → image | **VisionmasterPro_V3**: an SDXL (NoobAI, epsilon-prediction) photoreal checkpoint, `votepurchase/pornmasterPro_noobV3VAE` @75f59d1, creativeml-openrail-m |
| image edit / variation | **Qwen Image Edit 2511** (default): Qwen-Image-Edit-2511 fp8mixed + Lightning 4-step (optional NSFW edit adapter), reference-latent conditioning, optional mask |
| image edit | **VisionmasterPro_V3**: image-to-image (Restyle, Full transformation) and masked inpainting |

`gx-image` is still one alias. The model is an internal choice inside it:
request field `image_model` (`qwen-image-2512`, `qwen-image-edit-2511`,
`visionmaster-pro-v3`). Without it, generation uses Qwen Image 2512 and edits
use Qwen Image Edit 2511, exactly as before.

## Image edits (Build V3)

**Why edits used to come back unchanged.** Qwen-Image-Edit-2511 sees the
source through its reference latent and needs a *full* denoise. The Images
page sent `strength` 0.6, and the router used it as the sampler denoise on
a 4-step schedule that started from the source itself, so the result was
almost the source (measured: see `coordination/build-v3/img.md`). Edits now
always run the full denoise with the official 2511 reference method
(`index_timestep_zero`) and CFGNorm.

**Edit modes** (`edit_mode`) change what is sent to the model:

| Mode | Qwen Image Edit 2511 | VisionmasterPro_V3 |
|---|---|---|
| Change / replace | instruction + "keep everything else" | inpaint; **needs a mask** |
| Add | "add … with matching light and scale" | inpaint; **needs a mask** |
| Remove | "remove … and fill the gap" (mask grown more) | inpaint; **needs a mask** |
| Restyle | "redraw the whole image in this style" | image-to-image, strength = denoise 0.45-0.85 |
| Background | "replace the background, keep the subject" | inpaint; **needs a mask** (paint the background) |
| Subject | "change the subject, keep the background" | inpaint; **needs a mask** |
| Full transformation | no reference latent (the source reaches the model only through vision tokens); strength = denoise 0.8-1.0 | image-to-image, strength = denoise 0.7-1.0 |
| `instruct` (API default) | the instruction exactly as sent | treated as Restyle |

`strength` only applies where the table names a range; for the other Qwen
modes it is ignored and the response says `strength: null`.
`edit_quality: "quality"` (Qwen only) turns off the Lightning LoRA and runs
20 steps with CFG 4, so the negative prompt matters; `fast` (default) is 4
steps.

**Masks.** `mask` is a PNG with the same aspect ratio as the source; white
= may change. The router samples only inside the (grown, feathered) mask and
pastes the result over the source, so pixels outside it are the source's
own. Full transformation does not take a mask.
| text → video, image → video | Wan 2.2 A14B T2V / I2V fp8 + rzgar uncensored 4-step LoRAs; text → video also takes your own Wan 2.2 LoRAs (see **Video LoRAs**) |
| video edit | keyframe propagation: the first frame is edited with Qwen-Image-Edit, then Wan 2.2 I2V re-renders the clip |

## Wan 2.2 LoRAs

Text-to-video in GX-Playground can stack your own Wan 2.2 LoRAs, one chain per
expert (high noise and low noise), with presets, a workflow preview and a
video history. The files live on gx10-02 under `/srv/models/video/loras/`
(and `/srv/models/shared/loras/`); the media router lists them read-only
(`GET /v1/loras`, `POST /v1/loras/rescan`) and inserts them into the vetted
graph itself. Details: **Video LoRAs**.

Router 2.5.0 additions for text-to-video (JSON body):

| Field | Meaning |
|---|---|
| `loras` | `{"high": [{"name", "strength"}], "low": [...]}`; names from `GET /v1/loras`, strengths 0.0-1.5, at most 8 per branch; `"shared": true` on both sides for one general file on both experts; `"allow_unknown": true` for a file of unknown compatibility |
| `shift`, `cfg`, `steps`, `boundary` | ModelSamplingSD3 shift (0.5-20), CFG (1-10), total steps (2-40), step where the low-noise expert takes over |
| `sampler_name`, `scheduler` | allow-listed sampler and scheduler |

`GET /v1/videos/{id}/workflow` returns the exact graph a job ran,
`POST /v1/videos/workflow` builds one without running it, and
`POST /v1/videos/{id}/cancel` cancels a video that has not reached ComfyUI.
Failed jobs carry `error.code` (for example `lora_not_found`,
`out_of_memory`, `workflow_rejected`, `cancelled`).

## API

```bash
# text to image
curl -s "$GX_BASE/images/generations" -H "Authorization: Bearer $GX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gx-image", "prompt": "a lighthouse at dawn", "size": "1024x1024"}' \
  | python3 -c 'import sys,json,base64; open("out.png","wb").write(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"]))'

# image edit (source image + instruction -> new image)
curl -s "$GX_BASE/images/edits" -H "Authorization: Bearer $GX_API_KEY" \
  -F model=gx-image -F image=@photo.png -F edit_mode=background \
  -F prompt="a sunset beach with palm trees"
# ... limited to a painted area (white = may change)
curl -s "$GX_BASE/images/edits" -H "Authorization: Bearer $GX_API_KEY" \
  -F model=gx-image -F image=@photo.png -F mask=@mask.png -F edit_mode=remove -F prompt="the bicycle"

# text to image with VisionmasterPro_V3 (the OpenAI SDK sends it with extra_body)
curl -s "$GX_BASE/images/generations" -H "Authorization: Bearer $GX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gx-image", "image_model": "visionmaster-pro-v3", "prompt": "portrait photo, window light", "size": "832x1216"}'

# text to video (asynchronous)
curl -s "$GX_BASE/videos" -H "Authorization: Bearer $GX_API_KEY" \
  -F model=gx-video -F prompt="waves rolling onto a beach" -F seconds=3 -F size=640x640

# image to video: add the start frame as input_reference
curl -s "$GX_BASE/videos" -H "Authorization: Bearer $GX_API_KEY" \
  -F model=gx-video -F prompt="the camera slowly orbits" -F input_reference=@start.png

# poll, then download (use the id returned by the create call)
curl -s "$GX_BASE/videos/$VIDEO_ID" -H "Authorization: Bearer $GX_API_KEY"
curl -s "$GX_BASE/videos/$VIDEO_ID/content" -H "Authorization: Bearer $GX_API_KEY" -o out.mp4

# video edit: upload a video ...
curl -s "$GX_BASE/videos/edits" -H "Authorization: Bearer $GX_API_KEY" \
  -F model=gx-video -F video=@clip.mp4 -F prompt="make this scene take place at night"
# ... or edit an earlier result
curl -s "$GX_BASE/videos/$VIDEO_ID/remix" -H "Authorization: Bearer $GX_API_KEY" \
  -H "Content-Type: application/json" -d '{"prompt": "turn the sky stormy"}'
```

| Parameter | Where | Meaning |
|---|---|---|
| `size` | all | `WxH`, multiples of 16 |
| `seed` | all | reproducible results |
| `negative_prompt` | all | what to avoid |
| `image_model` | images | `qwen-image-2512` (generate default), `qwen-image-edit-2511` (edit default), `visionmaster-pro-v3`; also accepted as `gx.image_model` |
| `edit_mode` | image edits | `instruct` (default), `change`, `add`, `remove`, `restyle`, `background`, `subject`, `transform` |
| `edit_quality` | Qwen edits | `fast` (4 steps) or `quality` (20 steps, CFG 4) |
| `mask` | image edits | PNG, white = may change, same aspect ratio as the image |
| `quality_tags` | VisionmasterPro_V3 | `false` stops the router appending the checkpoint's quality tags |
| `uncensored` | Qwen images | `false` disables the NSFW adapter |
| `strength` | edits | image: 0–1 where the edit mode uses it (see above); variation: below 0.5 keeps the scene, higher re-imagines it; video: ≥ 0.75 instruction edit, 0.5–0.75 keeps more structure, lower = light restyle |
| `seconds` | video | 0.5–10 (frames are snapped to Wan's 4k+1 rule) |

Status objects follow OpenAI's video API: `status` is `queued`,
`in_progress`, `completed` or `failed`. Generations run one at a time. A
queued video that waits for memory also has `phase: "waiting"` and a
`waiting` object that says why.

## Limits

* Uploads: images up to 25 MB and 4096 px per side; videos up to 150 MB, of
  which the first 10 seconds are used.
* Media generation is refused while gx-max holds the cluster.
* **gx10-02 always keeps 30 GiB free (D-038).** A job starts only if the
  node still has at least 30 GiB available once the job's own memory is
  counted:

  | Job | Memory it takes | So this must be available |
  |---|---|---|
  | image (generate, edit, variation) | about 57 GiB | about 87 GiB |
  | video (t2v, i2v, restyle edit) | about 72 GiB | about 102 GiB |
  | keyframe video edit (strength 0.5 or more) | about 107 GiB | **cannot run** (B-028) |

  With the weights already loaded, a job needs only its extra memory plus the
  reserve.
* **When a job does not fit:**
  * **A video waits.** Its status stays `queued`, with `phase: "waiting"` and
    a `waiting` object: reason, required / available / reserve GiB, what
    blocks it, what happens next. It re-checks every 15 seconds for up to
    30 minutes.
  * **An image request is refused** with HTTP 503 and the same details. The
    Control Center and GX-Playground queue it instead.
* **gx-music:** an idle music engine is unloaded automatically to make room,
  unless it is pinned or the Music profile is active.
* **gx-reason:** it uses about 44 GiB, so while it is loaded neither video
  nor images fit. They wait until it idles out (20 minutes) or you unload it
  in Resource Control.
* **Keyframe video edits are refused** (HTTP 422 `exceeds_node_reserve`,
  137 GiB would be needed). Use a strength below 0.5 (the restyle edit)
  instead.
* Idle media models are unloaded after 10 minutes. A gx-reason request that
  has to start the model also unloads them straight away, unless an image or
  video is being generated at that moment. In that case gx-reason may fail to
  start; retry when the job has finished.
* The first media job after gx-reason has been used may take a little longer
  while the media models load again.
