# Control UI

## About this interface

| | |
|---|---|
| URL | `http://100.105.214.61:8088/` (Tailscale) or `http://127.0.0.1:8088/` on gx10-01 |
| Service | `gx-control-ui.service` (systemd **user** unit on gx10-01, starts at boot) |
| Code | `legenex/control-ui/` (Python standard library backend, plain ES-module frontend) |
| Logs | `/srv/logs/gx-control-ui/control-ui.log` (JSON lines), `/srv/logs/gx-control-ui/audit.log` |
| State | `/srv/projects/gx-cluster/state/control-ui/` |
| Password store | `/srv/projects/gx-cluster/secrets/control-ui/auth.json` (mode 0600, scrypt hash) |
| Health | `GET /api/health` (liveness), `GET /api/ready` (readiness) |

The UI is an **interface** to the existing control plane. It never schedules
or launches models by itself; every change maps to one fixed, audited
operation.

## Pages

| Page | What it shows / does |
|---|---|
| Dashboard | overall state, both nodes, rails, models, gx-max lifecycle, locks and ledger, Git sync, services, warnings, queue |
| Models | the seven aliases with facts, live state, last results, and LOAD / UNLOAD / RESTART |
| Runtime | per-node memory, swap, PSI, load, temperatures, containers, units; llama-swap, orchestrator, media, hostwatch |
| Cluster | topology of the two nodes, both RoCE rails with live throughput, Tailscale, SSH |
| Jobs / Queue | gx-max phases, live lifecycle output, job history, media queue, UI operations |
| Logs | 25 predefined, redacted log streams with filter, refresh and download |
| API Playground | chat, vision, tools, streaming, image and video against the real gateway, with curl/Python/JS snippets |
| Docs | this documentation |
| Settings / System | versions, commit and sync state, endpoints, units, directories, safe maintenance actions |

The UI refreshes health every 3–10 seconds while the tab is visible and
pauses when it is hidden. Use the pause button in the top bar to freeze it.

## Authentication

* One local admin account. Sessions are server-side, expire after 1 hour
  idle or 12 hours total, and use an `HttpOnly`, `SameSite=Strict` cookie.
* Every state-changing call needs a per-session CSRF token and a same-origin
  request.
* Five failed logins from one address lock that address out for 15 minutes.
* The browser never receives a LiteLLM, llama-swap or media key.

**Set or reset the password** (on gx10-01, in a terminal):

```bash
cd ~/Documents/Projects/Server/gx-cluster
legenex/control-ui/scripts/gx-ui-passwd              # prompts twice
legenex/control-ui/scripts/gx-ui-passwd --status     # shows whether one is set
```

Changing the password signs out every session immediately. At first
installation a random password is written to
`/srv/projects/gx-cluster/secrets/control-ui/initial-admin-password`
(mode 0600). Read it once, then set your own; the file is deleted when you do.

## Service management

```bash
systemctl --user status gx-control-ui.service
systemctl --user restart gx-control-ui.service
journalctl --user -u gx-control-ui.service -n 50
tail -f /srv/logs/gx-control-ui/control-ui.log
curl -sS http://127.0.0.1:8088/api/ready
legenex/control-ui/scripts/install.sh      # (re)install the unit; idempotent
```

The unit restarts on failure, is capped at 512 MB of memory, and never
starts a model.

## Safety of operations

* Model operations refuse while gx-max is loading, serving or releasing, and
  while a gx-max rank container exists.
* gx-max LOAD requires typing `gx-max`; Force release requires typing
  `FORCE RELEASE`; other disruptive operations need a confirmation.
* Only one model/infrastructure operation runs at a time.
* There is no shell, no arbitrary command, no file browser, and no upgrade,
  kernel or firmware button.
* Every operation, login and refusal is written to the audit log with user,
  client address, outcome and duration.
