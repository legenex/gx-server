# Project map — gx-cluster (two-node GX10)

**Current phase:** production operation of **eleven** aliases (L-10 as amended
by D-040). There is automated source control, the admin Control Center
(`http://100.105.214.61:8088/`) and the creative GX-Playground
(`http://100.105.214.61:8090/`, and over HTTPS at
`https://100.105.214.61:8443/`, which is the secure context the Live and Call
Agents pages need for the microphone and camera).
Version: see `VERSION`.
**Canonical remote:** https://github.com/legenex/gx-server (`main`). This
repository is **public**.

## Where things are

| Path | What it is |
|---|---|
| `CLAUDE.md` | Operating rules and the LOCKED constraints table (L-1..L-10) |
| `CURRENT_STATE.md` | What is actually running now. Read first. |
| `ARCHITECTURE.md` | Locked decisions and why |
| `coordination/DECISIONS.md` | Decision log (D-001..) |
| `coordination/BLOCKERS.md` | Open and resolved blockers (B-001..) |
| `TEST_RESULTS.md` | Only results that were actually observed |
| `OPERATIONS.md`, `RECOVERY.md`, `Manual.md` | Operator documentation |
| `legenex/gateway/` | LiteLLM + llama-swap configs and compose files (node 1 gateway, node 2 worker) |
| `legenex/orchestrator/` | gx-auto routing and gx-max lifecycle service (stdlib Python), with tests |
| `legenex/lifecycle/` | gx-max start/stop/unwind/status, the safety rules, both watchdogs, the resource guard |
| `legenex/media/` | gx-image / gx-video router (**2.5.0**: Wan 2.2 LoRA catalogue, pairing and branch-safe graph building, on top of 2.4.0's 30 GiB reserve admission, waiting video jobs, idle-music eviction, holds, pins, truthful residency) and ComfyUI compose (node 2) |
| `legenex/music/` | gx-music: node-2 supervisor, ACE-Step engine Dockerfiles, unit, tests (D-036) |
| `legenex/playground/` | GX-Playground: static SPA + allow-listed proxy + realtime WebSocket tunnel + HTTPS listener, unit, E2E, API contract (D-037, D-040, D-041). `scripts/deploy.sh` is the only sanctioned deployment path |
| `legenex/playground/flows-ui/` | Creative Flows editor: React + TypeScript + `@xyflow/react`, built by Vite into `web/flows/` |
| `legenex/voice/` | gx-voice: node-2 supervisor, Qwen3-TTS engine, unit, tests (D-040) |
| `legenex/call/` | gx-call: node-2 supervisor, NemotronLabs VoiceChat engine, unit, tests (D-040) |
| `legenex/live/` | gx-live: node-2 supervisor, MiniCPM-o 4.5 engine, `PROTOCOL.md`, unit, tests (D-040) |
| `legenex/control-ui/gx_control_ui/migrations/` | Application-database migrations, one per feature; applied on Control Center start after a pre-migration copy |
| `coordination/build-v3/` | Per-workstream logs, evidence, measured footprints and blockers for D-040 |
| `legenex/host/` | Host watchdog and the kernel-lock tooling |
| `legenex/models/registry.json` | Alias → model bindings (repository, revision, path, previous, interim target, media components, verified `identity` facts). Tracked in Git since D-038 |
| `legenex/scripts/hf-verify.py` | Pinned-revision sha256 verifier; writes `.gx-manifest.json` |
| `legenex/control-ui/` | Control Center: stdlib backend (also the Playground's backend: Library, queues, music, Resource Control, Storage), ES-module frontend, in-UI docs, unit/API/E2E tests (D-028, D-037) |
| `legenex/tests/` | Acceptance, unwind regression, gx-max validation and inference suites; production Open WebUI identity (`owui_identity_acceptance.py`, `owui_identity_browser.py`) and the live reserve acceptance (`reserve_live_acceptance.py`) |
| `ops/git-sync/` | Writer, mirror and audit tooling for source control (D-026) |
| `.githooks/` | Versioned Git hooks (writer only) |

## Implemented

* **Eleven aliases (D-036, D-040):** gx-mini, gx-fast, gx-reason, gx-max,
  gx-auto, gx-image and gx-video on the gateway; gx-music and gx-voice through
  the gx10-01 APIs; gx-call and gx-live as realtime services exposed on the
  Playground over the WebSocket tunnel.
* **gx-max** on SGLang, TP=2 across both nodes over RoCE, with:
  * a cluster-takeover admission policy;
  * phase-aware safety rules on both nodes;
  * a deadman on node 2 and a watcher on node 1;
  * a verified unwind.
* **Resource guard:** flock plus residency ledger, with a 30 GiB reserve for
  single-node tiers. Since D-038 the media router and the music supervisor
  enforce the same reserve on projected MemAvailable and account for each
  other's loads in progress.
* **Open WebUI identity (D-038):** registry-generated model entries in the
  production Open WebUI (`gx_control_ui/owui_identity.py`), kept in sync by
  Model Manager, Setup and the integrity audit.
* **Git sync:**
  * the only writer auto-commits and pushes;
  * the mirror reconciles to `origin/main`;
  * a daily audit runs on both nodes.
* **Management web UI** (`gx-control-ui.service`, port 8088, loopback and
  Tailscale). Nine pages: Dashboard, Models, Runtime, Cluster, Jobs,
  Logs, API Playground, Docs and Settings. It has password sessions with
  CSRF, a fixed set of audited operations, and in-UI documentation.
* **Orchestrator lifecycle events** (read-only): gx-max phases, a live
  output buffer, and a persistent job history (D-029).
* **V2 model set (2026-09-17):** uncensored gx-mini, gx-fast and gx-max
  (CRACK). gx-reason runs an interim model (B-025). Bindings, pinned revisions
  and rollback targets live in `legenex/models/registry.json`.
* **Kilo-aware gx-auto** with a routing journal (D-030).
* **Media v2:** image generate, edit and variation; t2v, i2v and keyframe
  video edit (D-031).
* **UI pages Create, Media Library, Model Manager and API Keys** (D-034,
  D-035).
* **gx-music (D-036).** ACE-Step 1.5 XL turbo runs on gx10-02 behind a
  private supervisor. gx-max drains it with a hold and a verified unload.
  Idle ComfyUI weights are evicted only through the media router.
* **GX-Playground (D-037)** on :8090. It covers Dashboard, Images, Video, the
  Music studio, the unified Library (images, video and audio) and History,
  and it shares the Control Center sign-in.
* **Control Center additions (D-037):**
  * Resource Control: profiles, live map, admission, compatibility, manual
    controls, pins and Maintenance;
  * Storage & Cleanup: SAFE / REVIEW / PROTECTED, opaque ids, node-side
    re-check;
  * Setup for Kilo Code, Open WebUI and generic clients;
  * Model Manager disk preflight;
  * gx-music as a first-class model.

  Create and Media Library moved to the Playground.

* **Build V3 (D-040, D-041) — the complete creative + realtime product,
  deployed and verified in a real browser (14/14, axe WCAG 2.2 AA clean):**
  * **GX-Playground navigation:** Create (Dashboard, Creative Flows, Images,
    Video, Music, Voice) · Realtime (Live, Call Agents) · Manage (Library,
    History, Models, Logs, Settings).
  * **Creative Flows:** 67 node types, graph validation, versioning, templates,
    AI flow creation, and a React/`@xyflow/react` editor built into
    `web/flows/`.
  * **Wan 2.2 LoRAs:** catalogue with bounded safetensors header analysis,
    high/low classification and pairing, presets, and branch-safe graph
    building (high LoRAs only on the high-noise expert, low only on low, no
    shared nodes). Proven by a real generation.
  * **gx-voice:** Qwen3-TTS 1.7B (CustomVoice / VoiceDesign / Base +
    12 Hz tokenizer + whisper-large-v3-turbo) behind a node-2 supervisor, with
    a Voice Studio page.
  * **gx-live:** MiniCPM-o 4.5 behind a node-2 supervisor, with VAD turn-taking
    and barge-in over a full-duplex transport (native `as_duplex` is not
    realtime on GB10 — measured), camera vision, and server-side tools.
  * **gx-call:** NemotronLabs VoiceChat 11B and the Call Agents page (agent
    editor, live call, transcript, intake, dispositions, recordings).
  * **Platform:** realtime WebSocket tunnel with tickets and per-owner limits,
    an HTTPS listener on :8443 backed by a local CA (the secure context
    `getUserMedia` requires), the observability helper, and the Models, Logs
    and Settings pages fed by live data.
  * **Application database:** eight migrations applied — provenance, WAN,
    flows, voice, call, live, images, platform.
* **Deployment is proven, not assumed (D-041, B-031).** Both browser-facing
  servers re-read a static file when it changes, and
  `legenex/{playground,control-ui}/scripts/deploy.sh` fails unless the ETag of
  every served file matches sha256 of the file in the checkout.
* **Hugging Face access is diagnosed, not guessed (D-041, B-030).** 401 and 403
  are separate machine codes with separate human actions, and the Model Manager
  renders live token and per-repository accessibility state only.

## Runtime layout (outside Git)

| Path | Contents |
|---|---|
| `/srv/models` | weights, a separate copy per node |
| `/srv/logs` | logs; `gx-git-sync/` holds sync logs and drift evidence |
| `/srv/projects/gx-cluster/state` | guard locks and ledgers, git-sync role and lock, watcher pid, `orchestrator/gx-max-history.json`, `control-ui/model-results.json` |
| `/srv/projects/gx-cluster/secrets` | mode 0700; machine-local secrets; `control-ui/auth.json` (scrypt hash, 0600) |
| `/srv/projects/gx-cluster/backups` | pre-migration Git bundle |
| `/srv/projects/gx-cluster/media` | Media Library (`metadata/library.db` schema 2, images/videos/audio + thumbnails) |
| `/srv/projects/gx-cluster/state/guard/` | admission locks and ledgers, `profile.json`, `pins.json`, `node{1,2}.maintenance-hold`, `node2.gxmax-hold` (both nodes) |
| `/srv/projects/gx-cluster/state/control-ui/music-jobs.json` | music jobs submitted through gx10-01 and their Library import state |
| `/srv/projects/gx-cluster/secrets/gx-music/api-key` | node-2 music supervisor key (0600, both nodes) |
| `/srv/projects/gx-cluster/secrets/control-ui/proxy-token` | Playground → Control Center proxy trust (0600) |
| `/srv/models/music`, `/srv/models/music-data` (gx10-02) | ACE-Step checkpoints; supervisor job DB, track files, uploads |
| `/srv/logs/gx-playground/`, `/srv/logs/gx-music/` (gx10-02) | Playground and music logs |
| `/srv/projects/gx-cluster/secrets/hf/token` | optional Hugging Face token (0600, absent today) |
| `/srv/models/staging` | Model Manager downloads before assignment |
| `/srv/logs/gx-auto-routing.jsonl` | gx-auto decisions and completions |
| `/srv/logs/acceptance/` | acceptance evidence (JSON, media, gx-max runs) |
| `legenex/gateway/.env`, `legenex/media/.env` | ignored; live keys |

## Environment

* No sudo.
* Everything runs through Docker (CDI GPU:
  `--device nvidia.com/gpu=all`) and `systemctl --user`.
* Kernel pinned to `6.17.0-1032-nvidia`; verify with
  `legenex/host/kernel-lock/verify-kernel-lock.sh`.
* SSH to node 2: `legenex-02@gx10-02` (Tailscale, management only).

## Pending decisions and limitations

* ~~**B-030**: the approved gx-reason checkpoint is gated per user.~~
  **CLOSED as obsolete, 2026-09-18 (D-042).** The user replaced the target with
  the ungated `wyattearp/Qwen3.8-27B-Uncensored-NVFP4`, which is installed and
  live. The iSkye gate still exists; the model behind it is no longer wanted.
  **There is no human blocker for gx-reason any more.**
* **B-023:** node 1 still reaches the swap ceiling during the gx-max load. It
  was re-measured on 2026-09-17: minimum 9.4 GiB available, swap at the
  ceiling. Whether the drain should also stop Open WebUI and AgentOS remains
  undecided.
* **B-015:** sshd, tailscaled and friends cannot be OOM-protected without
  root.
* **B-016:** there is no remote power-cycle path.
* **gx-music limits:** extract, lego and complete need ACE-Step XL base
  (not installed). Cancelling a running render lets it finish, then discards
  it.
* **B-028:** the keyframe video edit cannot keep the 30 GiB reserve and is
  refused (two-stage redesign pending a decision).
* **B-029:** production Open WebUI shares the Kilo Code gateway key.
* **Server-side status:** GitHub branch protection and secret scanning are
  not configured from here.

* **gx-call runtime is unproven.** The checkpoint is downloaded and verified on
  gx10-02, but the engine image build was interrupted and the model has never
  been loaded, so no footprint can be published and Resource Control shows
  "not measured yet" for `gx-call`.
* **Native full-duplex is not realtime on GB10** (measured: 1.5-1.6 s per 1 s
  unit while speaking). gx-live ships VAD turn-taking with barge-in instead.
* **Wan LoRAs apply to text-to-video only**; the public `gx-video` gateway path
  does not pass `loras`.

## Test commands

```bash
(cd legenex/orchestrator && python3 -m unittest discover -s tests)
(cd legenex && python3 -m unittest discover -s lifecycle/tests -t .)
(cd legenex/media/router && bash qa.sh)             # router 2.5.0: templates, compose, 174 tests, secret scan
legenex/tests/gx_tier_acceptance.py gx-mini          # per-tier real checks
legenex/tests/gx_media_acceptance.py                 # t2i/edit/variation/t2v/i2v/v2v through the gateway
legenex/tests/gx_ui_live_check.py keys library manager
legenex/tests/unwind-tests.sh E1 E2 E3 E4 E6         # non-destructive
legenex/tests/acceptance.sh                          # live tiers
legenex/tests/gx-max-inference.sh                    # against a running gx-max
ops/git-sync/integrity-audit.sh
ops/git-sync/tests/sync-regression.sh                # hermetic Git-sync failure paths (19 checks)
(cd legenex/control-ui && npm run qa)                # Control Center: lint, types, unit/API tests, build, E2E + axe, security
(cd legenex/playground && npm run qa)                # GX-Playground: proxy tests, build, E2E + axe, security
(cd legenex/music && ./qa.sh)                        # gx-music supervisor (hermetic)
(cd legenex && python3 -m unittest discover -s lifecycle/tests -t .)   # includes the gx-max music drain
(cd legenex/control-ui && npm run test:live)         # UI against the real cluster (real model calls)
legenex/tests/owui_identity_acceptance.py            # production Open WebUI via chat.legenex.co (disposable account)
legenex/tests/owui_identity_browser.py               # the same in system Chrome
legenex/tests/reserve_live_acceptance.py             # gx10-02 video + music with 1 Hz memory sampling
(cd legenex/control-ui && python3 -m gx_control_ui.owui_identity check)

# Build V3 (D-040, D-041)
(cd legenex/voice && ./qa.sh)                        # gx-voice supervisor + engine (hermetic)
(cd legenex/call  && ./qa.sh)                        # gx-call supervisor (hermetic)
(cd legenex/live  && ./qa.sh)                        # gx-live supervisor + engine (hermetic)
(cd legenex/playground/flows-ui && npm run qa)       # Creative Flows editor: tsc, eslint, vitest, bundle freshness
legenex/playground/scripts/deploy.sh                 # deploy AND prove what the browser is served
legenex/control-ui/scripts/deploy.sh                 # same for the Control Center; also applies pending migrations
(cd legenex/playground && npx playwright test --project=live e2e/live.navigation.spec.js)
                                                     # the deployed product: 13 pages, no dead links, axe WCAG 2.2 AA
(cd legenex/control-ui && .venv/bin/python -m gx_control_ui.footprints sync)
                                                     # record measured footprints from coordination/build-v3/*.md
python3 legenex/tests/media_footprint_probe.py i2v:640x640:33   # media memory growth, 1 Hz, admission-guarded
```

**Invocation note:** these suites import `tests/support.py` as a top-level
module, so `python -m unittest tests.test_x` fails with
`ModuleNotFoundError: support`. Always use
`python3 -m unittest discover -s tests [-p 'test_x.py']`.

**Running Playwright while another workstream is:** give your run its own
output directory and ports, or the runs delete each other's traces —
`GX_E2E_OUTPUT_DIR=test-results/<name> GX_E2E_BACKEND_PORT=<n> GX_E2E_PORT=<n+1>`.

## Next logical step

gx-reason, gx-call, gx-mini, gx-fast, gx-auto and the media tiers are all live
and accepted. The remaining work is finishing the Creative Flows adapter layer
and its two worked templates (see `coordination/build-v3/`), and the periodic
full regression. Nothing in the project is currently waiting on a human.

Then, in order:

1. Finish the `gx-call-engine` image build on gx10-02 and run the first guarded
   cold load to publish a measured `gx-call` footprint.
2. Decide B-023 (node 1 reaches the swap ceiling during the gx-max load).
3. Decide B-028 (split the keyframe video edit into two stages, or keep it
   refused).
4. Decide B-029 (give production Open WebUI its own gateway key).
