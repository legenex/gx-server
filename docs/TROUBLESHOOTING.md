# Troubleshooting

| Symptom | Check |
|---|---|
| `gx doctor` SSH fail | `ssh gx10-02 hostname`; Tailscale `tailscale status`; key `~/.ssh/id_ed25519_gxcluster` |
| Gateway 401 | LiteLLM master key from `legenex/gateway/.env` (do not print it) |
| gx-mini OOM 137 | memory limit must be ≥24g for Ornith Q8; ctx 65536 not 131072 |
| gx-code slow/empty | llama-swap may still be serving gx-fast/gx-reason during cutover; `docker ps` |
| Backup empty password | `RESTIC_PASSWORD_FILE=~/.config/gx-backup/restic-password` |
| Control UI stale JS | unit re-reads mtime; restart `systemctl --user restart gx-control-ui` |
| Orchestrator only on loopback | `wait-for-docker0.sh`; restart `gx-orchestrator` |
| 200 Gbps down | `ethtool enp1s0f0np0` / `enP2p1s0f0np0`; `rdma link` |
| OpenWebUI lost chats | restore `open-webui-data.tar.gz` into the volume; do not `docker volume rm` |
