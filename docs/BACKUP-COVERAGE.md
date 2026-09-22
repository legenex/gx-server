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

## Offsite

Private GitHub holds the recipe only. No paid offsite mutable-data destination was present. Fully independent offsite backup of chats/databases is **absent**.
