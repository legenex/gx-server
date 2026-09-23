# Backup coverage

Engine: restic 0.18.1, encrypted, deduplicating. Password: `~/.config/gx-backup/restic-password` on both nodes.

## Included

Projects, Documents, AgentOS (both checkouts), gx-cluster source, systemd user units, SSH metadata, dumps of LiteLLM/PageFlo/FinancialOS Postgres, OpenWebUI volume tarball, `/srv/logs`, `/srv/projects` (minus runtime caches), `/opt/open-webui` compose.

## Cross-node

| Copy | Location |
|---|---|
| gx10-01 data | `gx10-01:Documents/Backups/restic/local` and `gx10-02:Documents/Backups/restic/peer-gx10-01` |
| gx10-02 data | `gx10-02:Documents/Backups/restic/local` and `gx10-01:Documents/Backups/restic/peer-gx10-02` |

## Intentionally excluded

| Path | Class | Reason | Recreated how | Tested |
|---|---|---|---|---|
| `/srv/models` | MODEL | public weights, huge | `models.lock` hf download + sha256 | mini checksums matched after download |
| `/var/lib/docker` overlay | REPRODUCIBLE | image layers | docker pull / compose | images still present locally |
| `node_modules`, `.cache`, `.npm`, `.nvm`, `.vscode-server` | REPRODUCIBLE CACHE | regenerated | npm/pnpm/nvm | n/a |
| `/srv/projects/gx-cluster/runtime` | REPRODUCIBLE CACHE | vLLM compile cache | cold start | excluded after first snapshot permission errors |
| Chrome/VS Code `.deb` in Downloads | DUPLICATE | re-download | vendor URLs | n/a |

## Three different things (do not conflate)

| Layer | What it is | Offsite? |
|---|---|---|
| GitHub `legenex/gx-backup` | Rebuild **recipe** (scripts, pins, docs) | GitHub, not your chats/DBs |
| Cross-node encrypted restic | Mutable data copied GX10-01 ↔ GX10-02 | **No** — still in the same house |
| Independent offsite mutable backup | A restic copy that survives losing **both** GX10s | **OFFSITE MUTABLE DATA: NOT CONFIGURED** |

Cross-node restic is not offsite. That is a recommended future improvement, not a reason to keep this project open.

## Recovery secret

See `docs/RECOVERY-SECRET.md`. Total dual-machine loss also needs an independent copy of `restic-password` stored by the operator.

## Playground / media coverage (verified in `24ba5393` before removal)

Pre-cleanup snapshot **`24ba5393` / `20260923T091634Z`** already held recoverable copies of:

- GX-Playground source (`Documents/Projects/Server/gx-cluster/legenex/playground`)
- media router, voice, music, call, live source under `legenex/`
- `legenex/media/docker-compose.media.yml`, LiteLLM config, llama-swap YAML
- user systemd `gx-playground.service`
- `/srv/projects/gx-cluster/media` library (audio/images/videos/voice/metadata)
- `/srv/projects/gx-cluster/secrets` (playground-tls, gx-voice/music/call/live keys)
- `/srv/projects/gx-cluster/state` (music jobs, model-results)
- coordination docs (`MODELS.md`, `plt.md`)

Media **weights** under `/srv/models/{image,video,music,voice,live,voicechat,...}` were intentionally excluded (public/reproducible). They were deleted from gx10-02 after this verification. Recreate from Hugging Face / original download scripts in git if media is ever revived.

Git history `legenex/gx-server` is the recipe copy of the same source.

## Latest verified point

Post-cleanup stamp **`20260923T223525Z`** (`~/Documents/Backups/LATEST`). `restic check --read-data-subset=5%`: no errors.

| Copy | ID |
|---|---|
| GX10-01 local | `cf1568fd` |
| GX10-02 local | `ab30d3cb` |
| GX10-01 on GX10-02 | `6ebfdcdf` |
| GX10-02 on GX10-01 | `29e8aed9` |

Pre-cleanup verified point `20260923T091634Z` (`24ba5393` / `fa6b8856`) is kept.

Earlier snapshots are kept.

## Also excluded

Restic repositories themselves (`Documents/Backups/restic/**`). Backing them up into themselves ballooned snapshots 18→137 GiB; that is fixed.
