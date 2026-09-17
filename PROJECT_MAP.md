# Project map — gx-cluster (two-node GX10)

**Current phase:** production operation of all eight aliases. There is
automated source control, the admin Control Center
(`http://100.105.214.61:8088/`) and the creative GX-Playground
(`http://100.105.214.61:8090/`).
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
| `legenex/media/` | gx-image / gx-video router (2.3.0: holds, pins, truthful residency) and ComfyUI compose (node 2) |
| `legenex/music/` | gx-music: node-2 supervisor, ACE-Step engine Dockerfiles, unit, tests (D-036) |
| `legenex/playground/` | GX-Playground: static SPA + allow-listed proxy, unit, E2E, API contract (D-037) |
| `legenex/host/` | Host watchdog and the kernel-lock tooling |
| `legenex/models/registry.json` | Alias → model bindings (repository, revision, path, previous, interim target, media components) |
| `legenex/scripts/hf-verify.py` | Pinned-revision sha256 verifier; writes `.gx-manifest.json` |
| `legenex/control-ui/` | Control Center: stdlib backend (also the Playground's backend: Library, queues, music, Resource Control, Storage), ES-module frontend, in-UI docs, unit/API/E2E tests (D-028, D-037) |
| `legenex/tests/` | Acceptance, unwind regression, gx-max validation and inference suites |
| `ops/git-sync/` | Writer, mirror and audit tooling for source control (D-026) |
| `.githooks/` | Versioned Git hooks (writer only) |

## Implemented

* **Eight aliases (D-036):** gx-mini, gx-fast, gx-reason, gx-max, gx-auto,
  gx-image and gx-video on the gateway, plus gx-music through the gx10-01
  music API.
* **gx-max** on SGLang, TP=2 across both nodes over RoCE, with:
  * a cluster-takeover admission policy;
  * phase-aware safety rules on both nodes;
  * a deadman on node 2 and a watcher on node 1;
  * a verified unwind.
* **Resource guard:** flock plus residency ledger, with a 30 GiB reserve for
  single-node tiers.
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

* **B-025:** the required gx-reason checkpoint is gated.
  * The only expected human blocker.
  * No Hugging Face token has reached gx10-01 (Settings > Model Manager >
    Hugging Face token).
  * The interim `nvidia/Qwen3.6-27B-NVFP4` keeps serving.
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
* **Server-side status:** GitHub branch protection and secret scanning are
  not configured from here.

## Test commands

```bash
(cd legenex/orchestrator && python3 -m unittest discover -s tests)
(cd legenex && python3 -m unittest discover -s lifecycle/tests -t .)
(cd legenex/media/router && bash qa.sh)             # router: templates, compose, 74 tests, secret scan
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
```

## Next logical step

Close B-025: save an HF token in the Control Center, then use Model Manager to stage, test and assign the iSkye checkpoint on gx10-02 (the disk preflight passes; 328 GB is free). Then decide B-023.
