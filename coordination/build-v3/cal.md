# CAL: gx-call (NemotronLabs VoiceChat 11B) and Call Agents workstream log

Owner: CAL specialist. Contract: `coordination/BUILD_V3.md`, PLT interfaces in
`coordination/build-v3/plt.md`. Newest log entries last.

## Status (living)

| Item | State |
|---|---|
| Model | `nvidia/NVIDIA-NemotronLabs-VoiceChat-11B` @ `a4c40ca5b4fe77db13e9840ca4a2b91becf030c8` (openmdw-1.1, public). **Downloaded and verified** on gx10-02: `/srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B`, 41.35 GiB, 17 files, 6 sha256-checked (`/srv/logs/gx-call/hf-verify.log`) |
| Runtime | image `gx-call-engine:voicechat-097dfe9-t214`: build **completed 2026-09-17 21:09** (1e61699df9e7, 18.3 GB), import gate **PASSED**. Runtime **FAILED 2026-09-17 21:30** - dependent model `nvidia/NVIDIA-Nemotron-Nano-9B-v2` not in cache, network unreachable from container. Engine cannot load. |
| Service | `gx-call.service` (node 2, `192.168.100.11:18840` + `127.0.0.1:18840`), key `secrets/gx-call/api-key`. Unit file written; **not installed or started** |
| Control Center | complete: `calls.py`, `call_agents.py`, `call_intake.py`, `routes_cal.py`, 15/15 tests green |
| Playground page | `web/js/pages/call.js` complete and **integrated by the lead**; live on the deployed Playground. 5/5 offline Playwright specs green **in the repo, unchanged** (5 axe WCAG 2.2 AA checks) |
| Footprint | **not measured** — no FOOTPRINT line can be published yet (PLT section 7) |

## Interfaces other workstreams need (PLT, LIV, LEAD)

* **Node-2 service:** `gx-call` on `192.168.100.11:18840`, bearer key
  `/srv/projects/gx-cluster/secrets/gx-call/api-key` (0600 on both nodes).
* **Engine container:** `gx-call-engine`. Residency-ledger entry name: `gx-call-engine`
  (same as the container, as PLT section 5.1 asks). Workload class `medium`.
* **Open health:** `GET /health` follows PLT section 5 (`state`, `busy`, `pinned`,
  `active_sessions`, `memory.{pending_gib,resident_gib,estimate_gib}`).
* **Unload:** `POST /v1/call/unload {"if_idle": true}` (409 while a call is live or a
  pin is honoured); `{"if_idle": false, "reason": "gxmax"}` ends live calls and unloads.
* **Tunnel upstream path:** `/v1/call/sessions/<call_id>/ws?join=<node-2 join token>`.
  The join token is created by gx-call when the Control Center creates the session and is
  never sent to a client (it lives only in the registered upstream path).

## Runtime investigation: is VoiceChat 11B runnable on GB10?

**Verdict: nothing found that rules it out, and nothing yet proves it. The
engine has never run, because its image was never finished.** No substitute
model was considered.

Evidence, all collected read-only (no GPU work was started in this wave):

1. **Checkpoint is present and correct.**
   `ssh legenex-02@gx10-02 ls -la /srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B`
   → `model.safetensors` 44,382,749,892 B plus 16 smaller files;
   `.gx-manifest.json` records repository + revision
   `a4c40ca5b4fe77db13e9840ca4a2b91becf030c8`, `hash_checked: true`, `gated: false`;
   `/srv/logs/gx-call/hf-verify.log`:
   `VERIFIED … 17 files, 41.35 GiB, 6 sha256-checked in 49s`.
2. **The image does not exist.** `docker images` on gx10-02 lists
   `gx-live-engine:minicpmo45-503e754-t214` and `gx-voice-engine:qwen3tts-022e286-t214`
   but **no `gx-call-engine`**. `systemctl --user list-units "gx-*"` has no
   `gx-call.service`. So there is no runtime evidence of a model load.
3. **The one build attempt was cancelled, not failed.**
   `/srv/logs/gx-call/build-dev.log` (23.9 kB, last written 17:15) ends with
   `#12 CANCELED` / `ERROR: failed to build: failed to solve: Canceled: context canceled`
   during `[builder 2/3]` (the torch download for the mamba-ssm /
   causal-conv1d wheel build). The only error line in the whole log is that
   cancellation.
4. **The runtime Python stack does resolve on aarch64.** In the same log the
   runtime stage got all the way through `#16 DONE 37.4s`, installing
   `torch 2.14.0+cu130` + `torchaudio`/`torchvision`, `transformers==4.56.0`,
   `tokenizers==0.22.0`, `lhotse`, `websockets==15.0.1` and
   `nemo-toolkit==2.4.0rc0` (built from NeMo Speech @`097dfe9e…`, the
   `nemotron-labs-voicechat` branch). That is the hardest dependency risk and
   it is cleared.
5. **flash-attn is not involved.** `grep -rn "flash|attn_implementation|sdpa|eager"`
   over `legenex/call/engine/` returns only the *builder* image name
   (`jstarkg/vllm-gb10-flashnext`, reused for its CUDA 13 toolkit).
   `requirements-gx.txt` has no flash-attn: the engine uses NeMo's native
   PyTorch `StreamingS2SPipeline`, not a fused-attention path.
6. **sm_121 vs the `TORCH_CUDA_ARCH_LIST="12.0"` in the Dockerfile** is the
   established cluster practice, not a guess: `legenex/media/README.md` records
   "`torch 2.14.0+cu130` ships `sm_80/90/100/110/120` cubins and no PTX →
   sm_120 cubins execute correctly on sm_121 (same Blackwell major)", and
   gx-comfyui and gx-music-engine are built that way and run.
7. **The vLLM/NIM path is correctly excluded**, and the reason is recorded in
   `engine/Dockerfile`: `nvcr.io/nim/nvidia/nemotron-labs-voicechat` is
   published for linux/amd64 only, and its vLLM engine needs
   `vllm.config.model.CustomInputSpec`, which is in no public vLLM release.
8. **The GB10-specific work is three explicit, switchable patches**
   (`engine/gx_voicechat_patches.py`): `GX_VC_MMAP_LOAD` (zero-copy
   copy-on-write safetensors views, so the 44 GB fp32 checkpoint is page cache
   instead of two anonymous copies), `GX_VC_EARLY_CAST` (cast the LLM to bf16
   on the CPU before the first `.to("cuda")`), and `GX_VC_HYBRID_CACHE`
   (a correct Nemotron-H hybrid attention+Mamba cache, because upstream
   disables the LLM cache for Nemotron and re-runs the whole conversation
   through the 9B backbone every 80 ms, which cannot be real time). The engine
   ships `--selftest-cache`, which compares cached and uncached logits, and
   `--bench WAV`; **both still have to be run**.

### What remains unproven (and therefore what acceptance must show)

* that the image builds to the end (only the mamba-ssm / causal-conv1d wheel
  stage is untested; it needs ~30-60 min and ~10 GB of disk — 219 GB free);
* that the model loads inside the 30 GiB reserve. `GX_CALL_ENGINE_ESTIMATE_GIB`
  is **48.0** and `GX_CALL_ENGINE_MEMORY_CAP` is `72g`; those are estimates;
* that decoding keeps up with 80 ms frames (RTF < 1) with the hybrid cache on;
* that `--selftest-cache` agrees, i.e. the cache patch does not change outputs.

**No claim of inference, audio or latency is made anywhere in this workstream.**

## Test results (this wave, verified)

Run as `cd legenex/control-ui && .venv/bin/python -m unittest discover -s tests -p 'test_calls.py'`.
(`python -m unittest tests.test_calls` does **not** work: `tests/support.py` is
imported as a top-level module, so the start directory has to be on `sys.path`.)

| Run | Result |
|---|---|
| Before any edit | **Ran 15 tests in 55.556s — FAILED (failures=3, errors=1)** |
| After the fixes | **Ran 15 tests in 11.9s — OK** |

All four previously reported failures **did reproduce**. None was stale.

| Reported failure | Reproduced? | Root cause | Fix |
|---|---|---|---|
| tool_results not persisted (`IndexError: list index out of range` on `stub.tool_results[0]`) | yes | `legenex/call/tests/engine_stub.py` waited for the tool result **on the thread that reads the socket**, so the result could not arrive; `wait(5)` always timed out and the frame was only consumed later. A real engine never stops reading. | The stub now drains the socket in its own reader thread and feeds audio to the "model" loop through a queue; the test waits (bounded) for the engine to have the result, the same pattern `legenex/call/tests/test_gx_call.py:484` already used. Side effect: the suite went from 55 s to 12 s. |
| `call.agent.save` audit entry missing | yes | **Real bug.** `AgentStore.__init__` did `self.audit = audit or (lambda **_: None)`. The test's audit sink is a `list` subclass, and an **empty list is falsy**, so every audit call went to the no-op — for create, save and status alike. | `self.audit = (lambda **_: None) if audit is None else audit` (and the same for `CallManager.metric`). |
| `session.ended` never arrives on the tunnel | yes | The test is named "end_call tool", sets `tool_args={"outcome": "callback_requested"}` (an `end_call` argument) and gives the agent `tool_permissions=["end_call"]`, but the stub only ever called `update_intake_fields`, so nothing ended the call. | The stub now calls whichever tool the agent actually has (`update_intake_fields` when present, otherwise the first one), so the **real `end_call` path runs end to end**: tool → Control Center → scheduled end → gx-call → `session.ended`. |
| the recording route answered 409 because the session was not ended | yes | **Real bug.** `CallManager.end_session` posted the end to gx-call and then waited up to 10 s for the *event poller* to mark the row ended. The poller is `start_threads=not cfg.offline`, so in offline mode (and briefly in production) it never ran and the row stayed live. | `end_session` now finalises from gx-call's own authoritative answer (it returns the ended session view), and `_finalize` became atomically single-shot (it keys off the `UPDATE … WHERE state != 'ended'` row count), so the poller's later `session.ended` is a harmless no-op. |

One further assertion had to be corrected once the test got far enough to reach
it: `view["tools"][0]["ok"]` was expected to be `0` on the same tool call whose
`tool.result` event the test had already asserted was `ok: true`. `ok` mirrors
what the model was told; an intake that is not complete yet is **not** a tool
failure (the missing fields come back in the spoken output). The assertion is
now `1`, which is consistent with the `assertTrue(result["ok"])` above it.

### Other suites (verified totals)

| Suite | Result |
|---|---|
| `legenex/call/qa.sh` (supervisor + engine protocol, stub engine) | **Ran 28 tests — OK**, all five gate steps pass |
| `legenex/control-ui`, full `unittest discover -s tests` | First run of this wave (20:45): **Ran 654 tests — FAILED (failures=1, errors=5)**, all six in `tests/test_media_manager_keys.py` and none CAL (5 × `ModuleNotFoundError: No module named 'PIL'`, plus one image job needing a reachable router). Final run (21:45), after Pillow appeared in `control-ui/.venv` (12.3.0) and other workstreams landed more tests: **Ran 708 tests — OK**. |
| `ruff check gx_control_ui tests e2e` | CAL files were contributing 15 findings; **all 15 fixed**. 6 remain, none CAL: `footprints.py` E501, `music_ai.py` SIM102 ×2, `resources.py` E501, `test_music_reference.py` F401 + SIM117. |
| `mypy gx_control_ui` | CAL files were contributing 6 errors; **all 6 fixed**. 11 remain in 7 non-CAL files (`node2_services.py` ×3, `routes_plt.py` ×2, `music_reference.py` ×2, `activity.py`, `config.py` (`voice_base` defined twice), `music_ai.py`, `resources.py`). |
| `legenex/playground`, `node scripts/build-check.mjs` | **OK: 36 modules, 39 files, 580.4 KiB (budget 600 KiB)** — see the budget warning below |
| `legenex/playground` offline Playwright, `offline.h-call.spec.js` | **5 passed** (see below) |

`npm run qa` in `control-ui` was **not** run end to end: its step 7 starts a
Playwright browser suite on fixed ports, and three other workstreams currently
have e2e servers running on 18189/18190, 18489 and 18589. Steps 1, 2, 3, 4 and
6 were run individually and are reported above.

## Playground: the Call Agents page

`legenex/playground/web/js/pages/call.js` (804 lines, 38.9 KiB), vanilla ES
modules, built only from the existing components in `web/js/{ui,dom,api,jobs,
audio,nav,icons}.js` and the existing CSS classes (no change to `web/css/app.css`).

Three sections:

* **Agents** — real list from `/api/call/agents`; create from a template;
  a full editor for the name/branding, all eleven behaviour texts, the tools
  (server-enforced maximum of five), the required and optional intake fields,
  the transfer destination, recording + its spoken notice, retention and the
  maximum call length; **Check and preview** posts to `/api/call/preview` and
  shows the exact compiled prompt and tool list, or the server's refusal in
  place; save creates a new version with `base_version` (so a concurrent edit
  is a 409, never a silent overwrite); enable/disable, duplicate, archive and
  the version history with per-version call counts. Webhooks, business hours
  and the intake schema are shown read-only and are **round-tripped unchanged**
  by the editor, so saving from the form can never destroy them.
* **Call** — live call in the browser: `getUserMedia` → `gx-capture` worklet →
  `WebSocket /rt/call/<sid>` → gx-call, and agent audio → `gx-playback`
  worklet. Live transcript (`aria-live="polite"` log, partial deltas shown
  greyed), the authoritative intake with its completion line, microphone level,
  an "agent speaking" indicator, mute, request transfer and end call. Barge-in
  flushes playback on `interruption.started {flush:true}`. `secureContextInfo()`
  is checked before the microphone and `secureContextCallout()` is rendered
  when the context is insecure, with Start disabled.
* **History** — every call of the signed-in user with state, disposition,
  recording and purge badges; a detail drawer with the result summary,
  structured intake, transcript, the tools that ran with their latencies, the
  timing metrics, the recording (played through `audioPlayer` from the Library
  asset) and "Delete transcript and recording".

**LIV/PLT note:** the page consumes `web/js/realtime.js` (`openRealtime`,
`secureContextInfo`, `secureContextCallout`) and the two worklets
`web/js/realtime/{capture,playback}-worklet.js` **directly**, at their current
message contracts (`{type:'frame',pcm,samples,peak}` / `{type:'mute',muted}`
and `{type:'push',pcm,response}` / `{type:'flush'}` / `{type:'state',playing,
bufferedMs}`). Nothing was duplicated and nothing under `web/js/realtime/` was
edited. **LIV: if you wrap these in a higher-level module, say so here and I
will switch call.js over.**

### Offline browser tests

`legenex/playground/e2e/offline.h-call.spec.js`, run against the real
Playground proxy + the real Control Center fixture:

```
✓ empty state: no agents, and the Call tab says so
✓ agents: template, editor, compiled prompt, new version, enable and duplicate
✓ call tab: the engine banner and the shared secure-context callout
✓ call tab: a start that gx-call refuses is reported, and lands in the history
✓ the section tabs are fully keyboard operable
5 passed (10.0s)
```

Five axe WCAG 2.2 AA checks (empty list, editor, history, Call tab without a
microphone, call detail drawer) are inside those tests and pass. The
secure-context branch is covered by removing `navigator.mediaDevices` in an
init script, because `127.0.0.1` is itself a secure context.

**How they were run, and why that matters:** the four integration lines below
are in files I do not own, so the run used a scratch overlay
(`$SCRATCH/ov/legenex/playground`) that is a copy of `gx_playground/` and
`web/` with exactly those four lines applied, plus `GX_RT_CALL_TARGET=127.0.0.1:1`
so the fixture stays hermetic, on ports 18789/18790 to avoid the other
workstreams' running e2e servers. **The spec cannot pass in the repo until the
lead applies the same lines.**

Running `offline.a-shell` in that overlay also shows `#page-music h1` never
appearing — `web/js/pages/music.js` is currently modified by the MUS
workstream. Reported, not touched.

## Integration requests for the lead — DONE (lead, 2026-09-17 ~21:20)

All five landed and were verified on the deployed Playground
(`scripts/deploy.sh`: "43 files served match the checkout; 0 stale, 0 not served";
the lead's `e2e/live.navigation.spec.js` passes for Call Agents with no axe
WCAG 2.2 AA violations). `offline.h-call.spec.js` now passes **in the repo,
unchanged**: 5 passed (10.4 s) — the scratch overlay is no longer needed and
has been abandoned. Kept below for the record.

Four one-line additions plus one ALLOW block.

**1. `legenex/playground/web/js/routes.js`** — after the `voice` line:

```js
  call: () => import('./pages/call.js'),
```

**2. `legenex/playground/web/index.html`** — inside `<ul class="rail-list" id="nav-realtime">`:

```html
<li><a class="rail-link" href="#/call" data-page="call"><span class="rail-ic" data-icon="phone"></span><span class="rail-label">Call Agents</span></a></li>
```

**3. `legenex/playground/gx_playground/server.py`, `SPA_ROUTE`** — add `call`:

```python
SPA_ROUTE = re.compile(r"/(dashboard|images|video|music|voice|call|library|history|models|logs|settings)"
                       r"(/[a-z0-9_\-]{0,64}){0,2}")
```

**4. `legenex/playground/scripts/build-check.mjs`, `GX_BUILD_PAGES`** — add `call`:

```js
process.env.GX_BUILD_PAGES = 'dashboard,images,video,music,voice,call,library,history,models,logs,settings';
```

**5. `legenex/playground/gx_playground/server.py`, `ALLOW`** — this exact block
(`allowed()` uses `fullmatch` on the path only, so query strings are fine):

```python
        # Build V3 CAL: Call Agents (session) and the public gx-call API (gateway key)
        ("GET", r"/api/call/(catalog|model|agents|sessions|compare)"),
        ("POST", r"/api/call/(agents|preview|sessions)"),
        ("GET,POST", r"/api/call/integrations/secrets"),
        ("POST", r"/api/call/integrations/secrets/[a-z][a-z0-9_\-]{1,40}/delete"),
        ("GET,POST", r"/api/call/agents/agt_[0-9a-f]{24}(/(status|clone|versions))?"),
        ("GET,POST", r"/api/call/sessions/call_[0-9a-f]{32}(/(events|end|transfer|state|delete-content))?"),
        ("GET,POST", r"/v1/call/(agents|sessions)"),
        ("GET", r"/v1/call/(model|agents/agt_[0-9a-f]{24})"),
        ("GET,POST", r"/v1/call/sessions/call_[0-9a-f]{32}"
                     r"(/(ticket|state|transcript|tools|events|transfer|end|result))?"),
```

The page itself only uses the `/api/call/*` paths plus `/api/media/assets/<id>`
(already allowed) for the recording. The `/v1/call/*` lines mirror the
`/v1/voice` and `/v1/music` precedent so IntakePilot can reach the public API
through the Playground origin.

**Also for the lead — `web/` asset budget.** With `call.js` in, build-check
reports **580.4 KiB of the 600 KiB budget** (the Creative Flows Vite bundle is
already excluded by `GX_BUILD_EXCLUDE=flows`). LIV's `live.js` still has to fit
in the remaining ~20 KiB. Either the budget has to be raised or the pages have
to share more code; I can trim `call.js` if you would rather keep 600 KiB.

## What live acceptance needs (scheduled by the lead, after the measurement wave)

1. Build the image on gx10-02 (no GPU, but heavy CPU/disk, so not during the
   node-2 memory measurement):
   `docker build -t gx-call-engine:voicechat-097dfe9-t214 legenex/call/engine`
   and keep the log under `/srv/logs/acceptance/build-v3/cal/`.
2. `--selftest-cache` inside the image (GPU) — the cached and uncached backbone
   logits must agree, otherwise `GX_VC_HYBRID_CACHE` has to be switched off and
   the real-time claim re-examined.
3. First cold load **through `gx_guard_run`** (class `medium`, estimate 48 GiB),
   with MemAvailable sampled at 1 Hz, to produce the PLT section 7 FOOTPRINT
   line. gx-reason (~32 GiB) and any media job must be accounted for first.
4. `--bench` on a test WAV (no bundled WAVs exist — one needs to be created
    or borrowed from `legenex/voice/qa/test_files/`); for first-audio and
    per-frame timing.
5. One real browser call over **HTTPS** (`https://100.105.214.61:8443`, so the
   microphone is allowed) against the IntakePilot template: greeting heard,
   two answers saved into the intake through `update_intake_fields`, barge-in,
   a warm transfer, then `end_call`; then the same call recorded, and the mixed
   asset played back from the Library.
6. The public API path with a gateway virtual key that allows `gx-call`
   (create session → ticket → WS → result).

## Blockers

* ~~**B-CAL-1**: the integration lines~~ — **CLEARED** by the lead 2026-09-17 ~21:20.
  The page is live and `offline.h-call.spec.js` passes in the repo unchanged.
* ~~**B-CAL-2**: the engine image build~~ — **CLEARED 2026-09-17 21:09**: image
  `1e61699df9e7` (18.3 GB) on gx10-02, import gate **PASSED**.
* **B-CAL-3 (lead): a GPU slot for the first cold load** to produce the
  FOOTPRINT line; Resource Control shows "not measured yet" until then.
* ~~**B-CAL-4**: the `web/` asset budget~~ — **CLEARED** by the lead: the budget is
  now 700 KiB with a 64 KiB per-module cap (`GX_BUILD_MODULE_KB`) and
  `GX_BUILD_EXCLUDE=flows`. `call.js` is 38.9 KiB, well under the module cap, and
  the lead asked that it **not** be trimmed.
* **Not a blocker, reported:** `web/js/pages/music.js` currently does not render
  in the offline harness (MUS work in progress), which fails
  `offline.a-shell.spec.js`.

## Image build (B-CAL-2)

Started **2026-09-17 21:26 SAST** on gx10-02, detached with `setsid nohup` so it
survives the SSH session. GPU is untouched: this is a `docker build`, and the
CUDA kernels of mamba-ssm / causal-conv1d are compiled by `nvcc` on the CPU.

```
ssh legenex-02@gx10-02
cd ~/Documents/Projects/Server/gx-cluster        # commit 70dba5a
docker build --progress=plain \
  -t gx-call-engine:voicechat-097dfe9-t214 legenex/call/engine
```

Log: **`/srv/logs/gx-call/build-20260917T192556Z.log`** on gx10-02.

### Attempt 1 FAILED (for real this time) — root cause

Ended 21:50 after 480 s in `[builder 3/3]`, `Dockerfile:36`, the
`pip wheel … mamba-ssm==2.3.2.post1` step. `causal-conv1d` built (348 s);
`mamba-ssm` did not. 18 compiler errors, all of one kind:

```
torch/include/ATen/ATen.h:5:2: error: #error C++20 or later compatible compiler
                                      is required to use ATen.
torch/include/c10/util/intrusive_ptr.h:775:27: error: 'strong_ordering' in
                                      namespace 'std' does not name a type
  note: 'std::strong_ordering' is only available from C++20 onwards
torch/include/ATen/core/TensorBase.h:1031:5: warning: identifier 'requires' is
                                      a keyword in C++20 [-Wc++20-compat]
RuntimeError: Error compiling objects for extension
```

The nvcc invocation in the log shows why, and also shows a second problem:

```
nvcc … -O3 -std=c++17 -U__CUDA_NO_HALF_OPERATORS__ …
  -gencode arch=compute_75,code=sm_75   -gencode arch=compute_80,code=sm_80
  -gencode arch=compute_87,code=sm_87   -gencode arch=compute_90,code=sm_90
  -gencode arch=compute_100,code=sm_100 -gencode arch=compute_120,code=sm_120
  -gencode arch=compute_103,code=sm_103 -gencode arch=compute_110,code=sm_110
  -gencode arch=compute_121,code=sm_121 --threads 4
```

Both come from the packages' own `setup.py`, and **no environment variable can
override either**:

1. `-std=c++17` is hardcoded in `extra_compile_args["cxx"]` **and** `["nvcc"]`
   (mamba-ssm 2.3.2.post1 `setup.py:213,216,226,230`; causal-conv1d 1.6.2.post1
   `setup.py:209,212`).
2. The nine `-gencode` pairs are appended by hand to `cc_flag`
   (`setup.py:181-201`) and spliced in as `… + cc_flag`. Because the caller
   already passes `-gencode`, torch's `_get_cuda_arch_flags` stays out of the
   way, so `TORCH_CUDA_ARCH_LIST="12.0"` — which the Dockerfile *does* set — has
   no effect at all.

**Why the env-variable route was skipped, with evidence** (the lead asked for
least-invasive first; these two checks are cheaper than an 8-minute build and
settle it):

* `torch/utils/cpp_extension.py` at tag **v2.14.0** contains **zero**
  occurrences of `NVCC_APPEND_FLAGS`, `NVCC_PREPEND_FLAGS` and `CXXFLAGS`
  (verified against the file fetched from the v2.14.0 tag; `TORCH_CUDA_ARCH_LIST`
  appears 6 times, only inside `_get_cuda_arch_flags`). torch simply never reads
  those variables, so nothing they contain can reach nvcc or g++.
* Even an nvcc-only override would not be enough: one of mamba-ssm's sources,
  `csrc/selective_scan/selective_scan.cpp`, is a **host `.cpp`** compiled with
  the `"cxx"` flags.
* A newer release is not an option either: **2.3.2.post1 is the latest
  mamba-ssm on PyPI** (checked on the PyPI JSON API, 2026-09-17).

### The fix

`legenex/call/engine/patch-cuda-ext.py` (new) patches both unpacked sdists in
the builder stage before `pip wheel`:

* every `"-std=c++17"` → `"-std=c++20"` (4 in mamba-ssm, 2 in causal-conv1d);
* every `+ cc_flag` → `+ ["-gencode", "arch=compute_120,code=sm_120"]`, which
  leaves the nine `cc_flag.append(...)` statements building a list that nothing
  reads, so the compiler receives exactly one architecture.

It **asserts** the counts it expects and exits non-zero if upstream changes, so
the build fails loudly rather than silently producing the wrong binary. Dry-run
on both real sdists: the patched `setup.py` still parses, `+ cc_flag` occurrences
drop to 0, and re-running the patch refuses as designed. The Dockerfile carries
the whole rationale as a comment so nobody has to rediscover it.

`causal-conv1d` is patched too even though it compiled under C++17 — its sources
simply do not pull in `ATen.h` — so both agree with the torch 2.14 headers.

### Attempt 2 (patched)

```
ssh legenex-02@gx10-02
cd ~/Documents/Projects/Server/gx-cluster        # commit 931dc3a
docker build --progress=plain \
  -t gx-call-engine:voicechat-097dfe9-t214 legenex/call/engine
```

Log: **`/srv/logs/gx-call/build-20260917T201527Z.log`**, started 22:15 SAST.
The patch step reported exactly the expected counts:

```
patched causal_conv1d-1.6.2.post1/setup.py: 2x -std=c++17 -> c++20, 2x cc_flag -> sm_120 only
patched mamba_ssm-2.3.2.post1/setup.py: 4x -std=c++17 -> c++20, 2x cc_flag -> sm_120 only
```

**causal-conv1d: 348 s -> 134 s** for the same wheel, and it still builds under
C++20. mamba-ssm then compiled past the point where attempt 1 died, with 0
errors. **OUTCOME: BUILD SUCCESSFUL 2026-09-17 21:09 SAST.** Image `1e61699df9e7` (18.3 GB) on gx10-02. Import gate **PASSED**: `VoiceChatEngine`, `VoiceChatPatches`, `Tracker` all importable inside the container. The build completed without the `mamba_ssm` errors from attempt 1.

Node-2 state when it was started (VOI's voice acceptance was running, so the
build was checked not to crowd it): `gx-voice-engine` up, media router and
ComfyUI healthy, **MemAvailable 104.6 GiB**, load average 1.19, 219 GB free on
`/`. The Dockerfile already pins `MAX_JOBS=2` and `NVCC_THREADS=1`, so the wheel
build stays bounded. MemAvailable was 111 GiB two minutes in.

**Nothing was started.** The engine container was not run and the model was not
loaded, per the lead's instruction: gx10-02's GPU is held by the VOI acceptance
with a music acceptance queued behind it.

**`--selftest-cache` cannot be run yet — it needs the GPU.** Evidence:
`engine/gx_call_engine.py:610` `selftest_cache()` calls `VoiceChatEngine()` then
`engine.load()` (the full pipeline load), and then builds tensors on
`emb.device`, which is the CUDA device the pipeline was loaded onto. It is
deferred to the GPU slot and is step 2 of the acceptance plan above.

## Log

- 2026-09-17 16:35: started. Download of the pinned checkpoint started on gx10-02.
- 2026-09-17 17:15: `gx-call-engine` image build cancelled during the wheel stage.
- 2026-09-17 20:40: re-ran `tests.test_calls` before editing: 15 tests, 3 failures + 1 error.
  All four previously reported failures reproduced.
- 2026-09-17 20:55: fixes in `call_agents.py`, `calls.py` and
  `legenex/call/tests/engine_stub.py`; `tests.test_calls` 15/15 OK, `legenex/call/qa.sh` 28/28 OK.
- 2026-09-17 21:20: `web/js/pages/call.js` and `e2e/offline.h-call.spec.js` written;
  5/5 offline specs green in the integration overlay, build-check OK.
- 2026-09-17 21:35: `docs/18-call-agents.md` written; all CAL ruff (15) and mypy (6)
  findings cleared.
- 2026-09-17 21:45: final verification: `tests.test_calls` 15/15 OK, full control-ui
  suite **708 tests OK**, `legenex/call/qa.sh` 28/28 OK, playground build-check OK,
  `offline.h-call.spec.js` 5/5 green in the integration overlay.
- 2026-09-17 21:26: lead applied all five integration lines; `offline.h-call.spec.js`
  **5/5 green in the repo unchanged**. `gx-call-engine` image build restarted on gx10-02
  (`/srv/logs/gx-call/build-20260917T192556Z.log`).
- 2026-09-17 21:30: re-ran on the current checkout (other workstreams have landed
  changes): `tests.test_calls` **15/15 OK**, `legenex/call/qa.sh` **28/28 OK**,
  all five gate steps pass.
- 2026-09-17 21:09: `gx-call-engine:voicechat-097dfe9-t214` **BUILT** (1e61699df9e7, 18.3 GB).
  Import gate **PASSED**: VoiceChatEngine, VoiceChatPatches, Tracker all importable.
