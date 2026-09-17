# Build V3 — creative + realtime platform (D-040)

Started 2026-09-17 16:30 SAST on gx10-01 (lead agent). This file is the
shared contract for the parallel workstreams. Read it completely before
changing anything. `CLAUDE.md` (L-1..L-10) still wins wherever it is stricter.

## Scope

| WS | Owner | Parts |
|---|---|---|
| IMG | image specialist | image edit fix, VisionmasterPro_V3, image model selector, masks |
| WAN | video specialist | Wan 2.2 LoRA library, pairing, presets, workflow builder, video history |
| MUS | music specialist | music UX order, style tags, vocals fix, Build with AI, reference analysis |
| VOI | voice specialist | `gx-voice` (Qwen3-TTS 1.7B), Voice Studio |
| CAL | call specialist | `gx-call` (NemotronLabs VoiceChat 11B), Call Agents, IntakePilot API |
| LIV | live specialist | `gx-live` (MiniCPM-o 4.5), Live page |
| FLO | flows specialist | Creative Flows engine + React/TypeScript/@xyflow/react canvas, templates, AI flow creation |
| PLT | platform specialist | realtime WebSocket tunnel, HTTPS listener, node-2 tenant accounting for new services, Resource Control + Control Center updates, gateway aliases, Playground Models/Logs/Settings pages |
| LEAD | lead | contracts, shared modules, integration, reviews, top-level docs, release, Git |

## Facts established by the lead (verified 2026-09-17)

* `pornmasterPro_noobV3VAE` is **not** on either node. Civitai's noob-V3
  download (`/api/download/models/1767015`) answers **401** (needs an API key).
  The exact name exists on Hugging Face as
  `votepurchase/pornmasterPro_noobV3VAE` @ `75f59d136b165d48f3e678bb057af99f7cf1a71e`
  (public, not gated, diffusers-format SDXL/NoobAI: unet 10.27 GB,
  text_encoder 0.49 GB, text_encoder_2 2.78 GB, vae 0.33 GB; the `.fp16.`
  files have the same LFS sha256 as the plain files, so download each once).
  Licence on the card: creativeml-openrail-m.
* Model repositories verified on the live HF API (all public, not gated):

  | Repo | Revision | Size |
  |---|---|---|
  | `nvidia/NVIDIA-NemotronLabs-VoiceChat-11B` | `a4c40ca5b4fe77db13e9840ca4a2b91becf030c8` | 44.4 GB, licence openmdw-1.1, code: github NVIDIA-NeMo/Speech branch `nemotron-labs-voicechat` |
  | `openbmb/MiniCPM-o-4_5` | `503e754207c94da6bb26850b4469f367c9ea3582` | 20.1 GB, apache-2.0 |
  | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | `fd4b254389122332181a7c3db7f27e918eec64e3` | 4.5 GB, apache-2.0 |
  | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | `0c0e3051f131929182e2c023b9537f8b1c68adfe` | 4.5 GB |
  | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | `5ecdb67327fd37bb2e042aab12ff7391903235d3` | 4.5 GB |
  | `nvidia/personaplex-7b-v1` | `fdaf4090…` | gated=auto (optional A/B only; no HF token exists) |

* Wan 2.2 known-good workflow: `legenex/media/workflows/wan22-t2v-a14b-uncensored.api.json`
  (UNETLoader 3/4 → base LightX2V LoRA 5/6 → ModelSamplingSD3 7/8 →
  KSamplerAdvanced 12/13). User LoRAs go **between 5→7 (high) and 6→8 (low)**.
  Known-compatible Wan 2.2 T2V LoRA pair already installed for LoRA acceptance:
  `wan2.2_t2v_lightx2v_4steps_lora_v1.1_{high,low}_noise.safetensors` in
  `/srv/models/video/loras/` (gx10-02).
* ComfyUI (`gx-comfyui`, gx10-02) model roots: `/srv/models/{shared,image,video}/<kind>/`
  (see `legenex/media/comfyui/extra_model_paths.yaml`, deployed copy in
  `~/gx-media/comfyui/`).
* Image-edit prime suspect: the Playground sends `strength` 0.6 by default,
  which the router binds to `KSampler.denoise` on a 4-step Lightning
  schedule starting from the VAE-encoded source → near-copy. Verify by
  measurement; also check the 2511 reference-latent method.
* Browser microphone/camera need a secure context. `http://100.105.214.61:8090`
  is NOT one. `http://127.0.0.1:8090` is. Tailscale HTTPS is not enabled for
  the tailnet (no cert domains). The Cloudflare tunnel is root-managed and
  public — **do not add routes to it.**
* **Another Claude session is active on gx10-01** (D-039 text-inference work,
  gx-reason acceptance). Do not touch `legenex/orchestrator/`,
  `legenex/gateway/litellm/gx_hooks/`, the gx-reason/llama-swap configs, and do
  not unload gx-reason while `gx_tier_acceptance.py` runs (`pgrep -f gx_tier_acceptance`).

## Hard rules for every workstream

1. **Git.** Edit only the gx10-01 checkout. Autosync commits after 45 s quiet
   and pushes; gx10-02 pulls within ~1 min. Never edit, commit or push on
   gx10-02. Never `git push --force`, never `git stash`/`reset`/`checkout --`
   on shared files (other agents are editing). No secrets in tracked files.
2. **Memory (30 GiB reserve, L-1).** Any GPU container on either node is
   started ONLY through `legenex/lifecycle/resource-guard.sh`
   (`gx_guard_run <node> <name> <class> <est_gib> -- docker run ...`) or by a
   supervisor that does exactly that, so the node flock, the residency ledger
   and the reserve apply. Never a bare `docker run --device nvidia.com/gpu=all`.
   Measure MemAvailable (1 Hz) during every first load and record it.
   Never stop another tenant's container; use its sanctioned unload path
   (router `free_node`, music `POST /v1/music/unload {"if_idle":true}`).
3. **Never start gx-max.** Never change kernel, firmware, netplan, MTU, RDMA.
   No sudo. GPU = `--device nvidia.com/gpu=all` (CDI).
4. **Services on gx10-02** bind the fabric address `192.168.100.11` only (plus
   127.0.0.1), authenticate with a bearer key, and never face a browser.

   | Service | Port | Key file (0600, both nodes) | systemd user unit |
   |---|---|---|---|
   | media router (existing) | 18800 | `secrets/media-router/...` (existing) | container |
   | gx-music (existing) | 18820 | `secrets/gx-music/api-key` | `gx-music.service` |
   | **gx-voice** | **18830** | `secrets/gx-voice/api-key` | `gx-voice.service` |
   | **gx-call** | **18840** | `secrets/gx-call/api-key` | `gx-call.service` |
   | **gx-live** | **18850** | `secrets/gx-live/api-key` | `gx-live.service` |

   `secrets/` = `/srv/projects/gx-cluster/secrets`. Generate keys with Python
   `secrets.token_urlsafe(32)`, copy to gx10-02 over ssh with `umask 077`, never
   print them. Follow `legenex/music/` as the reference supervisor: unit
   symlinked from the node-2 checkout, on-demand engine container, idle unload,
   honours `state/guard/node2.gxmax-hold` and `node2.maintenance-hold`,
   open `GET /health` publishing `{"state", "memory": {"pending_gib", "resident_gib"}}`
   (D-038 pending-memory contract), `POST /v1/<svc>/unload {"if_idle": true}`.
   Placement is decided from measured footprints; the default is gx10-02
   (L-2). A service on gx10-01 needs the lead's sign-off first.
5. **Model files** go under `/srv/models/<family>/<repo-name>` on the node that
   runs them, downloaded at a pinned revision, verified with
   `legenex/scripts/hf-verify.py` where possible, and recorded in
   `legenex/models/registry.json` (new aliases get an entry; image variants
   go under `gx-image.components`/`variants`).
6. **Persistence.** One application database: the Library's
   `/srv/projects/gx-cluster/media/metadata/library.db`. Add tables ONLY via
   your own `legenex/control-ui/gx_control_ui/migrations/NNN_<name>.sql`
   (prefix table names; numbers: 020 WAN, 030 FLO, 040 VOI, 050 CAL, 060 LIV,
   070 IMG, 080 PLT). Get a connection with `app.library.connect()`. Library
   assets are created only through `MediaLibrary.add(NewAsset(...))`, which
   now carries `flow_id`, `flow_run_id`, `flow_node_id`, `source_kind`,
   `source_ref`. New operations: `tts`, `voice_design`, `voice_clone`,
   `composite`, `flow`, `recording`.
7. **Backend (gx10-01, `legenex/control-ui`).** Stdlib Python. Add routes in
   your own module `gx_control_ui/routes_<ws>.py` using
   `from .server import route, Handler` and import it at the end of
   `server.py` next to `routes_v2` (one line each). Attach your service object
   to `App` in `App.__init__` (one block each). Browser routes live under
   `/api/<feature>/...` and inherit session auth + CSRF + same-origin.
   Public API routes live under `/v1/<feature>/...`, are authenticated with a
   LiteLLM virtual key that allows the alias (see the `/v1/music` pattern in
   `routes_v2.py`), and never accept a session cookie.
   Outbound requests to user-supplied URLs MUST use `gx_control_ui/netguard.py`.
8. **Playground (gx10-01, `legenex/playground`).** Vanilla ES modules for all
   pages except Creative Flows (React + TypeScript + `@xyflow/react`, built by
   Vite into `web/flows/`, source in `legenex/playground/flows-ui/`). To add a
   page: one line in `web/js/routes.js`, one `<li>` in `index.html` (in the
   right nav group), the name in `SPA_ROUTE` in `gx_playground/server.py`, the
   name in `GX_BUILD_PAGES` in `scripts/build-check.mjs`, and your API paths in
   `ALLOW` (tight regexes). Use `web/js/ui.js`, `dom.js`, `api.js`, `jobs.js`,
   `workspace.js`, `audio.js` components and the existing design tokens.
   Navigation groups: **Create** (Dashboard, Creative Flows, Images, Video,
   Music, Voice) · **Realtime** (Live, Call Agents) · **Manage** (Library,
   History, Models, Logs, Settings).
9. **Restarting shared services** (`gx-control-ui`, `gx-playground`,
   `gx-media-router`): hold `flock /srv/projects/gx-cluster/state/build-v3/restart.lock`,
   make sure no creative job is running or queued (Playground jobs are in
   memory), restart, verify `/api/ready`. Media router: redeploy with
   `legenex/media/deploy-node2.sh` only after `legenex/media/router/qa.sh`
   passes on the current checkout (it contains everyone's router changes);
   router version for this build is **2.5.0**.
10. **Quality.** Every change ships with tests in the existing suites:
    `(cd legenex/control-ui && npm run qa)`, `(cd legenex/playground && npm run qa)`,
    `legenex/media/router/qa.sh`, `legenex/music/qa.sh`, and a `qa.sh` for each
    new service directory. A failure caused by someone else's in-progress work
    is reported, not "fixed" by rewriting their code. No mocks, placeholders,
    TODOs, dead buttons or fake generation in shipped code. Acceptance requires
    real inference/generation/playback, with evidence under
    `/srv/logs/acceptance/build-v3/<ws>/`.
11. **Documentation.** Each workstream updates its own in-UI docs page under
    `legenex/control-ui/docs/` (new: `17-voice.md`, `18-call-agents.md`,
    `19-live.md`, `20-creative-flows.md`, `21-video-loras.md`), its section of
    `Manual.md`, `legenex/playground/API.md`, and writes its evidence, results,
    measured footprints, limitations and blockers to
    `coordination/build-v3/<ws>.md`. The lead integrates those into
    `CURRENT_STATE.md`, `TEST_RESULTS.md`, `ARCHITECTURE.md`, `DECISIONS.md`,
    `BLOCKERS.md`, `CHANGELOG.md`, `PROJECT_MAP.md` and `VERSION`.
12. **Blocked?** Write the exact blocker, what was checked, and the smallest
    human action needed into `coordination/build-v3/<ws>.md`, finish
    everything else, and report it. Never silently substitute a different
    model; never claim acoustic/visual analysis or generation that did not
    happen.

## Aliases (L-10 amended by D-040 on the user's explicit instruction)

Eleven public aliases: the existing eight plus `gx-voice`, `gx-call`, `gx-live`.

* `gx-voice`: OpenAI-compatible `POST /v1/audio/speech` through LiteLLM
  (gateway → gx-voice on gx10-02), plus the Playground Voice API.
* `gx-call`, `gx-live`: realtime; exposed on the Playground (`/v1/call/*`,
  `/v1/live/*`, WebSocket upgrade tunnelled by the Playground to gx10-02 over
  the fabric), authenticated with a gateway virtual key that allows the alias,
  like `gx-music`.

## Coordination with gx-cluster-0c (D-039 session), agreed 2026-09-17 ~17:05

* It owns `legenex/orchestrator/**`, `legenex/gateway/**` (except the one
  additive `gx-voice` entry in `litellm/config.yaml`), `legenex/lifecycle/lib.sh`,
  `gx-max-start.sh`, `legenex/tests/gx_tier_acceptance.py`, and Control Center
  `models.py`, `services.py`, `web/js/pages/models.js`,
  `tests/test_text_observability.py`.
* Keep its `litellm_settings.callbacks` (gx_hooks budget hook) and
  `router_settings.disable_cooldowns: true`. The lead (not VOI) recreates
  gx-litellm, only with
  `env -i PATH="$PATH" HOME="$HOME" docker compose --env-file .env -f docker-compose.gateway.yml up -d --no-deps litellm`
  (B-027), keeping its new volume mounts.
* gx-reason on gx10-02 now uses MTP speculative decoding, TTL 20 min,
  footprint ≈ 32 GiB; media jobs queue behind it.
* Media router 2.4.1 (SIGTERM fix in `__main__.py`) is deployed; 2.5.0 keeps it.
* It announces its gx-max cold run (≥ 30-60 min away as of 17:05) before
  starting. No GPU work while `node2.gxmax-hold` exists or gx-max loads.
