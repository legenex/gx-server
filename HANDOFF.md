# Handoff

The *latest* handoff for whoever (human or agent) picks this up next. This file
is never a history — see `CHANGELOG.md`, `TEST_RESULTS.md` and
`coordination/DECISIONS.md` for that.

Last updated: 2026-09-17 ~22:10 SAST, by the lead agent on gx10-01, after the
Build V3 integration pass (D-040, D-041).

## READ THIS FIRST — the one thing that needs a human

**`coordination/BLOCKERS.md` B-030.** gx-reason's approved checkpoint
`iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` @ `91c3e3d4…` is **gated per user**,
and the account is not on its authorized list.

This is not a token problem, and it is worth being precise because an earlier
pass lost hours to the opposite conclusion:

* a fine-grained token **is** configured at
  `/srv/projects/gx-cluster/secrets/hf/token` (0600);
* it authenticates as **`legenex`** and already carries
  `canReadGatedRepos: true`;
* repository **metadata** returns **200** with it (92.68 B parameters,
  98.66 GiB — both confirmed);
* repository **files** return **403** with
  `X-Error-Code: GatedRepo`, *"you are not in the authorized list"*.

> **Action:** open
> <https://huggingface.co/iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070> in a
> browser signed in to Hugging Face as **`legenex`** and click **"Agree and
> access repository"**. The repo is `gated: auto`, so access is granted
> immediately. **Creating another token cannot change this.**

Afterwards nothing else is needed from you: Model Manager stages, verifies,
test-serves and assigns the checkpoint on gx10-02, and the interim
`nvidia/Qwen3.6-27B-NVFP4` is deleted **only** after that acceptance passes.
Until then the interim model keeps serving and is not deleted.

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
`coordination/BLOCKERS.md` (B-030 first), `PROJECT_MAP.md` → *Next logical
step*, and `coordination/build-v3/*.md` for each workstream's own evidence.
