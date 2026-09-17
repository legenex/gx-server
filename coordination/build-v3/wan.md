# WAN — Wan 2.2 LoRA support (Build V3, workstream WAN)

Owner: video specialist. Status: see "Live acceptance" below.

## What was built

| Layer | Files |
|---|---|
| Media router 2.5.0 (gx10-02) | `legenex/media/router/gx_media_router/lora_catalog.py` (discovery + bounded safetensors header analysis + noise classification), `lora_chain.py` (managed LoRA insertion, validation, graph checks, version string), additions in `service.py` (rescan, preview, per-job graph, cancel), `server.py` (routes), `comfy.py` (`/object_info`, error codes), `config.py` (`GX_MEDIA_LORA_ROOTS`), `jobs.py` (job fields), `errors.py` (`ConflictError`), `workflows.py` (`_gx.lora_chains`, `build(params, loras)`); template `workflows/wan22-t2v-a14b-uncensored.api.json` (`lora_chains` + shift/cfg/steps/boundary/sampler/scheduler bindings); `docker-compose.media.yml` (read-only LoRA mounts); `deploy-node2.sh` (creates `video/loras/wan22/{paired,high_noise,low_noise,general}`) |
| Control Center (gx10-01) | `gx_control_ui/wan_video.py` (library model, pairing, settings, presets, generation, history, error text), `routes_wan.py`, `migrations/020_wan_loras.sql`, hooks in `media_jobs.py` (`submit(wan=…)`, observers, JSON body for LoRA jobs, router cancel, `error_code`/`error_hint`), `media_library.py` (`has_thumbnail`), one App block + one import in `server.py` |
| Playground | `web/js/wan.js` (LoRA stack, library drawer, presets, advanced view, history, errors), `web/js/pages/video.js` (integrated page), small edits in `jobs.js` (`submitVideo`, retry, `error_hint`), `assets.js` (video recipe, LoRA rows), `ui.js` (`has_thumbnail`), CSS section in `app.css`, ALLOW entries in `gx_playground/server.py` |
| Tests | `legenex/media/router/tests/test_wan_loras.py` (40), `legenex/control-ui/tests/test_wan_video.py` (35), `legenex/control-ui/e2e/wan_router_stub.py` (the REAL router code with a fake ComfyUI, used by unit tests and the offline fixture), `legenex/playground/tests/test_proxy.py::test_allow_list_video_loras`, `legenex/playground/e2e/offline.c-video-loras.spec.js` (5, axe), `offline.c-video.spec.js` (updated), live spec `e2e/live.wan-loras.spec.js` |
| Docs | `legenex/control-ui/docs/21-video-loras.md` (new; docs are indexed by file name), `11-media.md`, `15-playground.md`, `Manual.md` §7 "Video: Wan 2.2 LoRAs…", `legenex/playground/API.md`, `legenex/media/README.md` |

## Design decisions (for DECISIONS.md)

* **Where things live.** The files are on gx10-02, so the router owns
  discovery and header checks (read-only mounts of `shared/loras` and
  `video/loras`, in ComfyUI's search order). The router also owns graph
  building: callers send LoRA *names* from its catalogue, never graph
  fragments. User decisions (pairs, settings, presets) and history live in the
  application DB on gx10-01.
* **Names.** A LoRA's name is its path below the root, exactly as ComfyUI's
  `LoraLoaderModelOnly.lora_name` lists it. The rescan asks ComfyUI
  (`/object_info/LoraLoaderModelOnly`) and marks files it does not list.
  ComfyUI refreshes its own list when a directory mtime changes; no restart.
  A name present in two roots is reported `shadowed` for the root ComfyUI
  does not pick.
* **Header checks.** 8-byte length + JSON, max 64 MiB, tensor offsets must fit
  the file; tensor data is never read. Compatible = Wan transformer keys
  (`blocks.N.{self_attn,cross_attn,ffn}`), hidden size 5120, ≤ 40 blocks.
  Wan 5B (3072), 1.3B (1536), Qwen-Image, FLUX, SD/SDXL and invalid files are
  incompatible. Wan 14B I2V LoRAs (`k_img`/`img_emb`) and unfamiliar layouts are
  unknown; unknown needs an explicit per-entry opt-in.
* **High/low.** Evidence from folder (`high_noise/`, `low_noise/`,
  `general/`), file name tokens (`high`, `hn`, `highnoise`, `high_noise`,
  camelCase split, any separator) and metadata (`ss_output_name`,
  `modelspec.title`, …). Conflicts → unknown. Auto pairs need exactly one high
  and one low file with the same folder (noise folders ignored) and the same
  stem once the marker is removed. Manual pairs and "split" markers are stored
  in the DB.
* **Branch safety.** High files only on the high branch, low only on low
  (router refuses otherwise). One file on both branches needs `shared: true` on
  both sides (router) and is offered only for general/unknown files (UI and
  Control Center). The generator inserts `LoraLoaderModelOnly` nodes
  `1000+i` (high, after node 5) and `2000+i` (low, after node 6) and then
  checks every reference and that the two model paths share no node.
* **Defaults** (0.8/0.8 per pair, 0.5 multi-LoRA hint, 0-1.5 range) are
  Control Center configuration (`wan_video.DEFAULTS`), not generator logic.
* **Workflow version** `gx-wan-lora/1+wan22-t2v-a14b-uncensored@<sha12 of the
  template graph>`; recorded per job and per history row.
* **History** records every video job (t2v with or without LoRAs, i2v, v2v)
  through a MediaJobs observer. The exported workflow is scrubbed (absolute
  paths → basename, IPv4 → `[address]`, node names → `[node]`, `redact()`).
* **Cancel** now reaches the router for a job that is queued/waiting there
  (`POST /v1/videos/{id}/cancel`); a job already in ComfyUI is not interrupted.

## Contract for Creative Flows (FLO)

* A Video node stores a preset id (`wp_<16 hex>`). Example ids are fixed:
  `wp_5c1e7a2b90d34f01` Cinematic Realism, `wp_7f3a9c4d12e84b02` High Detail,
  `wp_2b8d6e1f47a54c03` Character Consistency, `wp_9e4c0b7a35f14d04` Motion
  Style, `wp_1a6f8e2c59b74e05` Custom 1.
* Resolve + submit, server side (same process):

  ```python
  resolved = app.wan.resolve_preset(preset_id, {"prompt": text, "seed": seed,
                                               "flow_id": fid, "flow_run_id": rid, "flow_node_id": nid})
  if not resolved["valid"]: fail with resolved["warnings"]
  job = app.wan.generate(resolved["body"], user=user)   # a media job dict; poll app.media.get(job["id"])
  ```

  Browser/HTTP equivalent: `POST /api/video/presets/{id}/resolve
  {"overrides": {...}}` then `POST /api/video/generate` with `body`.
* Overrides: `prompt`, `negative_prompt`, `seed`, `size`, `seconds`, `fps`,
  `title`, `loras`, `advanced`, `flow_id`, `flow_run_id`, `flow_node_id`
  (ids `^[A-Za-z0-9_-]{1,64}$`). The prompt is the override (or preset prompt)
  followed by the preset's style suffix.
* The finished asset carries `settings.wan = {generation_id, loras, advanced,
  preset_id, workflow_version, comfy_prompt_id, chains, flow}`; the history row
  (`GET /api/video/generations/{job id}`) carries `flow_id`,
  `flow_run_id`, `flow_node_id`. **NewAsset flow_* columns are not set by the
  media job import** (MediaJobs builds the NewAsset); FLO can link assets via
  the history row or `settings.wan.flow`.
* History links to `#/flows?asset=<asset id>` ("Reuse in Creative Flows").

## Integration notes for the lead

* **Control Center restart applies every pending migration file** (live DB had
  none applied at 17:10: 010, 020, 030, 040, 050, 080 are pending). A dry run
  of all six on a copy of the live `library.db` succeeded (26 assets kept,
  5 presets seeded).
* Router QA (`legenex/media/router/qa.sh`) passes on the shared checkout with
  everyone's router changes (152 tests at the time of writing).
* `test_policy.py` pinned the router version; it now expects 2.5.0.
* Offline E2E fixture (`control-ui/e2e/fixture_server.py`) now routes
  `/v1/loras*`, `/v1/videos*`, `/v1/workflows` and `/health` to the real router
  code (`wan_router_stub.py`); image routes still go to the old stub.
* QA blockers seen that are not WAN's: `playground/web/js/pages/music.js`
  unused imports (build check), ruff findings in `flows/catalog.py`,
  `footprints.py`, `music_ai.py`, `routes_flo.py`, `voice.py`, and a
  reference-test lint error (MUS).

## Tests and results

(filled in below as runs complete)

## Live acceptance

(filled in below)

## Limitations

* LoRAs apply to text-to-video only (i2v/v2v templates declare no chains).
* The public gateway `gx-video` path (LiteLLM) does not pass `loras`.
* At most 8 LoRAs per branch; no per-LoRA memory estimate (the video
  footprint of 72 GiB was measured without user LoRAs; LoRA patches add
  roughly their file size while applied).
* Library thumbnails/previews for LoRAs are not shown: no trustworthy preview
  source exists for local files (header metadata rarely carries one).
* Compatibility is a structural check, not a quality guarantee.
