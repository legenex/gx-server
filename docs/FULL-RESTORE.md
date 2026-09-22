# Full restore

Clean-machine workflow:

```
git clone <gx-backup> ~/Documents/Projects/gx-backup
cd ~/Documents/Projects/gx-backup
# restore ~/.config/gx-backup from surviving node or printed recovery copy
./install
gx restore latest
```

`gx restore latest` is the **validation** path by default (writes under `/tmp/gx-restore-validate`). Full overlay onto `/` is documented here and must be invoked with restic `--target /` only after you have a second verified copy.

Steps the install script performs:

1. Puts `gx` on `PATH` (`~/.local/bin`)
2. Initialises restic repos if missing
3. Does not download 200 GB of public weights (use `models.lock`)

Then restore gx-cluster source from restic (`/home/legenex/Documents/Projects/Server/gx-cluster`) and start:

```
cd ~/Documents/Projects/Server/gx-cluster/legenex/gateway
docker compose -f docker-compose.gateway.yml up -d
systemctl --user enable --now gx-orchestrator gx-control-ui gx-backup.timer
```

Verify with `gx doctor`.
