# Video LoRAs (Wan 2.2)

GX-Playground → **Video** → **Text to Video** can apply your own Wan 2.2
LoRAs on top of the standard Wan 2.2 T2V-A14B workflow. You pick LoRAs from a
library, set a strength per expert, and the application builds the ComfyUI
graph for you. You never edit a graph by hand.

## How Wan 2.2 uses LoRAs

Wan 2.2 T2V-A14B has **two experts**:

* the **high-noise** model draws the first half of the steps (layout, motion);
* the **low-noise** model draws the second half (detail, texture).

Most Wan 2.2 LoRAs therefore ship as **two files**: one for the high-noise
model and one for the low-noise model. The application keeps the two paths
apart:

    high-noise model → built-in LoRA → your high LoRAs … → sampler (steps 0-2)
    low-noise model  → built-in LoRA → your low LoRAs …  → sampler (steps 2-4)

* A high-noise file is only ever applied to the high-noise model.
* A low-noise file is only ever applied to the low-noise model.
* A **general** LoRA (one file meant for both experts) is applied to both
  only when you choose **Both experts** for it. Nothing is applied to both
  paths silently.

The rest of the known-good graph (text encoder, VAE, shift, samplers, video
encoding) is unchanged. With no LoRAs the graph is exactly the standard one.

## Where LoRA files live

LoRA files are on **gx10-02**, in the folders ComfyUI reads:

| Folder | Use |
|---|---|
| `/srv/models/video/loras/` | video LoRAs (subfolders included) |
| `/srv/models/video/loras/wan22/paired/` | pairs: `<name>_high_noise.safetensors` + `<name>_low_noise.safetensors` |
| `/srv/models/video/loras/wan22/high_noise/` | files for the high-noise expert only |
| `/srv/models/video/loras/wan22/low_noise/` | files for the low-noise expert only |
| `/srv/models/video/loras/wan22/general/` | general LoRAs (you choose the branch) |
| `/srv/models/shared/loras/` | LoRAs shared by several model families |

* Only `.safetensors` files are listed. Existing files may stay where they
  are; the `wan22/` folders are a suggestion, not a requirement.
* Copy files in with your usual tools (for example `scp` to gx10-02), then
  press **Rescan** in the LoRA library. No restart is needed.
* The name shown and used is exactly the name ComfyUI uses: the path below
  the LoRA folder, for example `wan22/paired/Style_high_noise.safetensors`.
* Files are never renamed, moved or modified by the application.

## What the scan checks

For each file the media router reads **only the header** (a bounded read of
the safetensors length and JSON header; the weights are never read):

* **Valid file:** a readable safetensors header whose tensors fit the file.
* **Compatibility with Wan 2.2 T2V-A14B:**
  * *compatible*: Wan transformer block keys with hidden size 5120 and at most
    40 blocks (the 14B layout);
  * *incompatible*: Wan 5B (3072) or 1.3B (1536) LoRAs, Qwen-Image, FLUX or
    Stable Diffusion LoRAs, invalid files;
  * *unknown*: anything the scan cannot place (for example Wan 14B
    image-to-video LoRAs, or unfamiliar key names).
* **High / low classification** from, in order of evidence: the folder
  (`high_noise/`, `low_noise/`, `general/`), the file name (`high_noise`,
  `HighNoise`, `high`, `HN` and the low equivalents, any separator or case) and
  the header metadata. Conflicting markers make the file *unresolved*.
* **ComfyUI visibility:** whether ComfyUI already lists the file.

## Pairs

* **Automatic pairs:** a high file and a low file in the same folder whose
  names match once the noise marker is removed. When several files could
  match, nothing is paired automatically.
* **Pair by hand:** LoRA library → **Pair files by hand**: choose a
  high-noise and a low-noise file, press **Pair**. The pair is stored in the
  application database.
* **Unpair:** press **Unpair** on a pair. An automatic pair stays split until
  you pair it again.
* A pair whose file disappeared is shown as *High file missing* or *Low file
  missing* and can only be used for the half that still exists.

## Using LoRAs

1. Open **Video** → **Text to Video**.
2. Under **LoRAs**, press **Add LoRA**. Search, filter (paired, high only,
   compatible, …) and sort the library, then press **Add** on the LoRAs you
   want.
3. For each LoRA in the list:
   * **Enabled** switches it on or off without removing it;
   * **High noise** / **Low noise** set its strength for each expert
     (0.0-1.5, default 0.8 for a pair);
   * a general or unresolved LoRA asks **Applies to**: high noise, low noise
     or both;
   * **Move up / Move down** change the application order (keyboard
     accessible); **Remove** takes it out.
4. With two or more enabled LoRAs a hint offers **Use 0.5 for each** as a
   starting point. Your values stay unless you press it.
5. Press **Generate video**.

**Expected result:** a video in the viewer, a history entry listing the
LoRAs and strengths, and a stored workflow.

## LoRA library

**LoRA library** (next to Add LoRA) opens the full library:

* each entry shows its name, pair state, compatibility, files, size, date
  discovered and default strengths;
* **Details** shows every file's exact path on gx10-02, size, noise class and
  reason, compatibility and reason, key format, rank, hidden size, block
  count and header metadata;
* the settings form sets the display name, description, tags, default
  strengths, **Available for selection** (disable an entry) and **Allow unknown
  compatibility** (only for files you know work with Wan 2.2 T2V-A14B);
* **Move up / Move down** set the library order;
* **Rescan** reads the folders again and asks ComfyUI again.

## Presets

A preset stores the LoRAs and their order and strengths, size and aspect
ratio, length and frame count, frame rate, seed mode and seed, style wording,
negative prompt, and the sampler settings (steps, switch step, shift, CFG,
sampler, scheduler).

* **Apply** fills the form; everything stays editable.
* **Save as…** stores the current form as a new preset; **Update** replaces
  the selected preset with the current form.
* **Manage** lists presets with Apply, Rename, Duplicate and Delete.
* The five example presets (Cinematic Realism, High Detail, Character
  Consistency, Motion Style, Custom 1) contain settings only and no LoRA
  files. Their ids are fixed.
* Creative Flows reference presets by id (`wp_…`).

## Advanced settings and the advanced view

**Advanced settings** holds shift (5.0), CFG (1.0), steps (4), the step at
which sampling switches to the low-noise expert (2), sampler (euler) and
scheduler (simple). The defaults match the built-in 4-step distillation
LoRAs; raising CFG burns the output with them.

**Preview workflow** builds the exact graph without running it and shows:
the high and low chains in application order with file names and strengths,
the model files, the workflow version, and the ComfyUI workflow JSON with
**Copy JSON** and **Download JSON**. The same view is available for every
finished generation in **Video history → Details**, together with the
ComfyUI prompt id.

Exported workflows contain model file names only: no host paths, addresses,
node names or keys.

## Video history and errors

**Video history** lists every video generation with its prompt, seed, size,
frames, frame rate, LoRAs and strengths, status and run time. For each entry:

* **Play in viewer**;
* **Load settings into the form** (prompt, negative prompt, LoRAs, strengths,
  sampler settings, seed);
* **Run again** with the same settings;
* **Details**: the full record, the video, **Asset metadata**, **Reuse in
  Creative Flows**, and the advanced view with the workflow.

**Errors** lists failed and cancelled generations with a plain explanation
and the technical details, for example:

| Message | What to do |
|---|---|
| A selected LoRA file is no longer on gx10-02 | Rescan, then pick it again |
| The high- (low-) noise file of a paired LoRA is missing | Rescan, re-pair, or use the remaining half |
| not a valid safetensors file / not compatible | Use another file |
| compatibility could not be determined | Allow unknown compatibility for it, if you trust it |
| ComfyUI does not list a selected LoRA yet | Press Rescan |
| gx10-02 ran out of memory | Fewer LoRAs, a shorter clip or a smaller size |
| ComfyUI rejected the generated workflow | See the details; report it if it repeats |
| Waiting for … memory | Nothing: the job starts when gx10-02 has room |

A job that is still queued or waiting can be cancelled from its card.

## API (browser session)

All routes need a signed-in session and, for POST, the CSRF token.

| Route | Purpose |
|---|---|
| `GET /api/video/config` | defaults, ranges, sizes, samplers, model |
| `GET /api/video/loras` | library entries and files (`?refresh=1` re-reads) |
| `POST /api/video/loras/rescan` | rescan gx10-02 |
| `GET/POST /api/video/loras/{entry}` | details / settings |
| `POST /api/video/loras/order` | library order |
| `POST /api/video/pairs`, `/pairs/remove`, `/pairs/restore` | pairing |
| `GET/POST /api/video/presets`, `/presets/{id}` (+ `/duplicate`, `/delete`, `/resolve`) | presets |
| `POST /api/video/workflow` | build the graph (preview) |
| `POST /api/video/generate` | submit; poll `GET /api/media/jobs/{id}` |
| `POST /api/video/jobs/{id}/cancel` | cancel while queued or waiting |
| `GET /api/video/generations`, `/generations/{id}`, `/generations/{id}/workflow` | history, details, workflow download |
| `GET /api/video/errors` | recent failures |

## Limits

* LoRAs apply to **text to video** only (image to video and video edit use
  their own graphs without user LoRAs).
* At most 8 LoRAs per expert branch.
* Each LoRA adds memory on gx10-02; the usual 30 GiB reserve and waiting
  rules apply.
* The public gateway API (`gx-video` through LiteLLM) does not take LoRAs.
