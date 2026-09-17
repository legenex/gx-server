# IMG — gx-image: model selector, image editing, VisionmasterPro_V3 (Build V3)

Owner: image specialist. Status: **code complete, live acceptance NOT run**
(the lead holds node 2 for the media memory measurement; the acceptance plan
and its script are below, ready to run).

> **Nothing in this file claims a generation or a measurement that did not
> happen.** The only real gx-image output produced in this wave is the baseline
> source image in `/srv/logs/acceptance/build-v3/img/baseline/source.png`
> (router job `image-c087349dec2743b1`, `qwen-image-2512-lightning`, 26.2 s,
> recorded in `source.json`). The six edit files next to it are **0 bytes**:
> those runs never produced anything. See "Corrections" below.

---

## 1. The traced code path, hop by hop

Legend: **OK** = works today · **FIXED** = was wrong or misleading, changed in
this wave · **NEW** = added in this wave · **LEAD** = needs the lead's line.

| # | Hop | Where | State |
|---|---|---|---|
| 1 | Images page loads the catalogue | `legenex/playground/web/js/pages/images.js:47` → `GET /api/media/options` | OK |
| 2 | Model `<select>` (real, per operation) | `images.js:77`, `renderModels()` `images.js:167`, `setModel()` `images.js:199` | OK |
| 3 | Request body: **the model field is `image_model`** | `images.js:354` `body.image_model = model.id` | OK (verified: `image_model`, not `model`) |
| 4 | NSFW adapter flag | `images.js:355` `body.uncensored = draft.uncensored[adapterSlot()]` | FIXED (see §4) |
| 5 | SDXL quality tags | `images.js:356`, only for `kind === 't2i'` | FIXED (see §4) |
| 6 | Submit | `images.js:391` `submitMedia(body)` → `playground/web/js/jobs.js:133` `POST /api/media/jobs` | OK |
| 7 | Playground proxy allow-list | `playground/gx_playground/server.py:76` `/api/media/(assets\|jobs\|options\|upload\|delete\|zip)` | OK (no new entry needed) |
| 8 | Control Center route | `control-ui/gx_control_ui/server.py:1052` `api_media_job_submit` | OK |
| 9 | Server-side validation | `gx_control_ui/media_jobs.py:160` `validate()`; model resolved at `:180`, stored at `:183` | OK |
| 10 | Catalogue (single source) | `gx_control_ui/image_catalog.py:62` `ImageCatalog` imports the router's `image_models.py` from the same checkout | OK |
| 11 | Edit mask decoded, bounded, measured | `media_jobs.py:355` → `image_catalog.py:177` `parse_mask` / `:111` `png_mask_stats` | OK |
| 12 | Job → router | `media_jobs.py:555` `POST /v1/images/generations`, `:574` `/v1/images/edits` or `/v1/images/variations` (multipart, mask as a file part) | OK |
| 13 | Router picks the model | `media/router/gx_media_router/server.py:386` `_image_model` (`image_model`, else `gx.image_model`, else the workflow's model, else the operation default) → `image_models.py:186` `resolve` | OK |
| 14 | Generate: workflow selection | `server.py:400` `_images` — SDXL branch `:411`, Qwen branch `:428` | OK |
| 15 | Edit: **the prompt adapter and the plan** | `image_models.py:232` `plan_edit` (template, reference-latent method, denoise, mask grow) · `:284` `plan_variation` | OK |
| 16 | Adapter strength (`uncensored`) | `server.py:357` `_adapter`; defaults `T2I_ADAPTER_DEFAULT = 0.6`, `EDIT_ADAPTER_DEFAULT = 0.0` (`server.py:86`) | OK |
| 17 | Graph build → ComfyUI | `service.py:198` `generate_image` → `workflows.py:53` `build` → `legenex/media/workflows/*.api.json` | OK |
| 18 | Response → Library asset | `media_jobs.py:676` `_store_images` writes `settings.image_model`, `image_model_label`, `image_model_family`, `settings.edit`, `settings.mask`, `parent_id` | OK |
| 19 | Provenance of the checkpoint | `server.py:247` `workflow_identity` picks the checkpoint whose `image_model` matches (`server.py:255`) | OK |
| 20 | Durable history / lineage / provenance | `image_catalog.py:363` `ImageHistory` + `migrations/070_images.sql` + `routes_img.py` | NEW / LEAD |
| 21 | Library + History UI | `playground/web/js/assets.js:256` shows "Image model"; `assets.js:219` lineage view | OK |

### What already worked before this wave

The bulk of IMG was already built and is correct:

* The Images page has a **real** model selector driven by
  `GET /api/media/options` → `image_models.py`; the chosen id really reaches
  the router in `image_model` and really selects the workflow. It is filtered
  per operation, remembers a choice per slot, and drives sizes, quality
  presets, edit modes, the mask box and the strength slider.
* All three models are on disk on gx10-02 and in the registry, with hashes.
* The image-edit bug is already fixed at the root (`strength` is no longer a
  denoise for the reference-latent modes) — see §5.
* Masks are validated server-side (size, aspect ratio, coverage, decompression
  bound) and only their metadata is stored, never the image.
* 22 router tests, 10 Control Center tests and 4 offline Playwright tests
  covered it.

### What was broken or missing

1. **Unsupported "measured" claims** in shipped code and docs (§2).
2. **A decorative control**: the "Quality tags" switch was shown for
   VisionmasterPro_V3 edits, but `quality_tags` is only sent for `t2i` (§4).
3. **The NSFW edit adapter was on by default from the Playground** although the
   router's own default is off, and the adapter is not a 2511 LoRA (§4).
4. **No migration 070 and no durable image history** — nothing recorded which
   model made which picture once the process restarted (§6).
5. **No runtime signal for "the edit came back as a copy"** (§6).
6. The three Qwen text-to-image templates did not declare `_gx.image_model`,
   so `WorkflowSpec.image_model` was empty for them (fixed).
7. **A real ordering bug in `MediaJobs`** that affects every observer (§9).

---

## 2. Corrections: claims that were not backed by evidence

`image_models.py` and `qwen-image-edit-2511.api.json` said the near-copy
behaviour was *"measured 2026-09-17, coordination/build-v3/img.md"*, and
`docs/11-media.md` repeated it. There is no such measurement:
`/srv/logs/acceptance/build-v3/img/baseline/` holds a real generated
`source.png` plus **six zero-byte** result files (`bg_s0.6.json`,
`bg_s1.0.json`, `cloth_*`, `style_*`), and this file did not exist.

Changed to state the mechanism (which *is* established by reading the graphs)
and to say plainly that the A/B is the acceptance run and has not happened:

* `legenex/media/router/gx_media_router/image_models.py` (module docstring,
  `plan_variation` docstring)
* `legenex/media/workflows/qwen-image-edit-2511.api.json` (`_gx.note`)
* `legenex/control-ui/docs/11-media.md`

`legenex/models/registry.json` still carries
`aliases.gx-image.measured = "1024x1024 generation 29 s; instruction edit 31 s;
variation 16 s (2026-09-17)"` and `variants["visionmaster-pro-v3"].footprint =
"measured live; see coordination/build-v3/img.md"`. The first predates this
wave; the second is **not** backed by anything — VisionmasterPro_V3 has never
generated an image on this cluster. The replacement entries in §8 fix it.

---

## 3. Models: what is really on disk (gx10-02, read-only check)

| UI label | id | Files (ComfyUI roots) |
|---|---|---|
| Qwen Image 2512 | `qwen-image-2512` | `image/diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors` (20.4 GB) + `image/loras/Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors` + `image/loras/tumblr.safetensors` |
| Qwen Image Edit 2511 | `qwen-image-edit-2511` | `image/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors` (20.5 GB) + `image/loras/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` + `image/loras/qwen-image-edit-plus-nsfw-lora.safetensors` |
| VisionmasterPro_V3 | `visionmaster-pro-v3` | `image/diffusion_models/pornmasterPro_noobV3VAE/unet.safetensors` (10.27 GB) · `image/text_encoders/pornmasterPro_noobV3VAE/{clip_l,clip_g}.safetensors` · `image/vae/pornmasterPro_noobV3VAE/vae.safetensors` |

**VisionmasterPro_V3 was NOT re-downloaded.** It is the existing checkpoint
from earlier work: `votepurchase/pornmasterPro_noobV3VAE` @
`75f59d136b165d48f3e678bb057af99f7cf1a71e`, diffusers-format SDXL/NoobAI,
licence creativeml-openrail-m. Its manifest
(`/srv/models/image/pornmasterPro_noobV3VAE/.gx-manifest.json`, copy in
`/srv/logs/acceptance/build-v3/img/visionmaster-manifest.json`) records
`hash_checked: true` and the sha256 of each file; the four sha256 values match
the four entries in `registry.json`. "VisionmasterPro_V3" is the public label
the cluster gives that checkpoint; the file names keep the upstream name.

**Unverified on this node:** ComfyUI has never loaded these four files. They
are diffusers-format tensors going into `UNETLoader`, `DualCLIPLoader(type=sdxl)`
and `VAELoader`; ComfyUI converts diffusers keys, but that conversion has not
been exercised here. Case `src_vm` of the acceptance plan is the first real
test — if it fails, it will fail at load time with a key error, not silently.

---

## 4. The model selector and the two misleading switches

The selector itself was already real, so it was kept. Two switches next to it
were not honest, and both are image-specific UI I own:

**"Quality tags" (VisionmasterPro_V3).** It was visible in Edit mode, but
`generate()` only sent `quality_tags` for `kind === 't2i'`, so on an edit it did
nothing: a decorative control. The router applies the NoobAI quality tags to
SDXL edits regardless (`server.py:496`). Fixed by hiding the switch outside
generation (`images.js:246`) and saying so in the hint and in the docs. If you
would rather make it controllable on edits, that needs one line in
`media_jobs.py`, which I do not own — see §9, request R-3.

**"Uncensored adapter".** This is a real control (it sets
`adapter_strength` on a `LoraLoaderModelOnly` node), not a label, so it stays.
But the page sent `uncensored: true` for **edits** too, which makes the router
apply `ScottzillaSystems/qwen-image-edit-plus-nsfw-lora` at 0.8 — a LoRA that
`registry.json` itself records as *"NOT exact: byte-identical to a Qwen-Image
(original) LoRA; loads on 2511 by key"*. The router's own default for edits is
0.0. A mismatched LoRA at 0.8 on a 4-step edit is a plausible second cause of
weak edits, so the page now keeps **two** defaults, matching the router:
generation on (0.6), edits and variations off. One switch, remembered per
operation (`images.js:22-26`, `:126`, `:241`).

*This is a behaviour change a user can notice: an edit is no longer
NSFW-adapted unless they switch it on. It is one click, the hint explains it,
and cases `edit_adapter_off` / `edit_adapter_on` in the acceptance plan measure
whether the adapter helps or hurts. Revert by changing `uncensored:
{ generate: true, edit: false }` back to `edit: true` in `images.js`.*

No other safety or platform control was touched. There is **no** content
filter anywhere in the image path — verified by grep over
`media/router/gx_media_router/`, `media_jobs.py` and `image_catalog.py`: no
NSFW classifier, no prompt blocklist, no post-generation filtering. The only
prompt rewriting is listed in §5.

---

## 5. VisionmasterPro_V3: what touches its prompt, and what does not

Verified by reading the code, not by inference:

| Question | Answer | Evidence |
|---|---|---|
| Qwen instruction templates applied to it? | **No.** `plan_edit` returns the raw `instruction` for `VISIONMASTER` and never formats `qwen_template` | `image_models.py:239-256` |
| Qwen reference-latent method? | **No.** Only the Qwen branch sets `reference_method` | `image_models.py:267-271` |
| Qwen NSFW adapter? | **No.** `adapter_strength` is set only when `model.id == QWEN_EDIT`; generation sets `adapter = None` for SDXL | `server.py:524-525`, `server.py:431` |
| Any filtering of prompt or output? | **No.** Nothing in the router or the Control Center inspects prompts or pixels for content | grep, §4 |
| Is the model explicit or inferred? | **Explicit.** `image_model` is carried end to end; inference from a named `workflow` happens only when the caller sent no `image_model` | `server.py:386-398` |
| Does the workflow really load its own files? | Yes — the three SDXL templates name only `pornmasterPro_noobV3VAE/*` | `sdxl-visionmaster-pro-v3*.api.json` |

**Normalisation that does apply, by design:**

1. **Quality tags.** `", masterpiece, best quality, amazing quality, very
   aesthetic, absurdres, newest"` is appended for SDXL generation unless
   `quality_tags: false`, and **always** on SDXL edits. NoobAI-family
   checkpoints are trained with these tags. The router reports it back as
   `gx.prompt_suffix`. (`image_models.py:60`, `server.py:418-423`, `:496-501`)
2. **Default negative prompt.** If the caller sends none, SDXL gets
   `SDXL_NEGATIVE_DEFAULT` (`worst quality, low quality, …`). Sending
   `negative_prompt: ""` is *not* "none" — an explicit empty string is kept.
   (`image_models.py:61`)
3. **Canvas limit.** Over 1.6 Mpx is refused with the list of trained sizes;
   SDXL duplicates subjects on larger canvases. (`image_models.py:56`,
   `server.py:415`)
4. **Edit mode → denoise.** For SDXL, `strength` is a real image-to-image
   denoise and is mapped into the mode's range (`sdxl_denoise`), e.g. Restyle
   0.45-0.85, Background 0.75-1.0. (`image_models.py:98-135`, `:246`)
5. **`instruct` is remapped to `restyle`** for SDXL, and the instruction-style
   modes require a mask, because SDXL cannot follow edit instructions.
   (`image_models.py:236-241`)

---

## 6. Image editing: the suspect, re-inspected

**The prime suspect was already fixed before this wave**; I re-checked it
rather than re-fixing it.

* The Playground `draft.strength` is still 0.6, but `generate()` only sends
  `strength` when the slider is visible (`images.js:370`), and the slider is
  visible only when the selected mode's `strength_applies` is true
  (`images.js:229-230`). For Qwen Image Edit 2511 every mode except **Full
  transformation** has `qwen_denoise = (1.0, 1.0)`, so `strength_applies` is
  false and no strength is sent.
* `validate()` drops `strength` for a mode whose `strength_applies` is false
  (`media_jobs.py:203-204`), so even a direct API call cannot smuggle it in.
* `plan_edit` computes the denoise from the **mode**, never 1:1 from the
  caller (`image_models.py:257-261`). `KSampler.denoise` is bound from
  `plan.params["denoise"]`, which is 1.0 for every reference-latent mode.
* Router test `test_regression_strength_never_becomes_a_partial_qwen_denoise`
  pins this.

**The 2511 reference-latent method** is correct and matches the official
ComfyUI template: `TextEncodeQwenImageEditPlus` (with the VAE, so it emits both
vision tokens and the reference latent) → `FluxKontextMultiReferenceLatentMethod`
with `index_timestep_zero` on both the positive and the negative conditioning →
`CFGNorm` → `KSampler` at full denoise over the VAE-encoded source, which only
fixes the latent size. The three templates differ exactly where they should:

| Template | Source reaches the model via | Latent | Mask |
|---|---|---|---|
| `qwen-image-edit-2511` | vision tokens + reference latent | VAE-encoded source, denoise 1.0 | — |
| `qwen-image-edit-2511-masked` | the same | `SetLatentNoiseMask` + `DifferentialDiffusion`, then `ImageCompositeMasked` over the scaled source | grown + feathered |
| `qwen-image-edit-2511-transform` | vision tokens only (no VAE on the encoder, no reference-latent node) | denoise 0.8-1.0 from `strength` | refused |

Two things are still **unproven without a live run**: whether a full denoise
with `index_timestep_zero` actually preserves identity in practice on this
checkpoint, and whether the masked composite lands pixel-accurate. Both are
acceptance cases.

**New: the near-copy becomes a number.** Every finished edit or variation is
now compared with its own source with a 64-bit difference hash
(`image_catalog.py:334` `dhash`, `:502` `_similarity`), and the distance is
stored per output. ≤ 6 bits is flagged `near_duplicate`. Pillow is used when
present (it is, on the system Python that runs the service) and a pure-stdlib
PNG decoder otherwise (0.64 s for 1024×1024, measured here). The comparison
never fails a job.

---

## 7. Migration 070 and the history store

`legenex/control-ui/gx_control_ui/migrations/070_images.sql`, modelled on
`050_call_agents.sql` and the migrations README:

| Table | One row per | Carries |
|---|---|---|
| `img_generations` | image job | user, status, kind, model id/label/family, **checkpoint repository + revision that actually ran**, workflow, edit mode, edit quality, denoise, strength applied, mask (coverage, sha256, source), prompt + `prompt_sent` + `prompt_suffix`, adapter strength, uncensored, quality tags, size, seed, steps, guidance, batch, source asset, router job, timings, error code/message/detail, `flow_*` |
| `img_outputs` | produced image | asset id, index, operation, model, workflow, size, bytes, sha256, **dhash**, `source_asset_id` (edit lineage), `source_dhash`, `similarity_method/_distance/_score`, `near_duplicate` |
| `img_checkpoints` | (model, workflow, repository, revision) | `first_seen`, `last_seen`, `generations` — what the cluster actually ran, not a copy of the registry |

> **Naming note for the lead:** the migrations README says the 070 prefix is
> `image_`; my brief said `img_`. I used **`img_`**. Request R-2 updates the
> README row.

Writer: `image_catalog.ImageHistory` (`image_catalog.py:363`), a `MediaJobs`
observer — nothing in the job path knows about it and an exception there cannot
fail a job. Reader: `gx_control_ui/routes_img.py`, four read-only,
session-authenticated browser routes (`GET /api/images/generations`,
`…/generations/{id}`, `…/assets/{id}/lineage`, `…/models`). No new Playground
proxy ALLOW entry is needed, because the Playground does not call them.

**Dry run** — on a **copy** of the live database, never the live file
(`cp /srv/projects/gx-cluster/media/metadata/library.db <scratch>/library.db`):

```
before: assets 25, migrations 010,020,030,040,050,080
after : assets 25, migrations 010,020,030,040,050,070,080
img tables   : img_checkpoints, img_generations, img_outputs
img indexes  : 8
integrity_check: ok
re-run        : ok (every statement is IF NOT EXISTS)
```

---

## 8. What the lead must add

### R-1 — `legenex/control-ui/gx_control_ui/server.py` (two lines)

In the existing IMG block in `App.__init__` (currently lines 183-186):

```python
        # --- Build V3 IMG: gx-image model catalogue (router image_models.py) for validation and options
        from .image_catalog import ImageCatalog, ImageHistory
        self.image_catalog = ImageCatalog(cfg.repo_root)
        self.media.catalog = self.image_catalog
        #: durable gx-image history, provenance and edit lineage (migration 070)
        self.image_history = ImageHistory(self.library, self.media, self.image_catalog)
```

and next to the other route imports at the end of the file (after line 1249):

```python
from . import routes_img  # noqa: E402,F401  (Build V3 IMG: gx-image history, provenance, lineage)
```

### R-2 — `legenex/control-ui/gx_control_ui/migrations/README.md`

```
| 070 | image models / edits |
```
→
```
| 070 | image models / edits (tables are prefixed `img_`) |
```
and in the prefix list, `image_` → `img_`.

### R-3 — optional, `legenex/control-ui/gx_control_ui/media_jobs.py`

Only if you want the Quality-tags switch to work on SDXL **edits** as well.
In `validate()` (around line 194) move the `quality_tags` block out of the
`if kind == "t2i":` branch into `if kind in ("t2i", "edit"):`, and in `_run()`
add `"quality_tags": p.get("quality_tags")` to the edit `fields` dict (line
570). I would then drop the `isGen` condition in `images.js:246`. Until then
SDXL edits always use the tags, which is what the docs now say.

### R-4 — `legenex/models/registry.json`, `aliases["gx-image"]["variants"]`

Replace the current three-key dict with this list. It matches the PLT contract
(`coordination/build-v3/plt.md` §8) — the Models page keeps only
`id, label, family, repository, revision, licence, capabilities, workflows,
default, status, measured, base, resolution` — and it claims no measurement
that has not happened.

```json
"variants": [
 {
  "id": "qwen-image-2512",
  "label": "Qwen Image 2512",
  "family": "qwen-image",
  "repository": "Comfy-Org/Qwen-Image_ComfyUI",
  "revision": "not recorded at download (MODELS.md)",
  "licence": "apache-2.0",
  "base": "Qwen-Image-2512 fp8_e4m3fn + Lightning 4-step LoRA",
  "resolution": "512x512 to 1664x928; default 1328x1328",
  "capabilities": ["text-to-image", "quality fast/standard/hd", "negative prompt", "NSFW adapter on by default (0.6)"],
  "workflows": ["qwen-image-2512-uncensored", "qwen-image-2512-lightning", "qwen-image-2512-quality"],
  "default": "generation",
  "status": "in use",
  "measured": "1024x1024 in 26.2 s on 2026-09-17 (router job image-c087349dec2743b1, qwen-image-2512-lightning)"
 },
 {
  "id": "qwen-image-edit-2511",
  "label": "Qwen Image Edit 2511",
  "family": "qwen-image",
  "repository": "Comfy-Org/Qwen-Image-Edit_ComfyUI",
  "revision": "7d41107b653d3039be20972fb82398b01b3213eb",
  "licence": "apache-2.0",
  "base": "Qwen-Image-Edit-2511 fp8mixed + Lightning 4-step LoRA",
  "resolution": "follows the source, fitted to the router's edit target",
  "capabilities": ["instruction edit", "masked edit", "variation", "7 edit modes", "edit quality fast/quality", "NSFW adapter off by default for edits"],
  "workflows": ["qwen-image-edit-2511", "qwen-image-edit-2511-masked", "qwen-image-edit-2511-transform"],
  "default": "editing and variations",
  "status": "in use",
  "measured": "not measured since the Build V3 edit fix; see the IMG live acceptance"
 },
 {
  "id": "visionmaster-pro-v3",
  "label": "VisionmasterPro_V3",
  "family": "sdxl",
  "repository": "votepurchase/pornmasterPro_noobV3VAE",
  "revision": "75f59d136b165d48f3e678bb057af99f7cf1a71e",
  "licence": "creativeml-openrail-m",
  "base": "SDXL / NoobAI, epsilon prediction, diffusers format (UNet + CLIP-L + CLIP-G + VAE)",
  "resolution": "832x1216 default; at most about 1.6 megapixels",
  "capabilities": ["text-to-image", "image-to-image", "masked inpainting", "negative prompt", "NoobAI quality tags", "no instruction following"],
  "workflows": ["sdxl-visionmaster-pro-v3", "sdxl-visionmaster-pro-v3-img2img", "sdxl-visionmaster-pro-v3-inpaint"],
  "default": "",
  "status": "downloaded and verified on gx10-02; never loaded by ComfyUI yet",
  "measured": "not measured; the first load is case src_vm of the IMG live acceptance"
 }
]
```

While you are in that file: `aliases["gx-image"]["uncensored"]` says the edit
adapter is "optional for edits" — still true, but it is now **off by default**
in the Playground as well as in the router.

---

## 9. A real bug found in shared code (not fixed by me — `media_jobs.py` is yours)

`MediaJobs.submit()` appends the job to the queue and notifies the worker
(`media_jobs.py:372-373`) **before** it delivers the `submitted` event to
observers (`media_jobs.py:376`). A job that fails immediately — for example a
router that is down — therefore delivers `failed` from the worker thread
*before* `submitted` arrives on the caller's thread. An observer that inserts
its row on `submitted` and updates it afterwards loses the result and keeps a
row that says `queued` for ever.

* Reproduced deterministically: `tests/test_image_history.py::HistoryTests::
  test_events_that_arrive_out_of_order_still_record_the_result`. It also
  appeared as a real flake (3 of 6 runs) before the workaround.
* `ImageHistory` is immune: its insert is `INSERT OR IGNORE` and runs on every
  event (`image_catalog.py:391-395`).
* **`wan_video.WanVideo.observe` (`wan_video.py`, the `INSERT OR IGNORE …
  wan_generations` on `submitted` followed by `UPDATE`) has exactly the same
  shape and is exposed to it.** A video job that fails fast — a router
  refusal, a bad LoRA name — will keep a `queued` row with no error.
* The clean fix is one line in `media_jobs.py`: move `self._notify(job,
  "submitted")` above the `with self._cv:` block that enqueues the job (the
  job object is fully built by then), or do the enqueue after the notify.

---

## 10. Live acceptance plan (ready to run, NOT run)

**Blocked on:** the lead's exclusive node-2 media memory measurement. Nothing
in this wave submitted a job to ComfyUI.

Runner: `legenex/media/tools/image_accept.py` (new), which drives the existing
`legenex/media/tools/image_eval.py` (SSIM, 64-bit perceptual-hash distance,
colour-histogram correlation, mean absolute difference).

```bash
# gx10-01, after the node-2 hold is released
cd /home/legenex/Documents/Projects/Server/gx-cluster
~/.venvs/gx-img-eval/bin/python legenex/media/tools/image_accept.py --list
~/.venvs/gx-img-eval/bin/python legenex/media/tools/image_accept.py
# a subset, e.g. only the edit-adapter A/B:
~/.venvs/gx-img-eval/bin/python legenex/media/tools/image_accept.py \
    --only src_portrait,edit_adapter_off,edit_adapter_on
```

It refuses to start while `state/guard/node2.gxmax-hold` or
`node2.maintenance-hold` exists, starts no container itself (every job goes
through the media router, which owns ComfyUI admission, the node flock and the
30 GiB reserve), and writes evidence to
`/srv/logs/acceptance/build-v3/img/<UTC stamp>/`: `<case>.png`, `<case>.json`
(router metadata + metrics), the mask it painted, `summary.json` and
`RESULTS.md`. Exit status is non-zero if any case FAILs.

**Verdicts.** An edit FAILs when the result is a near-duplicate of its source
(SSIM ≥ 0.90 **and** phash distance ≤ 8), when the file is missing or zero
bytes, or when the router errored. WEAK means it moved, but only just
(SSIM > 0.88 and phash < 10) — look at the images. Zero-byte evidence is never
a pass.

| Case | Covers | Model |
|---|---|---|
| `src_portrait`, `src_street` | sources for the edit cases | Qwen Image 2512 |
| `src_vm` | **first ever load of VisionmasterPro_V3** | VisionmasterPro_V3 |
| `edit_background` | (a) background replacement | Qwen Edit 2511 |
| `edit_object` | (a) object replacement | Qwen Edit 2511 |
| `edit_remove` | (a) object removal | Qwen Edit 2511 |
| `edit_style` | (b) style transformation | Qwen Edit 2511 |
| `edit_transform` | (b) full transformation, no reference latent | Qwen Edit 2511 |
| `edit_clothing` | (c) clothing modification | Qwen Edit 2511 |
| `edit_add` | (c) object addition | Qwen Edit 2511 |
| `edit_masked_lower` | mask / inpainting, masked template | Qwen Edit 2511 |
| `edit_vm_inpaint` | masked inpainting | VisionmasterPro_V3 |
| `edit_vm_img2img` | source-image transform (real denoise) | VisionmasterPro_V3 |
| `variation_high` | variation at 0.9 | Qwen Edit 2511 |
| `edit_adapter_off` / `edit_adapter_on` | A/B of the new edit-adapter default | Qwen Edit 2511 |
| `regression_strength` | strength 0.6 on an instruction edit must be ignored and the picture must still change | Qwen Edit 2511 |

**Lineage** is checked from the Playground afterwards (or from the new API):
every edit result must have `parent_id` = its source, `settings.image_model` =
the model that ran, and an `img_outputs` row whose `source_asset_id` matches
and whose `near_duplicate` is 0. `legenex/playground/e2e/live.images-models.spec.js`
already drives that through the UI: `npm run test:live` in
`legenex/playground` once the Playground is reachable.

**What to record here afterwards:** the `RESULTS.md` table, the measured
footprint of VisionmasterPro_V3 (MemAvailable at 1 Hz during `src_vm`, per
BUILD_V3 rule 2), the first-load and steady-state timings, and the verdict of
the adapter A/B.

---

## 11. Tests and results (this wave, all really run)

| Suite | Command | Result |
|---|---|---|
| Media router | `legenex/media/router/qa.sh` | **PASS** — 174 tests, 15 templates validated, secret scan clean |
| Router image unit tests | `python3 -m unittest tests.test_image_models` | **22 passed** |
| Control Center image tests | `.venv/bin/python -m unittest discover -s tests -p 'test_image*.py' -t tests` | **23 passed** (10 existing + 13 new), stable over 8 consecutive runs |
| Control Center, whole suite | `python3 -m unittest discover -s tests -t tests` | **708 passed** (system Python; the whole repo's suites, including the other workstreams') |
| Playground offline E2E (images) | `npx playwright test --project=offline e2e/offline.b-images.spec.js e2e/offline.b-images-models.spec.js` | **5 passed**, incl. 6 axe WCAG 2.2 AA checks and a 390 px layout check |
| Lint / types | `ruff check` and `mypy` on the IMG files | clean |
| Migration 070 | dry run on a copy of the live `library.db` | applied, 25 assets kept, `integrity_check ok`, idempotent |

New tests: `legenex/control-ui/tests/test_image_history.py` (13) and a third
test in `legenex/playground/e2e/offline.b-images-models.spec.js` ("the NSFW
adapter and the quality tags are only offered where they do something").

## 12. QA failures that are NOT IMG's

Reported, not "fixed" by rewriting someone else's code (BUILD_V3 rule 10):

1. `legenex/playground/scripts/build-check.mjs` fails with two findings, both
   in the built Creative Flows bundle (FLO):
   * `flows/assets/flows-KdTy-3p_.js: innerHTML/insertAdjacentHTML outside dom.setTrustedHTML`
   * `web/: asset size 1115248 B exceeds budget 614400 B`
2. `legenex/control-ui/tests/test_media_manager_keys.py` fails with 5 errors
   and 1 failure **only under `.venv/bin/python`**, because that venv has no
   Pillow and the test's own `png()` helper imports `PIL`. Under
   `/usr/bin/python3` (which the service uses, Pillow 12.3.0) the same 18 tests
   pass. Either install Pillow into `legenex/control-ui/.venv` or have
   `scripts/qa.sh` run the unit tests with the system Python.
3. `mypy` reports 17 pre-existing errors across `config.py`, `calls.py`,
   `activity.py`, `music_ai.py`, `music_reference.py`, `node2_services.py`,
   `resources.py`, `routes_cal.py`, `routes_plt.py`. `ruff check gx_control_ui
   tests` reports 21 in `calls.py`, `test_calls.py`, `call_agents.py`,
   `call_intake.py`, `music_ai.py`, `music_reference.py`, `resources.py`,
   `footprints.py`. None are IMG's; the IMG files are clean under both.
4. While several agents run Playwright in the same checkout, they share
   `legenex/playground/test-results/` and delete each other's trace files, so a
   spec can die with `browserContext.close: ENOENT … recording2.trace`. It is
   not a product failure: the same specs pass 5/5 with
   `--output=<a private directory>`. Worth giving each workstream its own
   `--output` in `qa.sh` while the build is parallel.

## 13. Limitations

* VisionmasterPro_V3 has never been loaded by ComfyUI on this cluster; its
  memory footprint is unknown and its diffusers→ComfyUI key conversion is
  unproven. First load is acceptance case `src_vm`.
* The near-duplicate signal is a 64-bit difference hash, not SSIM: it is a
  cheap in-process alarm, not the acceptance metric. `image_eval.py` remains
  the judge.
* `img_generations.flow_*` stay NULL for images: `validate()` does not carry
  flow ids on image jobs (Creative Flows links its assets through the flow
  run instead).
* LoRAs beyond the four fixed ones in the templates are not selectable for
  images (that is WAN's library, for video).
* The public gateway (`gx-image` through LiteLLM) passes `image_model` through,
  but the Playground history API is a browser route only.
* `assets.js` and the Library still show provenance from `settings`; the new
  `img_*` tables have no UI of their own yet.
