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

## Latest verified point

Stamp `20260923T091634Z` (`~/Documents/Backups/LATEST`).

| Copy | ID |
|---|---|
| GX10-01 local | `24ba5393` |
| GX10-02 local | `fa6b8856` |
| GX10-01 on GX10-02 | `f5588ad0` |
| GX10-02 on GX10-01 | `bbee85a2` |

Earlier snapshots are kept.

## Also excluded

Restic repositories themselves (`Documents/Backups/restic/**`). Backing them up into themselves ballooned snapshots 18→137 GiB; that is fixed.
