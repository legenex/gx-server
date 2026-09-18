# Handoff

The *latest* handoff for whoever (human or agent) picks this up next. This file
is never a history — see `CHANGELOG.md`, `TEST_RESULTS.md` and
`coordination/DECISIONS.md` for that.

Last updated: 2026-09-17 ~22:10 SAST, by the lead agent on gx10-01, after the
Build V3 integration pass (D-040, D-041).

## READ THIS FIRST — gx-reason changed, and B-030 is closed

**There is no longer anything waiting on a human for gx-reason.**

gx-reason now serves **`wyattearp/Qwen3.8-27B-Uncensored-NVFP4`** @
`91ec573a3d8e660b78b7161395e4a5b6247c2c8b` ("Qwen3.8-27B Dense Uncensored
NVFP4"), installed, verified and live since 2026-09-18 (D-042).

That replaced **both** of the previous identities, on the user's explicit
instruction:

* `nvidia/Qwen3.6-27B-NVFP4` — the interim checkpoint. **Deleted from gx10-02.**
  It is not a fallback. Do not reinstall it.
* `iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` — the previously approved target.
  **Abandoned.** Do not continue troubleshooting its Hugging Face gate and do
  not rotate tokens for it. **B-030 is closed as OBSOLETE** — the gate is real,
  but the model behind it is no longer wanted.

Do not confuse the new checkpoint with the retired
`gx10-vllm/Qwen3.8-27B-Uncensored` runtime (locked decision 15), which stays
retired. Similar names, different artefacts: one is a local runtime folder, the
other an upstream NVFP4 repository at a pinned revision.

Measured on 2026-09-18, not inherited: 51.2 GiB node footprint at
`--gpu-memory-utilization 0.42`, 392 s cold load, ~9 tok/s decode, 65 536
context. Reasoning, coding, tool calling and vision all exercised live, through
the LiteLLM `gx-reason` alias. Unloads cleanly; memory returns.

## What is running

Eleven aliases (L-10 as amended by D-040). gx-max is **working** — the old
B-022 "gx-max cannot run on this hardware" conclusion was wrong and was
superseded by D-025 on 2026-09-16. gx-max is never auto-started; it takes over
both nodes when you ask for it through the Control Center MAX profile.

| Where | Service | Port |
|---|---|---|
| gx10-01 | Control Center | 8088 |
| gx10-01 | GX-Playground | 8090, and **8443 over HTTPS** |
| gx10-01 | LiteLLM gateway | 4000 |
| gx10-01 | orchestrator | 18900 |
| gx10-02 | media router 2.5.0 | 18800 |
| gx10-02 | gx-music / gx-voice / gx-live supervisors | 18820 / 18830 / 18850 |
| gx10-02 | gx-call supervisor | 18840 — **not yet running**, its engine image is unfinished |

The Playground shows the complete product: **Create** (Dashboard, Creative
Flows, Images, Video, Music, Voice) · **Realtime** (Live, Call Agents) ·
**Manage** (Library, History, Models, Logs, Settings).

## Three things that will save you time

1. **Editing the source is not deploying it.** Both web apps cache `web/` in
   memory. They now re-read a changed file, but the Python package still loads
   once and Creative Flows ships a built bundle. Run
   `legenex/playground/scripts/deploy.sh` (or the Control Center's) — it fails
   unless the ETag of every served file matches sha256 of the file in the
   checkout. This is not paranoia: the deployed Playground once served a
   seven-hour-old copy of itself with eleven files 404ing (B-031).
2. **Use HTTPS for the microphone and camera.** `http://100.105.214.61:8090` is
   not a secure context, so Live and Call Agents cannot capture audio there.
   Use `https://100.105.214.61:8443/` with the CA from `/pg/ca.crt` trusted, or
   `http://127.0.0.1:8090/` on gx10-01 itself. Manual §7.0 has the steps.
3. **Run the suites with `discover`.** `python -m unittest tests.test_x` fails
   with `ModuleNotFoundError: support`; use
   `python3 -m unittest discover -s tests [-p 'test_x.py']`. And if another
   workstream is running Playwright, give your run its own
   `GX_E2E_OUTPUT_DIR`, `GX_E2E_BACKEND_PORT` and `GX_E2E_PORT`.

## What is proven, and what is not

**Proven with real generations:** Wan 2.2 LoRA video (branch placement traced
on the real graph, 49 decoded frames, measurably different output at the same
seed), gx-voice text-to-speech / voice design / authorised cloning (verified
independently with ffprobe and offline ASR, and end to end through the
gateway), image generation and editing, and the I2V memory footprint.

**Not proven:** the gx-call runtime. Its checkpoint is downloaded and verified
on gx10-02, but the engine image build was interrupted and the model has never
been loaded, so no footprint can be published and Resource Control correctly
shows "not measured yet" for `gx-call`.

## The rules that matter most

* 30 GiB `MemAvailable` reserve on each node, always. Start a GPU container
  only through `gx_guard_run` or a supervisor that does.
* Never `docker stop` a tenant's engine container; never call ComfyUI `/free`
  directly. Use the sanctioned unload paths (D-036).
* Never start gx-max without meaning to; never change kernel, firmware,
  netplan, MTU or RDMA. No sudo anywhere.
* gx10-01 is the only Git writer. gx10-02 is a pull-only mirror.

## Where to look next

`CURRENT_STATE.md` (what is running), `ARCHITECTURE.md` (what is locked),
`coordination/BLOCKERS.md`, `PROJECT_MAP.md` → *Next logical
step*, and `coordination/build-v3/*.md` for each workstream's own evidence.
