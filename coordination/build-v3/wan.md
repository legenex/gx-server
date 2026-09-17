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

* **Control Center restart applies every pending migration file.** Done: the
  20:10 restart applied 010, 020, 030, 040, 050 and 080; `schema_migrations`
  now records `020_wan_loras.sql`. No further restart is needed for WAN.
* Router QA (`legenex/media/router/qa.sh`) passes on the shared checkout with
  everyone's router changes (**174 tests, 2026-09-17 20:44**; it was 152 when
  this note was first written).
* `test_policy.py` pinned the router version; it now expects 2.5.0.
* Offline E2E fixture (`control-ui/e2e/fixture_server.py`) now routes
  `/v1/loras*`, `/v1/videos*`, `/v1/workflows` and `/health` to the real router
  code (`wan_router_stub.py`); image routes still go to the old stub.
* QA blockers seen that are not WAN's — superseded by the list under
  "Integration requests for the lead"; the `music.js` finding is now fixed.

## Tests and results

All runs on gx10-01 on 2026-09-17 against checkout `c42ba2a`, evidence under
`/srv/logs/acceptance/build-v3/wan/`. This project uses `unittest`, not pytest.

| Suite | Command | Result | Log |
|---|---|---|---|
| Media router QA (all workstreams' router code) | `legenex/media/router/qa.sh` | **QA PASSED** — 15 workflow templates validated, `Ran 174 tests … OK`, no secrets | `01-router-qa.log` |
| Router Wan LoRA unit tests | `cd legenex/media/router && python3 -m unittest tests.test_wan_loras -v` | **`Ran 40 tests in 1.362s … OK`** | `09-test_wan_loras.log` |
| Control Center Wan video tests | `cd legenex/control-ui && python3 -m unittest discover -s tests -p test_wan_video.py -v` | **`Ran 35 tests in 22.795s … OK`** | `08-test_wan_video.log` |
| Control Center full unit suite | `cd legenex/control-ui && python3 -m unittest discover -s tests` | **`Ran 708 tests in 140.9s … OK`** | — |
| Playground proxy/unit tests (incl. `test_allow_list_video_loras`) | `cd legenex/playground && python3 -m unittest discover -s tests` | **`Ran 11 tests … OK`** | — |
| Offline Playwright LoRA spec (+ axe) | `cd legenex/playground && npx playwright test --project=offline e2e/offline.c-video-loras.spec.js` | **5 passed (25.9 s)** | `10-offline-video-loras-e2e.log` |
| Live Playwright LoRA acceptance | `npx playwright test --project=live e2e/live.wan-loras.spec.js` | run 1 **failed** on a viewer-handle race (see below), run 2 after the fix **1 passed (2.0 m)** | `11-…-log`, `12-…-run2.log` |

**Invocation note.** `python -m unittest tests.test_wan_video` does *not* work
(the test modules import their `support` helper by bare name); the suites must
be run with `-m unittest discover -s tests …`, which is what
`npm run test:unit` does.

**Environment finding, not a WAN defect.** `legenex/control-ui/.venv` has no
`Pillow`, so `.venv/bin/python -m unittest discover -s tests` reports
`Ran 665 … FAILED (failures=1, errors=5)`; every one of them is
`ModuleNotFoundError: No module named 'PIL'` inside `tests/test_media_manager_keys.py`.
The canonical runner (`npm run test:unit` → system `python3`, which has
Pillow 10.2.0) runs **708 tests, OK**. Smallest fix for the lead:
`legenex/control-ui/.venv/bin/pip install Pillow` (or recreate the venv with
`--system-site-packages`).

**Blockers seen in QA that belong to other workstreams** (reported, not
touched — BUILD_V3 rule 10): `legenex/playground` `npm run build` fails with 4
findings, none in WAN files —
`flows/assets/flows-CYmKOQRh.js: innerHTML/insertAdjacentHTML outside dom.setTrustedHTML`,
`js/pages/live.js: unused import 'uid'`, `js/pages/live.js: unused import 'href'`,
and `web/: asset size 1076358 B exceeds budget 614400 B` (the Flows bundle).
The `music.js` finding recorded earlier is gone. `legenex/playground` QA cannot
be green until FLO and LIV clear those.

### Deployment (BUILD_V3 rule 9)

* Deployed router version **before: 2.4.1** → **after: 2.5.0** (`03-health-after-deploy.json`,
  `14-health-final.json`). `qa.sh` was run on the shared checkout first and passed (174 tests).
* `legenex/media/deploy-node2.sh` output: `router ok uploads True workflows 15`,
  `deployed media router at c42ba2a9d92f` (`02-deploy-node2.log`).
* LoRA mounts on `gx-media-router` confirmed read-only:
  `/srv/models/shared/loras → /srv/loras/shared (ro)`,
  `/srv/models/video/loras → /srv/loras/video (ro)`.
* `/srv/models/video/loras/wan22/{paired,high_noise,low_noise,general}` created
  by the deploy (all four exist, empty, owned by `legenex-02`).
* `/health` reports `"loras": {"enabled": true, "files": 4,
  "lora_loader_available": true, "comfy_error": null}`.

### Live router API checks (deployed 2.5.0, over the fabric)

* `POST /v1/loras/rescan` → 4 files, both LightX2V pairs classified
  `compatible` / `wan-14b` / hidden 5120 / 40 blocks, `noise` high|low from
  `filename`, `comfy_visible: true`, `usable: true`, `problems: []`
  (`04-lora-rescan.json`).
* Refusals carry codes (`07-negative-cases.txt`): a high file on the low branch →
  400 `lora_branch_mismatch`; an unknown name → 400 `lora_not_found`;
  `strength: 99` → 400 `lora_invalid_strength`.

## Live acceptance

Real generations on gx10-02 through the deployed GX-Playground Video page and
the deployed 2.5.0 router. Evidence:
`/srv/logs/acceptance/build-v3/wan/live-run-1/` (first attempt) and
`/srv/logs/acceptance/build-v3/wan/live-run-2/` (the green run).
**Four real videos were produced**; none of the evidence files is empty.

| Run | Job | ComfyUI prompt id | User LoRAs | Elapsed | Asset | sha256 (first 12) | Bytes |
|---|---|---|---|---|---|---|---|
| 1 no-lora (cold) | `6db678c2e6c39a75` | `d85c916f-c818-452f-85c7-eab3ef04d51d` | none | 56.2 s | `a_db6bfb9ac6260e8809a7edfa` | `87f02eb52179` | 412 636 |
| 1 LightX2V pair | `482ef3998236f07c` | `201d17a1-834b-481c-9023-8210a04695b9` | high+low @0.3 | 52.2 s | `a_23bd85493b6e0aa25aaa9e01` | `19c65af555eb` | 553 473 |
| 2 no-lora (warm) | `343a85ea25c0eceb` | `32f3617f-ded7-4b72-9953-724d…` | none | 49.6 s | `a_664eac6bb981346a5825d76e` | `edcf9efe8dc7` | 412 636 |
| 2 LightX2V pair | `2cda9e1afc89622f` | `fdff4960-5b21-4625-9851-cd806d52f191` | high+low @0.3 | 52.2 s | `a_7a1dfb0f1063c2dfef1d1b17` | `c4247031981c` | 553 473 |

Every job: `wan22-t2v-a14b-uncensored`, 640×640, 49 frames, 16 fps, seed
20260917, workflow version
`gx-wan-lora/1+wan22-t2v-a14b-uncensored@2b10c3148e0d`, status `ready`,
`error_code: null`.

**1. Discovery.** The rescan walked both roots and found the four installed
files; the two `wan2.2_t2v_lightx2v_4steps_lora_v1.1_{high,low}_noise.safetensors`
are classified `noise: high` / `noise: low` with
`noise_source: "filename"`, `compatibility: compatible`
("Wan 14B layout: hidden size 5120, 40 of 40 blocks"), `key_format: kohya`,
1200 tensors, rank 64, `comfy_visible: true`. The live UI catalogue
(`GET /api/video/loras?refresh=1`) shows the same, plus the header SHA-256
`d65be4de…` in the details panel (`live-run-*/wan-live.json`, key `library-entry`).

**2. Pairing.** Both files carry `pair_key ::wan2-2-t2v-lightx2v-4steps-lora-v1-1`
and the Control Center formed the auto pair
`l_33db6a59347573e1` "wan2 2 t2v lightx2v 4steps lora v1 1",
`pair_state: paired`, `pair_source` auto, `usable: true`, with the second
installed pair (`Wan2.2_LightX2V_*_n54vv`) formed independently. The live
library card shows the **Paired** and **Compatible** badges and lists both
file names (asserted by the spec).

**3. Strengths reach the graph independently.** A preview built directly on the
router with deliberately different values (`05-workflow-preview.json`) returns
`strength_model 0.75` on node `1000` and `0.45` on node `2000`. The accepted
generations used 0.3/0.3 and the history row reports
`chains.high[1] = {node: "1000", …_high_noise.safetensors, strength: 0.3, base: false}`
and `chains.low[1] = {node: "2000", …_low_noise.safetensors, strength: 0.3, base: false}`,
with the built-in LightX2V LoRAs still at 1.0 on nodes 5 and 6.

**4. Branch placement — the two model paths share no node.** Traced on the
real generated graph (`06-branch-trace.txt`):

```
KSampler 12 (add_noise enable, steps 0-2) <- 7 ModelSamplingSD3 <- 1000 LoRA(user high 0.75) <- 5 LoRA(base high 1.0) <- 3 UNET wan2.2_t2v_high_noise_14B_fp8_scaled
KSampler 13 (add_noise disable, steps 2-4) <- 8 ModelSamplingSD3 <- 2000 LoRA(user low 0.45) <- 6 LoRA(base low 1.0) <- 4 UNET wan2.2_t2v_low_noise_14B_fp8_scaled
shared nodes between the two model paths: NONE
```

The UI "Advanced view" table shown before the run says the same:
High noise → built-in 1.00, then the high file 0.30 on node 1000; Low noise →
built-in 1.00, then the low file 0.30 on node 2000.

**5. Generated workflow JSON (scrubbed).** `live-run-*/workflow-*.json` and
`05-workflow-preview.json`. The spec asserts the exported text contains no
`/srv/`, no `192.168.`, no `gx10-0`, no `Bearer` and no `sk-…`; that assertion
passed.

**6. ComfyUI execution.** Four prompt ids (table above), all returned by
ComfyUI, all jobs `ready`, `error_code: null`, `error_message: null`. Router
`/health` never reported a refusal or eviction during the runs.

**7. Playable output videos.** `ffprobe` (5.1.9) on the run-2 files
(`live-run-2/ffprobe.txt`; run-1 has its own):

```
no-lora        h264 640x640 yuv420p r_frame_rate=16/1 nb_read_frames=49 duration=3.0625 size=412636 bit_rate=1077730
lightx2v-pair  h264 640x640 yuv420p r_frame_rate=16/1 nb_read_frames=49 duration=3.0625 size=553473 bit_rate=1445571
```

49 frames actually decoded, 3.0625 s, non-trivial size. The library's own
analysis agrees (`frame_count 49`, `distinct_frames 49` — no frozen frames).
Playback was also proved in the browser: the stage viewer reached
`readyState 4` and `currentTime` advanced 1.49 s in a 1.5 s window at
640×640 with `video.error === null`. With the same seed the LoRA run differs
from the plain run (`sha_differs: true`, and 553 473 vs 412 636 bytes), so the
LoRA measurably changed the output.

**8. History row and metadata.** `GET /api/video/generations/<job>` returns
`workflow_version`, `comfy_prompt_id`, `high_model`
(`wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors`), `low_model`, the full
`chains`, and `loras[0] = {kind: "pair", apply: "pair", strength_high: 0.3,
strength_low: 0.3, compatibility: "compatible", high_file: …, low_file: …}`.
The asset carries the same under `settings.wan`. In the UI the history row
appears with its title, its details dialog plays the video and its Advanced
view contains the ComfyUI prompt id (asserted).

**9. Memory (1 Hz on gx10-02 throughout, 1 087 samples, 20:49:05 → 21:07:22).**
`13-node2-memavailable-1hz.csv`, summary `13-node2-memory-summary.txt`:

| | |
|---|---|
| baseline MemAvailable | **112.56 GiB** |
| minimum MemAvailable | **39.26 GiB** (20:53:01, during the cold load) |
| peak growth | **73.30 GiB** |
| headroom over the 30 GiB reserve at the minimum | **9.26 GiB** |
| final MemAvailable | **111.55 GiB** |
| swap used start / max / end | 3.12 / 3.12 / 3.12 GiB of 64 GiB (no swap growth) |

The cold Wan 2.2 640×640×49 t2v job therefore still costs ≈ 73 GiB with a
LoRA pair applied — within the 72 GiB figure the lead measured, and the
reserve was never approached. Warm runs added nothing measurable.

**10. Cleanup.** Freed through the sanctioned path
(`docker exec gx-media-router python -m gx_media_router.free_node` →
`{"freed": true, "models": [4 Wan files]}`). Final `/health`: version 2.5.0,
`busy: false`, `video_queue_depth: 0`, `resident_models: []`,
`pending_gib: 0.0`, `available_gib: 111.5`, ComfyUI VRAM free 79.2 GiB,
`gx-music` engine `unloaded` (`14-health-final.json`). **gx10-02 is idle.**

### The one failure, and what it was

Live run 1 failed at the very end of the second generation:
`expect(played.advanced).toBeGreaterThan(0.2)` received `0`. Both generations
had already completed successfully and both assets were saved — the failure
was in the browser playback measurement, not in generation.

Root cause, established by measurement rather than guessed: `workspace.js`
`renderViewer()` does `clear(viewer)` and re-appends, so the stage rebuilds its
`<video>` element on every re-render. The handle the spec grabbed right after
the job completed was detached by the next render; a detached element resolves
`play()` but its `currentTime` stays at 0. A direct probe against the deployed
Playground, re-locating the element, played the very same LoRA asset with
`readyState 4`, `advanced 2.49 s`, `error: null`. `playVideo()` in
`e2e/live.wan-loras.spec.js` now re-locates the element and measures again (up
to three attempts, recording each retry), and also asserts `video.error` is
null. Run 2 passed on the first attempt with no retries recorded.

This is a test-robustness fix, but it points at a small product wart in a file
I do not own — see the integration requests.

## Integration requests for the lead

Copy-pasteable. None of these touch a file WAN owns.

1. **`legenex/control-ui/.venv` is missing Pillow**, so
   `.venv/bin/python -m unittest discover -s tests` reports 6 errors in
   `tests/test_media_manager_keys.py` (`No module named 'PIL'`). The canonical
   runner (system `python3`, what `npm run test:unit` uses) is green at
   **708 tests OK**. Smallest fix:

   ```bash
   legenex/control-ui/.venv/bin/pip install Pillow
   ```

2. **`legenex/playground` `npm run build` is red for other workstreams** (WAN
   files are clean). Please route to FLO and LIV:

   ```
   flows/assets/flows-CYmKOQRh.js: innerHTML/insertAdjacentHTML outside dom.setTrustedHTML
   js/pages/live.js: unused import 'uid'
   js/pages/live.js: unused import 'href'
   web/: asset size 1076358 B exceeds budget 614400 B   (the Flows bundle)
   ```

3. **Shared file, small product wart (`legenex/playground/web/js/workspace.js`).**
   `renderViewer()` clears and re-appends the viewer, which destroys and
   recreates the `<video>` element, so any re-render while the user is
   watching restarts playback from 0. Suggested minimal change, for whoever
   owns `workspace.js` (Images and Video both use it):

   ```js
   // in update()/select(): skip renderViewer() when the selected asset id and
   // its mutable fields are unchanged, or reuse the existing <video> element
   // when only the surrounding chrome changed.
   ```

   WAN did not touch it. The live spec now re-locates the element instead.

4. **No Playground or Control Center restart is needed for WAN.**
   `legenex/playground/scripts/deploy.sh --verify` reports
   "32 files served match the checkout; 0 stale" and
   `/api/ready` is `{"ready": true, "problems": []}` with migration
   `020_wan_loras.sql` applied. WAN changed no Playground or Control Center
   Python in this session — the only edit was
   `legenex/playground/e2e/live.wan-loras.spec.js` (test code).

5. **For `CURRENT_STATE.md` / `TEST_RESULTS.md` / `CHANGELOG.md`:** the media
   router on gx10-02 is now **2.5.0** (was 2.4.1), deployed from `c42ba2a`, and
   the Wan 2.2 LoRA path has a real live acceptance (four generations,
   evidence in `/srv/logs/acceptance/build-v3/wan/`). gx10-02 was returned to
   idle afterwards.

## Limitations

* LoRAs apply to text-to-video only (i2v/v2v templates declare no chains).
* The public gateway `gx-video` path (LiteLLM) does not pass `loras`.
* At most 8 LoRAs per branch; no per-LoRA memory estimate (the video
  footprint of 72 GiB was measured without user LoRAs; LoRA patches add
  roughly their file size while applied).
* Library thumbnails/previews for LoRAs are not shown: no trustworthy preview
  source exists for local files (header metadata rarely carries one).
* Compatibility is a structural check, not a quality guarantee.
