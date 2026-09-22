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

## OpenWebUI

`open-webui-data.tar.gz` in the dump directory. Stop the container, replace the volume contents, start it.

## AgentOS

Live path: `/home/legenex/agentos`. Git copy: `/home/legenex/Documents/Projects/AgentOS`. Restore either tree from restic; systemd unit `agentos-control-center.service` points at the live path.
