# GX Backup

Rebuild recipe and operator command for the dual ASUS GX10 cluster (gx10-01 + gx10-02).

This Git repository is the **installation recipe**. Encrypted restic snapshots hold mutable data. Public model weights are **not** in Git; they are pinned in `models.lock`.

## Everyday commands

```
gx status
gx backup
gx doctor
gx models
gx auto
gx mini
gx code
gx max
gx logs
gx benchmark
```

## After a dead machine

1. Install Ubuntu 24.04 on the replacement ASUS GX10.
2. Clone this repository to `/home/legenex/Documents/Projects/gx-backup`.
3. Restore `~/.config/gx-backup/restic-password` and `age.key` from the surviving node or your independent copy (`docs/RECOVERY-SECRET.md`).
4. Run `./install`
5. Run `gx restore latest`
6. Follow `docs/GX-BACKUP-RESTORE-GUIDE.pdf` / `docs/DISASTER-RECOVERY.md`

## What Git contains

- `gx` CLI and install/backup/restore scripts
- Pinned model IDs, revisions, checksums (`models.lock`)
- Container and package manifests
- Sanitized configs (no plaintext secrets)
- Documentation and the recovery PDF

## What Git does not contain

- Model weights
- Docker image layers
- Plaintext API keys, Tailscale auth keys, SSH private keys, database passwords
- OpenWebUI chat databases (those live in restic)

## Layout

| Path | Role |
|---|---|
| `/home/legenex/Documents/Projects/gx-backup` | this recipe |
| `/home/legenex/Documents/Projects/Server/gx-cluster` | live cluster software |
| `/home/legenex/Documents/Projects/AgentOS` | AgentOS |
| `/srv/models` | model weights |
| `/home/legenex/Documents/Backups/restic/` | encrypted restic data |
| `~/.config/gx-backup/` | restic password + age identity (mode 0600) |

Gateway: `http://127.0.0.1:4000/v1` (also on Tailscale `100.105.214.61:4000`)

Modes: `gx-auto` (default), `gx-mini`, `gx-code`, `gx-max`.

**OFFSITE MUTABLE DATA: NOT CONFIGURED** — cross-node restic is not offsite. See `docs/BACKUP-COVERAGE.md`.

OpenWebUI: `http://127.0.0.1:3000`

GX-Cluster UI: `http://127.0.0.1:8088`
