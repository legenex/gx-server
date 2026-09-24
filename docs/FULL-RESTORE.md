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
systemctl --user enable --now gx-orchestrator gx-control-ui gx-backup.timer gx-gateway-ts-proxy
systemctl --user enable --now agentos-control-center agentos-supervisor
# OpenWebUI: docker compose in /opt/open-webui (host network :3000)
```

Do not enable `gx-playground` (it is masked to `/dev/null`). Do not start media compose.

Verify with `gx doctor`. Public models: gx-mini, gx-code, gx-auto, gx-max.
