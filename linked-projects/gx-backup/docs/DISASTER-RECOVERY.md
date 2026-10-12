# Disaster recovery

## EMERGENCY RECOVERY

If one GX10 dies:

1. On the surviving node, confirm backups: `gx doctor` and `restic -r ~/Documents/Backups/restic/local snapshots` (peer copy of the other node is under `~/Documents/Backups/restic/peer-gx10-02` on gx10-01, or `~/Documents/Backups/restic/peer-gx10-01` on gx10-02).
2. Install Ubuntu 24.04 on the replacement ASUS GX10 (NVIDIA GB10 image).
3. Recreate user `legenex` (gx10-01) or `legenex-02` (gx10-02), add to `docker`, `sudo`.
4. Clone this recipe: `git clone git@github.com:legenex/gx-backup.git ~/Documents/Projects/gx-backup`
5. Copy `~/.config/gx-backup/restic-password` and `age.key` from the surviving node.
6. `./install` then `gx restore latest`
7. Recreate models: `hf download` commands in `models.lock`
8. Bring up compose from `Documents/Projects/Server/gx-cluster/legenex/gateway`
9. `gx doctor`

If **both** nodes die and only GitHub remains: you have the recipe, not the latest chats/databases. Independent offsite mutable backup was not configured.

## Restore one file

```
export RESTIC_PASSWORD_FILE=$HOME/.config/gx-backup/restic-password
restic -r $HOME/Documents/Backups/restic/local restore latest --target /tmp/restore --include /path/to/file
```

## Restore a database

SQL dumps are in each snapshot under `Documents/Backups/dumps/<stamp>/`. Restore into a throwaway postgres container first, never onto a live DB without a typed confirmation.

## AgentOS

Canonical checkout: `/home/legenex/Documents/Projects/AgentOS`.

User systemd units `agentos-control-center.service` and `agentos-supervisor.service` must set:

```
AGENTOS_CC_REPO=/home/legenex/Documents/Projects/AgentOS
AGENTOS_CC_DEV_REPO=/home/legenex/Documents/Projects/AgentOS
AGENTOS_CC_PROD_REPO=/home/legenex/Documents/Projects/AgentOS
AGENTOS_CC_LLM_URL=http://127.0.0.1:4000/v1
AGENTOS_CC_LLM_MODEL=gx-code
```

Do not recreate `/home/legenex/agentos` unless you intend a separate serving mirror. The obsolete path is not required.

Control Center: `http://127.0.0.1:4173` (also Tailscale).

## OpenWebUI

Container `open-webui`, host network port 3000, volume `open-webui`. Restore `open-webui-data.tar.gz` into that volume. Do not wipe chats.

Production OpenAI connection (host-network OpenWebUI):

- URL: `http://127.0.0.1:4000/v1`
- Auth: Bearer
- Key: `LITELLM_MASTER_KEY` from `legenex/gateway/.env`
- Model IDs: `gx-auto`, `gx-mini`, `gx-code`, `gx-max`

External clients use `http://100.105.214.61:4000/v1` via `gx-gateway-ts-proxy.service` (Docker stays loopback-only so compose can boot before Tailscale exists).

## Four modes only

Public logical models: `gx-mini`, `gx-code`, `gx-auto`, `gx-max`.

Internal workers `gx-code-01` and `gx-code-02` stay addressable on loopback for gx-code load-balancing and gx-max dual-worker. They are filtered from the Tailscale `/v1/models` listing.

Do **not** restore GX-Playground, media routers, gx-fast/gx-reason as primary user models, or ports 8090/8443 as part of normal recovery. Source remains in git history and older restic snapshots.

## Gateway boot

Keep `GX_LITELLM_BIND=127.0.0.1` and `GX_LITELLM_TS_BIND=127.0.0.2`. Enable `gx-gateway-ts-proxy.service` so Tailscale `:4000` appears after Tailscale is up.

## Postgres dumps

`litellm.sql.gz` is `pg_dumpall` from Postgres 16.15. It includes `\\restrict` / `\\unrestrict` tokens. Restore into a throwaway cluster as superuser after stripping those lines, never onto production without a typed confirmation.
