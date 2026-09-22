# GX-Cluster UI

URL: `http://127.0.0.1:8088` (Tailscale too). User systemd `gx-control-ui.service`.

Primary nav:

- Dashboard — two machine cards, AUTO/MINI/CODE/MAX
- Models
- Jobs
- Logs
- Machines (`#/cluster`)
- Backup & Recovery (`#/backup`) — status, Back Up Now, Verify, restore-point list
- Settings

Extra pages (Resource Control, Storage, Docs) remain linked as secondary nav. Playground stays at :8090.

Backup actions call `/api/backup/status|now|verify`. Destructive restore is not a one-click UI action.
