# Filesystem layout

## User-facing (gx10-01)

```
/home/legenex/Documents/
  Projects/
    gx-backup/          this recipe
    Server/gx-cluster/  live cluster software
    AgentOS/            git-oriented AgentOS checkout
    PageFlo/ FinancialOS/ ProjectOS/ DataScraper/ GrowthOS/
  Backups/restic/       encrypted restic data
  Archive/pre-cleanup-<stamp>/
  Exports/
  Reference/
  Misc/
  Obsidian/
```

Live AgentOS systemd still runs from `/home/legenex/agentos` (ahead of origin). Do not delete it.

## System / data (do not move into Documents)

| Path | Role |
|---|---|
| `/srv/models` | GGUF and vLLM/DeepSeek weights |
| `/srv/projects/gx-cluster` | runtime state, vLLM caches, secrets |
| `/srv/logs` | cluster logs |
| `/opt/open-webui` | compose file |
| `/var/lib/docker` | containers and the `open-webui` volume |
| `~/.config/gx-backup` | restic password, age identity |
| `~/.config/systemd/user` | gx-* and agentos units |

## gx10-02

User `legenex-02`. Models at `/srv/models`. Cluster checkout under `/srv/ai-stack` and `~/Documents/Projects/Server`. Backups at `~/Documents/Backups/restic/`.
