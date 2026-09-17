# LIV — gx-live (MiniCPM-o 4.5) workstream log

Owner: LIV specialist. Contract: `coordination/BUILD_V3.md`. Newest entries first.

## Status (living)

| Item | State |
|---|---|
| Model | `openbmb/MiniCPM-o-4_5` @ `503e754207c94da6bb26850b4469f367c9ea3582` (apache-2.0, public), at `/srv/models/live/MiniCPM-o-4_5` on gx10-02 (verified) |
| Runtime | transformers remote code (4.51.0) on the proven GB10 torch 2.14/cu130 stack, SDPA (no flash-attn for sm_121). Image `gx-live-engine:minicpmo45-503e754-t214` built from `legenex/live/engine/Dockerfile` on gx10-02 |
| Service | `gx-live.service` (node 2, `192.168.100.11:18850` + `127.0.0.1:18850`), key `secrets/gx-live/api-key` |
| Footprint | measured 2026-09-17 (probe 1): see FOOTPRINT line below |
| Control Center | `gx_control_ui/live.py` (`App.live`) + `routes_liv.py` (`/api/live/*`, `/v1/live/*`), migration `060_live.sql` — **written, tested, waiting for the lead's two lines in `server.py`** |
| Playground | `web/js/pages/live.js` + shared `web/js/realtime/{audio,camera,capture-worklet,playback-worklet}.js` — **written, offline E2E green, waiting for the lead's nav/route/ALLOW lines** |
| Node 2 | `gx-live.service` symlinked into the node-2 checkout, `daemon-reload` done, **not started, not enabled at boot** (this wave starts no GPU workload) |

## Interfaces other workstreams need (PLT, CAL, LEAD)

* **Node-2 service:** `gx-live` on `192.168.100.11:18850` (+ `127.0.0.1:18850`), bearer key
  `/srv/projects/gx-cluster/secrets/gx-live/api-key` (0600 on both nodes).
* **Engine container / ledger entry:** `gx-live-engine` (plt.md 5.1), class `medium`, node `node2`.
* **Open health (plt.md 5):** `GET /health` → `{"service": "gx-live", "state":
  "unloaded|waiting|loading|ready|busy|unloading|failed", "busy", "pinned", "active_sessions",
  "active_jobs": 0, "queue": 0, "waiting": {code, reason, required_gib, available_gib, pending_gib} | null,
  "idle_seconds", "memory": {"estimate_gib", "resident_gib", "pending_gib", "reserve_gib"}}`.
  `busy` = a live session is open (the model is exclusively held by it).
* **Unload:** `POST /v1/live/unload {"if_idle": true}` → 409 `busy` (session live or loading) /
  `pinned`; `{"if_idle": false, "reason": "gxmax"}` ends the session (`session.ended` reason `gx_max`)
  and unloads with verification (container gone, ledger entry gone, no engine processes). A fresh
  `node2.gxmax-hold` does the same within 1 s.
* **Tunnel upstream path (PLT §1.3):** `/v1/live/sessions/<live_id>/ws?join=<node-2 join token>`. The
  join token is created by gx-live when the Control Center creates the session, is compared in
  constant time, and never reaches a client. The supervisor also requires the bearer key and
  `X-GX-Session` == path id, and pings every 20 s.
* **Tool bridge (Control Center → node 2, fabric):** `GET /v1/live/sessions/<id>/tool-calls?wait=25`
  (long poll) and `POST /v1/live/sessions/<id>/tool-calls/<call_id>` (`{"kind": "progress"|"result", ...}`).
* **Shared realtime pieces (CAL):** stdlib RFC 6455 endpoints in `legenex/common/gxcommon/rtws.py`
  (`accept(handler)`, `connect(host, port, path, headers=)`, masking/size/close rules, tests in
  `legenex/common/tests/test_rtws.py`). Browser capture/playback worklets in
  `legenex/playground/web/js/realtime/` (see the log entry when they land).
* **Protocol:** `legenex/live/PROTOCOL.md` (`gx-live.v1`).

## Measured footprint (PLT parses this line)

**Corrected by the GPU acceptance on 2026-09-17 22:0x** (was `resident_gib=31 startup_s=102
evidence=.../probe1-feasibility`). `cold_gib` stays 34: it is the admission estimate, it was never
exceeded, and the measured node growth was 30.6 GiB. `startup_s` rises to 128 because both real cold
loads today took 123-128 s, not 102 s - probe 1 measured the weight load alone on a quiet node, while a
real cold start also pays the per-session speaker-prompt encode and shares the disk with whatever else
is loading (gx-voice, then gx-music, were resident/loading during these two).

FOOTPRINT gx-live node=gx10-02 cold_gib=34 resident_gib=30 startup_s=128 measured=2026-09-17 evidence=/srv/logs/acceptance/build-v3/liv/acceptance

* Weights on GPU after load: 20.7 GiB (torch allocated). During speech turns torch holds 24-26 GiB
  (reserved up to 29.5 GiB after a native-duplex test). CUDA context, ONNX speaker model and Python
  add ~3 GiB.
* MemAvailable on gx10-02 fell from 60.8 to a minimum of 28.1 GiB during probe 1 (1 Hz samples,
  `mem.tsv`), i.e. ~32.7 GiB at peak with other tenants steady. The probe had been admitted with a
  30 GiB estimate, so the 30 GiB reserve was undershot by ~1.9 GiB for ~20 s at the end of the
  probe (native-duplex test). The service estimate is therefore **34 GiB** (class `medium`).
* Cold start: 97 s weight load (disk-bound while other downloads ran; 94-186 s observed) + 5 s TTS init.

## Log

- 2026-09-17 22:05: **GPU ACCEPTANCE PASSED on the deployed stack** (evidence
  `/srv/logs/acceptance/build-v3/liv/acceptance/`). Real MiniCPM-o 4.5, real microphone audio, real
  camera frames, real tools, on gx10-02 while gx-voice and then gx-music were also resident. Node 2 was
  left **idle**: engine unloaded, container gone, ledger empty, 109.8 GiB MemAvailable.

  **Two real bugs were found and fixed, both invisible to the offline suite:**

  * **B-LIV-4 (shipped fix, `gx_live/service.py`): a warm model had a dead microphone.** The engine gates
    microphone audio on `client_attached`, which is decided when the engine link opens. On a warm model
    that happens *before* the browser's WebSocket arrives (create -> link -> connect), so the engine was
    told "no client" and silently dropped every mic frame for the whole session; typed input still
    worked, which is why nothing looked broken. `serve_client` now announces the attach whenever an
    engine link exists, not only when the engine is already ready. Regression test
    `test_client_attached_reaches_the_engine_when_it_linked_first` **fails on the pre-fix code**
    (`('json', 'client.attached') not found in []`) and passes after - the stub engine gained a
    `ready_delay` so it reproduces the real 1.5 s window. Supervisor suite is now **30 tests**.
  * **B-LIV-5 (shipped fix, `web/js/pages/live.js`): short sessions lost their turn timings.** Turns were
    only flushed at 5 pending / every 15 s / on `finish()`, so a one-turn call ended by navigation wrote
    nothing to `live_turns` (observed: 0 rows after a real browser session). Every finished turn is now
    posted immediately, and the page's cleanup flushes before it tears down. Verified: a real browser
    turn now lands as `response=1 trigger=speech status=completed first_audio_ms=1764 turn_ms=6846
    audio_ms=11240 assistant_chars=171`.
  * Also hardened: the delegation "why am I waiting" hint now runs off the answer's path (it called
    Resource Control inline), and the offline spec waits for a session to be released before the next
    test starts.

  **Cold start (1 Hz MemAvailable sampling, `mem.tsv`, 661 samples / 666 s):**

  | | |
  |---|---|
  | baseline before the load | 92.0 GiB |
  | minimum during load + whole session | 61.4 GiB |
  | growth | **30.6 GiB** (admission estimate 34 GiB was never exceeded) |
  | headroom at the minimum | 31.4 GiB above the 30 GiB reserve - **the reserve was never breached** |
  | startup | engine **128.1 s**, supervisor `load_ms` 131.0 s, client create->ready 132.0 s |
  | GPU allocated after load | 22.68 GiB (engine log) |
  | resident (supervisor `/health`) | 26.3-30.1 GiB while a session ran |
  | after unload | 110.0 GiB |

  A second cold start earlier the same hour (while gx-voice was loading) took **124.9 s**. Probe 1's
  102 s was the weight load alone on a quiet node; the FOOTPRINT line above is corrected to 128 s.

  **Acceptance items, each with its evidence and how it was measured:**

  | Item | Result | Evidence / method |
  |---|---|---|
  | cold start + sampling | 128.1 s, growth 30.6 GiB, reserve never breached | `mem.tsv` (1 Hz), `acceptance.json.cold_start`, engine log |
  | guarded load path | `gx_guard_run` admitted it: "admitted: 109.1GiB projected of 121.0GiB node total; MemAvailable leaves 44.8GiB (reserve floor 30.0GiB)"; ledger `gx-live-engine class=medium 34 GiB` | `/srv/logs/gx-live/gx-live.log`, `node2-residency.json` |
  | real spoken conversation | 4.04 s of real speech in, **ASR: "Hello, can you tell me what a fossil is in two sentences?"**, answer "A fossil is the preserved remains or impression of an ancient organism..." | `events.jsonl`, `turn1_assistant.wav` (13.16 s, peak -1.89 dBFS) |
  | first-audio latency | **1857 ms** (server), median 1857 / min 1621 / max 1867 over 4 spoken responses; client-side end-of-speech -> first audio frame 2457 ms (the 700 ms VAD window is inside that) | `acceptance.json.turn1_spoken`, `record.metrics.latency` |
  | turn latency | median **3569 ms** (min 1588, max 8424) over 8 turns | `record.metrics.latency.turn_ms` |
  | barge-in | **169 ms** server-measured (speech detected -> generation stopped), reason `barge_in`; assistant audio frames froze at 2 and stayed 2 for 1.5 s afterwards | `acceptance.json.barge_in`, `turn2_interrupted.wav` (2.00 s) |
  | live camera vision | **"I see a red circle, a blue square, and the number seven."** for a JPEG holding exactly a red circle, a blue square and a black 7 | `vision_frame.jpg`, `turn4_vision.wav`, `acceptance.json.vision.mentions` |
  | `delegate_to_gx` | model emitted `{"model":"gx-fast","task":"Write a haiku about GPUs."}`; gateway answered in **494 ms**, `routed_to=gx-fast`, and the haiku was spoken back | `live_tool_calls` row, `run.log` |
  | `get_time` | 2 ms, spoken back as "It's currently 9.57 PM on Thursday, September 17th, ... South African Standard Time" | `live_tool_calls`, `run.log` |
  | `search_library` | 5 matches in 6 ms, **5 `live_session_assets` provenance rows** joined to real `assets` ids | `live_session_assets`, `record.assets` |
  | `fetch_url` | https://example.com through netguard in 154 ms, real page text returned | `live_tool_calls`, `run.log` |
  | Save transcript / Delete conversation | 16 entries saved and read back; after delete **0 entries, `content_purged=1`** | `acceptance.json.transcript` |
  | clean shutdown, route change | session `ended` / `completed` the moment the page navigated away | `browser_leg.json` |
  | clean shutdown, page-hide | session `ended` / `abandoned` after the documented 60 s reconnect grace; `gx-live` back to `ready`, `active_sessions 0` - **the model is never left held** | `browser_leg.json`, node-2 summary |
  | DB rows | `live_sessions` 3, `live_events` 16 (`session.created`, 4x `tool.call`+`tool.result`, `transcript.saved`, `session.ended`, `content.deleted`), `live_tool_calls` 4 **with argument names only** (`["model","task"]`, `["query","type"]`, `["url"]`, `[]`), `live_session_assets` 5, `live_turns` 1 (after the B-LIV-5 fix) | `sqlite3` on the live DB |
  | memory returns after unload | `POST /v1/live/unload {"if_idle":true}` -> `returned_gib 26.1, container_gone true, ledger_released true, engine_processes 0, verified true` in 4.2 s; independently: container 0, ledger `[]`, 0 engine processes, 109.8 GiB | unload response + direct checks |

  **Browser leg** (`browser_leg.mjs`, real Chromium on **https://127.0.0.1:8443**, `isSecureContext: true`,
  fake device fed from `browser_mic.wav` = real recorded speech): the Live page's own capture worklet
  streamed the speech, the page showed "First audio (median) 1764 ms / Turn (median) 6846 ms / Assistant
  audio 11.2 s / Model load 131.0 s", the camera preview ran at 1280 px, Save transcript wrote 2 lines,
  and there were **no console or page errors**. Screenshot: `browser-live-session.png`.

  **What I did NOT verify:** I cannot listen. The four assistant WAVs are real 24 kHz PCM with sane
  levels (peak -1.89 to -5.16 dBFS, RMS ~-23 dBFS, 92-98 % non-zero samples), so they are certainly not
  silence, and their *content* is confirmed by the model's own captions and by the ASR of what it heard -
  but whether the speech *sounds* natural is a subjective judgement a human still has to make. The four
  WAVs are in the evidence directory for exactly that.


- 2026-09-17 21:2x: **Control Center, Live page and offline acceptance (no GPU workload started).**

  **Verified test totals (all run on gx10-01, this checkout):**

  | Suite | Command | Result |
  |---|---|---|
  | gx-live service + engine | `legenex/live/qa.sh` | supervisor **29 tests OK**, engine **17 tests OK**, ruff clean, credential scan clean -> `QA PASSED` (re-run after the PROTOCOL.md edit) |
  | Control Center unit suite | `(cd legenex/control-ui && python3 -m unittest discover -s tests)` | **707 tests OK** in 142 s (was 678 before this workstream; `tests/test_live.py` adds **29**) |
  | LIV only | `python3 -m unittest discover -s tests -k test_live` | **30 OK** (my 29 + PLT's existing `test_resources_v3.test_live_session_needs_confirmation_to_unload`) |
  | ruff / mypy on the new modules | `.venv/bin/ruff check ...` / `.venv/bin/mypy gx_control_ui/live.py gx_control_ui/routes_liv.py` | **clean** |
  | Playground build check | `node scripts/build-check.mjs` | **OK: 36 modules, 39 files, 580.6 KiB (budget 600 KiB)** |
  | Live offline E2E (incl. axe WCAG 2.2 AA) | `e2e/offline.h-live.spec.js`, 5 tests | **5 passed (24 s)** — see "How the E2E was run" below |

  `tests/test_live.py` runs the **real gx-live supervisor** (legenex/live) with the stub engine from its
  own suite, so the session create, the tunnel registration, the tool bridge long poll, the tool
  execution on gx10-01 and the end-of-session record all cross the real HTTP/WebSocket APIs. LiteLLM and
  the URL fetcher are stubs; there is no GPU, no Docker and no traffic off loopback.

  **Migration `060_live.sql` (dry run against a COPY, never the live file).**
  Copied `/srv/projects/gx-cluster/media/metadata/library.db` (536,576 B, migrations 010/020/030/040/050/080
  applied, 25 assets) to the scratchpad and applied the file through the real applier path
  (`media_library._split_sql`, one transaction, `schema_migrations` row):

  * 15 statements; created `live_sessions`, `live_events`, `live_turns`, `live_tool_calls`,
    `live_transcripts`, `live_session_assets` and 9 indexes.
  * Running every statement a second time changed nothing (all `IF NOT EXISTS`); `PRAGMA integrity_check` =
    `ok`; `foreign_key_check` empty; `assets` still 25 rows; `call_*`, `voice_*`, `wan_*`, `flow_*`,
    `plt_preferences` untouched.
  * Write smoke test: session -> event -> turn -> tool call -> transcript -> `live_session_assets` joined to
    a real `assets` row. A duplicate `(session_id, response)` turn is refused
    (`UNIQUE constraint failed: live_turns.session_id, live_turns.response`), which is what makes the
    browser's turn reports idempotent (`ON CONFLICT ... DO UPDATE`).
  * `tests/test_live.py::SchemaTests` re-checks both properties on a fresh database in CI.

  **What is stored, and what is not.** Session record, per-turn timings, tool name + argument **names** +
  timing + outcome + delegated model, and which Library assets `search_library` surfaced. Never the
  arguments' content, the instructions, the transcript (unless the owner presses Save transcript, which
  writes `live_transcripts`), and never audio or camera frames. `_meta_only()` drops content keys before
  anything reaches `live_events`; a test asserts no metric line carries `text`/`transcript`/`prompt`/`task`/
  `join_token`.

  **PROTOCOL.md section 6 corrected:** a saved transcript is stored in `live_transcripts`, not as a
  "Library text asset" — `MediaLibrary` supports image, video and audio only, and adding a text type would
  mean rewriting shared library code. Deleted by *Delete conversation* and automatically after 30 days.

  **Live page.** Real microphone capture (AudioWorklet, 16 kHz PCM, 100 ms frames, level meter, mute),
  real playback (AudioWorklet, 24 kHz, gapless, instant flush for barge-in), camera stills at 1/s with a
  preview, typed turns, push-to-talk commit, Stop (interrupt), tool activity panel, live metrics
  (turns, median first-audio/turn/interruption latency, assistant audio received), Save transcript,
  Delete conversation, End session, and a clean shutdown on page-hide and on route change (the model is
  never left held). The shared browser realtime code is `web/js/realtime/` — **CAL can import
  `startMicCapture`, `createSpeaker`, `mediaErrorText` and `startCamera` as they are**; the gx-live binary
  framing stays in the page.

  **Deviation to note:** `web/css/app.css` got an appended `.live-*` block (plus two `.live-side .card-*`
  rules) at the end of the file, the same way VOI/WAN/MUS appended theirs. It is additive and prefixed; no
  existing rule was changed.

  **How the E2E was run without touching the lead's shared files.** The spec is shipped at
  `legenex/playground/e2e/offline.h-live.spec.js` and the stub at `legenex/control-ui/e2e/live_stub.py`
  (the real gx-live supervisor + stub engine, the VOI pattern). Because the nav link, `routes.js`, the
  Playground `ALLOW`/`SPA_ROUTE` and the `App.live` block are all yours, I ran the suite against a
  *staged copy* in my scratchpad that applies exactly the lines requested below (a copy of `web/` with the
  two lines added, a `gx_playground.server` launcher that appends the `ALLOW` entries and `SPA_ROUTE`, and
  a `fixture_server` wrapper that attaches `App.live`). Nothing in the repo was changed for the run.
  **After you apply the lines below, `npm run qa` in `legenex/playground` picks the spec up unchanged.**


- 2026-09-17 16:40: model downloaded and verified (`hf-verify.py`, 54 files, 18.67 GiB,
  `.gx-manifest.json`). Engine image `gx-live-engine:minicpmo45-503e754-t214` built on gx10-02
  (torch 2.14.0+cu130, transformers 4.51.0, onnxruntime 1.21.0, minicpmo-utils 1.0.6, silero-vad 6.2.1,
  ruamel.yaml pinned to 0.18.10 because 0.19 breaks hyperpyyaml).
- 2026-09-17 16:56: **GB10 feasibility (probe 1, admission-guarded, evidence
  `/srv/logs/acceptance/build-v3/liv/probe1-feasibility/`)**:
  * transformers remote code + SDPA works on sm_121 (no flash-attn). torchaudio 2.11 needs TorchCodec for
    `load/save`; the engine installs a soundfile shim (`gx_live_engine/compat.py`).
  * image understanding OK ("fossil of an ancient bird embedded in rock"), ASR via `chat()` 1.4 s for 4 s of
    speech and exact; ASR between turns does not disturb the streaming session.
  * half-duplex streaming speech: prefill 0.28-0.57 s, first audio 1.33-1.70 s after prefill, 1.5-1.9x
    faster than realtime. Breaking the generator mid-answer (interruption) leaves the session usable.
  * vision inside a speech turn OK ("I see cars, a road, trees, buildings, and signs").
  * tool calls: with text input the model emits exact `<tool_call>` JSON (get_time, delegate_to_gx
    gx-fast); in a speech session with the tool block appended to the voice prompt it did NOT call the
    tool; a `<tool_response>` prefill was then spoken correctly. Prompt variants under test (probe 2).
  * **native full-duplex (`as_duplex`) is not realtime on GB10**: 0.74 s per 1 s unit while listening, but
    1.5-1.6 s per unit while speaking (prefill 0.5 s + generate 1.05 s). The shipped mode is therefore
    VAD turn-taking with barge-in over a full-duplex transport (mic and camera keep streaming while the
    assistant speaks; speech onset interrupts). Duplex numbers are kept here as the measured reason.


## Integration requests for the lead

Everything below is copy-pasteable. Nothing else of mine needs a shared file.

### 1. `legenex/control-ui/gx_control_ui/server.py` — `App.__init__` block

Put it after the CAL block (it needs `self.realtime`, `self.activity`, `self.library`, `self.actions`,
`self.resources` and `self.cluster`, all of which exist by then):

```python
        # --- Build V3 LIV: gx-live realtime sessions and their tools (live.py, routes_liv.py)
        from .live import LiveClient, LiveManager
        self.live = LiveManager(connect=self.library.connect,
                                client=LiveClient(cfg.live_base, cfg.secrets_root / "gx-live" / "api-key"),
                                realtime=self.realtime, library=self.library,
                                gateway_base=cfg.litellm_base,
                                gateway_headers=self.cluster.litellm_headers, audit=self.actions.audit,
                                metric=metric, explain=lambda alias: self.resources.explain(alias),
                                start_threads=not cfg.offline)
        self.activity.register("live", self.live.activity)
```

### 2. `legenex/control-ui/gx_control_ui/server.py` — import line

Next to the other `routes_*` imports at the end of the file:

```python
from . import routes_liv  # noqa: E402,F401  (Build V3 LIV: Live sessions, /v1/live)
```

### 3. `legenex/playground/web/js/routes.js`

```js
  live: () => import('./pages/live.js'),
```

### 4. `legenex/playground/web/index.html` — inside `<ul class="rail-list" id="nav-realtime">`

```html
          <li><a class="rail-link" href="#/live" data-page="live"><span class="rail-ic" data-icon="camera"></span><span class="rail-label">Live</span></a></li>
```

### 5. `legenex/playground/gx_playground/server.py` — `SPA_ROUTE`

Add `live` to the alternation (shown with VOI's `voice` already in place):

```python
SPA_ROUTE = re.compile(r"/(dashboard|images|video|music|voice|live|library|history|models|logs|settings)"
                       r"(/[a-z0-9_\-]{0,64}){0,2}")
```

### 6. `legenex/playground/gx_playground/server.py` — `ALLOW` entries

```python
        # Build V3 LIV: the Live page (session) and the public gx-live API (gateway key)
        ("GET,POST", r"/api/live/(model|sessions)"),
        ("GET,POST", r"/api/live/sessions/live_[0-9a-f]{32}(/(end|events|turns|transcript|delete-content))?"),
        ("GET,POST", r"/v1/live/(model|sessions)"),
        ("GET,POST", r"/v1/live/sessions/live_[0-9a-f]{32}(/(ticket|end|events|turns|transcript))?"),
```

Exhaustive list of the paths those four regexes must cover (browser routes are session+CSRF, `/v1` is
gateway-key only, never a cookie):

| Method | Path |
|---|---|
| GET | `/api/live/model` |
| GET, POST | `/api/live/sessions` |
| GET | `/api/live/sessions/<sid>` (`?refresh=1`) |
| GET | `/api/live/sessions/<sid>/events` (`?after=`) |
| POST | `/api/live/sessions/<sid>/turns` |
| GET, POST | `/api/live/sessions/<sid>/transcript` |
| POST | `/api/live/sessions/<sid>/end` |
| POST | `/api/live/sessions/<sid>/delete-content` |
| GET | `/v1/live/model` |
| GET, POST | `/v1/live/sessions` |
| GET | `/v1/live/sessions/<sid>` |
| POST | `/v1/live/sessions/<sid>/ticket` |
| GET | `/v1/live/sessions/<sid>/events` |
| POST | `/v1/live/sessions/<sid>/turns` |
| GET, POST | `/v1/live/sessions/<sid>/transcript` |
| POST | `/v1/live/sessions/<sid>/end` |

The WebSocket path `/rt/live/<sid>` is already handled by PLT's tunnel and needs no `ALLOW` entry.

### 7. `legenex/playground/scripts/build-check.mjs` — `GX_BUILD_PAGES`

```js
process.env.GX_BUILD_PAGES = 'dashboard,images,video,music,voice,live,library,history,models,logs,settings';
```

(Add `live` in the Realtime position; CAL will add `call` next to it.)

### 8. `legenex/control-ui/e2e/fixture_server.py` — three lines, so the offline E2E runs in `npm run qa`

```python
from live_stub import LiveStub  # noqa: E402  (Build V3 LIV: the real gx-live API, stub engine)
```

in `main()`, next to `voice = VoiceStub(...)`:

```python
    live = LiveStub()
```

and in the `TempEnv(...)` call add:

```python
                  live_base=live.url, rt_live_target=f"127.0.0.1:{live.port}",
```

and after `auth.PasswordStore(...)`, before `srv.build`:

```python
    (env.cfg.secrets_root / "gx-live").mkdir(parents=True, exist_ok=True)
    (env.cfg.secrets_root / "gx-live" / "api-key").write_text(live.key)
```

(`UIConfig` already has `live_base` and `rt_live_target`; `LiveStub.close()` exists for the `finally`.)

### 9. `legenex/models/registry.json` — the `gx-live` alias

```json
    "gx-live": {
      "alias": "gx-live",
      "task": "realtime-omni",
      "node": "gx10-02",
      "repository": "openbmb/MiniCPM-o-4_5",
      "revision": "503e754207c94da6bb26850b4469f367c9ea3582",
      "licence": "apache-2.0",
      "runtime": "transformers 4.51.0 remote code (MiniCPMO), torch 2.14.0+cu130, SDPA, bfloat16",
      "quantization": "none (bf16)",
      "image": "gx-live-engine:minicpmo45-503e754-t214",
      "path": "/srv/models/live/MiniCPM-o-4_5",
      "capabilities": ["audio-in", "audio-out", "vision", "text", "interruption", "tools"],
      "languages": ["en", "zh"],
      "protocol": "gx-live.v1",
      "concurrent_sessions": 1,
      "service": {"port": 18850, "unit": "gx-live.service", "container": "gx-live-engine",
                  "health": "http://192.168.100.11:18850/health"},
      "measured": {"cold_gib": 34, "resident_gib": 31, "startup_s": 102, "measured_on": "2026-09-17",
                   "evidence": "/srv/logs/acceptance/build-v3/liv/probe1-feasibility"}
    }
```

The FOOTPRINT line PLT parses is unchanged and already in this file:

```
FOOTPRINT gx-live node=gx10-02 cold_gib=34 resident_gib=30 startup_s=128 measured=2026-09-17 evidence=/srv/logs/acceptance/build-v3/liv/acceptance
```

### 10. Restarts I did not do

I did not restart `gx-playground.service` or `gx-control-ui.service` (you own restarts). Both are needed
after items 1-7 land, under the `state/build-v3/restart.lock` per BUILD_V3 rule 9.

## What the live GPU acceptance run needs from the lead

Nothing was started on either node in this wave. When the node-2 measurement window is over, the
acceptance run needs, in this order:

1. **A clear node 2**: no `node2.gxmax-hold`, no `node2.maintenance-hold`, and ~34 GiB free above the
   30 GiB reserve (gx-reason at ~32 GiB and a cold gx-live do not fit together — expect a wait, which the
   page reports with numbers; that wait is itself worth recording).
2. **Start the supervisor on gx10-02** (it loads no model):
   `ssh legenex-02@gx10-02 'systemctl --user start gx-live'` — the unit is installed and
   `daemon-reload`ed; `~/.config/gx-live/gx-live.env` exists with `GX_LIVE_BINDS=127.0.0.1,192.168.100.11`.
   Check `curl -s http://192.168.100.11:18850/health`.
   (I deliberately did **not** `systemctl --user enable` it; say the word if it should come up at boot.)
3. **Items 1-8 above applied and both services restarted**, otherwise the Live page is not reachable.
4. **A browser on a secure context**: `https://<host>:8443/#/live` with the Playground CA trusted, or
   `http://127.0.0.1:8090/#/live` on gx10-01 itself. `http://100.105.214.61:8090` cannot use the
   microphone.
5. Then I (or you) run: cold start with 1 Hz MemAvailable sampling, a spoken conversation, a barge-in, a
   camera question, a `delegate_to_gx` to gx-fast, a `search_library`, a `fetch_url`, Save transcript, and
   a clean end, with evidence under `/srv/logs/acceptance/build-v3/liv/acceptance/`. Roughly 25 minutes,
   with the node held for gx-live for that time.

## Open requests after the acceptance (lead)

### A. One line in `legenex/control-ui/e2e/fixture_server.py` (blocks 2 of my 5 offline tests)

The App block correctly uses `start_threads=not cfg.offline`, and the E2E fixture builds the app with
`offline=True` - so in the offline suite my **tool executor thread never runs** and a tool call sits at
`running` forever. The fixture already tweaks services after `srv.build` (`app.media.poll_interval`,
`app.voice.poll_interval`, `app.music.poll_interval`); please add next to those:

```python
    app.live.start_threads = True   # Build V3 LIV: run the tool executor in the offline suite
```

With the executor enabled the whole spec passed 5/5 (I ran it that way through a scratchpad copy of the
fixture before your integration landed). Against the fixture as it stands today it is **3 passed,
2 failed**: `a tool call runs on the Control Center and is shown` (the tool never completes) and
`the transcript is saved only when asked` (it inherits the busy session from the failed test).
Nothing else is needed - production is unaffected, this is test wiring only.

### B. Re-sync the corrected FOOTPRINT into `legenex/models/registry.json`

The line PLT parses now reads:

```
FOOTPRINT gx-live node=gx10-02 cold_gib=34 resident_gib=30 startup_s=128 measured=2026-09-17 evidence=/srv/logs/acceptance/build-v3/liv/acceptance
```

so `aliases.gx-live.measured_footprint` should become `cold_gib 34, resident_gib 30, startup_s 128`,
`measured_on 2026-09-17`, `evidence /srv/logs/acceptance/build-v3/liv/acceptance`.

### C. Restarts I did not do

`gx-live.service` on gx10-02 was restarted **by me** at 21:53 to pick up the B-LIV-4 fix (it is my
service; that restart is what unloaded the engine, which also produced the unload evidence). The
Playground and Control Center were not restarted by me - and note the Control Center restarted at
21:59:54 during my browser leg, which correctly closed the in-flight session as `restarted`
(`LiveManager._resume`), exactly as designed.

## Blockers

* ~~**B-LIV-1**~~ **CLEARED 2026-09-17 21:5x**: the lead applied all ten integration items; the Live page
  is live in the deployed Playground and the GPU acceptance ran through it end to end.
* **B-LIV-2 (not mine to fix, for information):** `mypy gx_control_ui` is red on other workstreams'
  in-progress code — 12 errors in 8 files at 21:30 (`activity.py`, `node2_services.py` x3, `music_ai.py`,
  `resources.py`, `image_catalog.py`, `music_reference.py` x2, `routes_plt.py` x2), so QA step 3 fails
  before my change. **`live.py` and `routes_liv.py` contribute none of them**: `routes_liv` reaches
  `App.live` through one `_live(h)` accessor with a single `type: ignore[attr-defined]`, so it is clean
  both before and after item 1 lands (`warn_unused_ignores = false`).
* **B-LIV-3 (watch, not blocking):** the Playground asset budget was raised to 700 KiB while I worked
  (it was 600 KiB and the tree briefly hit 1,076 KB when FLO's `web/flows/` bundle landed). Final state:
  `build check OK: 36 modules, 39 files, 581.6 KiB (budget 700 KiB)`. My page plus the shared realtime
  code are 45 KB of that; CAL's page still has to fit.
* **No blocker on the model side:** `/srv/models/live/MiniCPM-o-4_5` (19 GiB) and
  `gx-live-engine:minicpmo45-503e754-t214` (13.7 GB) are both still on gx10-02, verified today, and the
  bearer key exists at `/srv/projects/gx-cluster/secrets/gx-live/api-key` with mode 0600 on **both** nodes
  with an identical sha256 (compared as digests; the key was never printed).
