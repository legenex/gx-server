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
| text → image | Qwen-Image-2512 fp8 + Lightning 4-step + NSFW-capable adapter (perpetual3x Tumblr-NudeShot, non-commercial licence) |
| image edit / variation | Qwen-Image-Edit-2511 fp8mixed + Lightning 4-step (optional NSFW edit adapter) |
| text → video, image → video | Wan 2.2 A14B T2V / I2V fp8 + rzgar uncensored 4-step LoRAs |
| video edit | keyframe propagation: the first frame is edited with Qwen-Image-Edit, then Wan 2.2 I2V re-renders the clip |

## API

```bash
# text to image
curl -s "$GX_BASE/images/generations" -H "Authorization: Bearer $GX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gx-image", "prompt": "a lighthouse at dawn", "size": "1024x1024"}' \
  | python3 -c 'import sys,json,base64; open("out.png","wb").write(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"]))'

# image edit (source image + instruction -> new image)
curl -s "$GX_BASE/images/edits" -H "Authorization: Bearer $GX_API_KEY" \
  -F model=gx-image -F image=@photo.png \
  -F prompt="Change the background to a sunset beach and make the shirt black."

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
| `uncensored` | images | `false` disables the NSFW adapter |
| `strength` | edits | image: 0.05–1 (1 = follow the instruction fully); video: ≥ 0.75 instruction edit, 0.5–0.75 keeps more structure, lower = light restyle |
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
